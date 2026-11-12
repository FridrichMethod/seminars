"""The ``claude-cli`` backend: Claude Code in print mode, paid by the Claude subscription.

Verified with Claude Code 2.1.322 (the version is recorded in
config/llm.yaml and installed at exactly that version in CI). The command,
with the prompt on standard input::

    claude -p --model <model> \\
      --tools "" --strict-mcp-config --disallowedTools "mcp__*" \\
      --setting-sources "" --disable-slash-commands \\
      --permission-prompts none --no-session-persistence \\
      --max-budget-usd <limit> \\
      --output-format json --json-schema '<schema>' \\
      --system-prompt '<system prompt>'

What each part is for:

* ``--tools ""`` switches every built-in tool off. It does not cover MCP
  tools, hence ``--strict-mcp-config`` (no MCP configuration is loaded) and
  ``--disallowedTools "mcp__*"``.
* ``--setting-sources ""`` reads no user, project or local settings and
  ``--disable-slash-commands`` loads no skills or commands, so nothing on
  the machine can add tools, hooks or instructions.
* ``--permission-prompts none`` denies whatever would ask for a permission.
* ``--no-session-persistence`` writes no transcript of page text to disk.
* ``--max-budget-usd`` and the timeout of the child process bound a request;
  this version has no ``--max-turns``. For the same reason ``max_tokens``
  of a request cannot be passed on.
* ``--bare`` must not be used: bare mode does not read
  ``CLAUDE_CODE_OAUTH_TOKEN``.
* The schema and the system prompt are arguments; both are written by this
  project. The user prompt, which holds page text, is never an argument.

The child runs in an empty temporary working directory, removed after the
call, with an environment that holds exactly these variables
(``CHILD_ENV_NAMES``) and nothing else of this process:

* PATH, HOME, LANG, LC_ALL and TMPDIR as this process has them, the
  general variables of every child (``seminars.llm.cli`` says why each);
* CLAUDE_CODE_OAUTH_TOKEN, the login: the subscription token, stripped.
  It is the only credential the client gets;
* CLAUDE_CONFIG_DIR, a new empty temporary directory, removed after the
  call, so the client neither reads nor changes the configuration in the
  home directory (no settings, memory or plugins of the owner) and leaves
  nothing behind;
* DISABLE_AUTOUPDATER=1 and CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1,
  set here, not passed on: the client does not update itself and sends
  nothing on the side.

ANTHROPIC_API_KEY in particular never reaches the client: with it the
client would bill the API instead of the subscription. ``--version`` runs
with the general variables and the two set here, without a login. The
chain only builds this backend with a token. Built without one (a local
test), the client uses the login it finds itself: then a CLAUDE_CONFIG_DIR
of this process, where that login is, is passed on instead of a temporary
one.

The answer is the JSON object on standard output. It counts only when
``is_error`` is false, ``subtype`` is ``success`` and ``structured_output``
is present; the latter is validated like every answer.
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from seminars.contracts import (
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProviderError,
    LLMTimeoutError,
)
from seminars.llm.base import Completion, ProviderBase
from seminars.llm.cli import (
    PASSED_THROUGH,
    CliRequest,
    CliResult,
    CliRunner,
    CliStartError,
    CliTimeoutError,
    child_environment,
    installed_version,
    run_cli,
    version_warning,
)
from seminars.llm.failures import (
    failure_for_status,
    failure_for_text,
    is_model_problem,
    provider_failure,
)
from seminars.llm.schema import parse_answer, strict_json_schema

PROVIDER_ID: Final = "claude-cli"
EXECUTABLE: Final = "claude"
TOKEN_ENV: Final = "CLAUDE_CODE_OAUTH_TOKEN"
CONFIG_DIR_ENV: Final = "CLAUDE_CONFIG_DIR"

MAX_BUDGET_USD: Final = "1.00"
"""Upper bound of one request in dollars as the CLI counts them; a runaway stop, not a price."""

QUIET_ENV: Final[dict[str, str]] = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
"""Keeps a request from updating the client or sending telemetry on the side."""

CHILD_ENV_NAMES: Final = frozenset({*PASSED_THROUGH, TOKEN_ENV, CONFIG_DIR_ENV, *QUIET_ENV})
"""Every variable a claude child can get; the module docstring says why each one."""

MODEL_FALLBACK_WARNING: Final = (
    "the configured model is not available to this login; the CLI default is used"
)

_LIMIT_SUBTYPES: Final = frozenset({"error_max_budget_usd", "error_max_turns"})


class _ModelUnavailableError(Exception):
    """The CLI refused the requested model."""


def claude_argv(
    executable: str, *, model: str | None, system: str, schema: Mapping[str, Any]
) -> tuple[str, ...]:
    """The command line of one request; see the module docstring for every flag."""
    selection = ("--model", model) if model else ()
    return (
        executable,
        "-p",
        *selection,
        "--tools",
        "",
        "--strict-mcp-config",
        "--disallowedTools",
        "mcp__*",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--max-budget-usd",
        MAX_BUDGET_USD,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--system-prompt",
        system,
    )


def _result_object(stdout: str) -> dict[str, Any] | None:
    """The result the CLI printed: the whole output, or its last line that is a JSON object."""
    candidates = [stdout, *reversed(stdout.splitlines())]
    for candidate in candidates:
        try:
            document = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(document, dict):
            return document
    return None


def _count(value: object) -> int:
    """A token count as reported, 0 for anything that is not a non-negative integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _cost(value: object) -> float:
    """A cost in dollars as reported, 0 for anything that is not a finite, non-negative number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if math.isfinite(value) and value >= 0 else 0.0


def _usage(document: Mapping[str, Any]) -> tuple[int, int, float]:
    """Input tokens (cached ones included), output tokens and cost of a result."""
    usage = document.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = sum(
        _count(usage.get(name))
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return tokens_in, _count(usage.get("output_tokens")), _cost(document.get("total_cost_usd"))


class ClaudeCliProvider(ProviderBase):
    """``Provider`` over the Claude Code CLI; see the module docstring.

    ``token`` is the value of ``CLAUDE_CODE_OAUTH_TOKEN`` or ``None`` to use
    the login of the client itself. ``env`` is the environment of this
    process, from which only the general variables are passed on.
    ``verified_version`` is the version recorded in config/llm.yaml; another
    installed version gives a warning.
    """

    def __init__(
        self,
        *,
        model: str | None,
        token: str | None,
        timeout: float,
        env: Mapping[str, str],
        verified_version: str | None = None,
        runner: CliRunner = run_cli,
        executable: str = EXECUTABLE,
    ) -> None:
        self._model = model or None
        self._token = (token or "").strip() or None
        self._timeout = timeout
        self._env = dict(env)
        self._verified_version = verified_version
        self._runner = runner
        self._executable = executable
        self._lock = threading.Lock()
        self._version_checked = False
        self._version_note: str | None = None
        self._use_default_model = False

    @property
    def name(self) -> str:
        return PROVIDER_ID

    @property
    def model(self) -> str | None:
        return self._model

    def _child_env(self, scratch: Path | None) -> dict[str, str]:
        """The environment of a child; see the module docstring for every variable.

        ``scratch`` is the temporary directory of a request, ``None`` for
        ``--version``, which needs no login.
        """
        extra = dict(QUIET_ENV)
        if scratch is None:
            return child_environment(self._env, extra)
        if self._token is not None:
            config_dir = scratch / "config"
            config_dir.mkdir()
            extra[TOKEN_ENV] = self._token
            extra[CONFIG_DIR_ENV] = str(config_dir)
        elif self._env.get(CONFIG_DIR_ENV):
            extra[CONFIG_DIR_ENV] = self._env[CONFIG_DIR_ENV]
        return child_environment(self._env, extra)

    def _version_warning(self) -> str | None:
        """Compare the installed version with the verified one, once per provider."""
        with self._lock:
            if not self._version_checked:
                self._version_checked = True
                if self._verified_version is not None:
                    env = self._child_env(None)
                    with tempfile.TemporaryDirectory(prefix="seminars-claude-") as work:
                        found = installed_version(self._runner, self._executable, env, Path(work))
                    self._version_note = version_warning("claude", found, self._verified_version)
            return self._version_note

    def _failure(self, text: str, status: object, returncode: int) -> LLMProviderError:
        detail = f"exit status {returncode}"
        if isinstance(status, int) and not isinstance(status, bool):
            return provider_failure(failure_for_status(status, text), self.name, f"HTTP {status}")
        return provider_failure(failure_for_text(text) or LLMProviderError, self.name, detail)

    def _structured(self, result: CliResult, *, model: str | None) -> dict[str, Any]:
        """The result object of a successful run; the matching error for any other outcome."""
        document = _result_object(result.stdout)
        if document is None:
            if result.returncode == 0:
                raise LLMInvalidOutputError("the CLI printed no result object", provider=self.name)
            raise self._failure(result.stderr, None, result.returncode)
        if document.get("is_error") or document.get("subtype") != "success":
            if document.get("subtype") in _LIMIT_SUBTYPES:
                raise LLMInvalidOutputError(
                    "the request hit the budget or turn limit of the CLI", provider=self.name
                )
            text = f"{document.get('result', '')}\n{result.stderr}"
            if model is not None and is_model_problem(text):
                raise _ModelUnavailableError
            raise self._failure(text, document.get("api_error_status"), result.returncode)
        if document.get("structured_output") is None:
            raise LLMInvalidOutputError("the result has no structured output", provider=self.name)
        return document

    def _attempt(self, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """Run the client once in fresh temporary directories and read its result."""
        model = None if self._use_default_model else self._model
        with tempfile.TemporaryDirectory(prefix="seminars-claude-") as scratch:
            work = Path(scratch) / "work"
            work.mkdir()
            request = CliRequest(
                argv=claude_argv(self._executable, model=model, system=system, schema=schema),
                stdin=user,
                env=self._child_env(Path(scratch)),
                cwd=work,
                timeout=self._timeout,
            )
            try:
                result = self._runner(request)
            except CliTimeoutError:
                detail = f"{self._timeout:g} s"
                raise provider_failure(LLMTimeoutError, self.name, detail) from None
            except CliStartError:
                detail = "the claude command could not be started"
                raise provider_failure(LLMNetworkError, self.name, detail) from None
            return self._structured(result, model=model)

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        """One request. ``max_tokens`` cannot be passed to the CLI; see the module docstring."""
        strict = strict_json_schema(schema)
        note = self._version_warning()
        try:
            document = self._attempt(system, user, strict)
        except _ModelUnavailableError:
            self._use_default_model = True  # for the rest of the run
            document = self._attempt(system, user, strict)
        warnings = [note] if note else []
        if self._use_default_model:
            warnings.append(MODEL_FALLBACK_WARNING)
        tokens_in, tokens_out, cost = _usage(document)
        return Completion(
            value=parse_answer(document["structured_output"], schema, provider=self.name),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=cost,
            warnings=tuple(warnings),
        )
