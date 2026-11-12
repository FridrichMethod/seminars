"""The ``codex-cli`` backend: ``codex exec``, paid by the ChatGPT subscription.

Local only. It is never used inside GitHub Actions, it joins the provider
chain only when LLM_PROVIDERS (or ``--llm``) names it, and it only answers
requests whose answers are closed vocabularies (field tagging). The reason
for all three is the same: this client cannot be run without tools.

Verified with Codex CLI 0.167.0 (recorded in config/llm.yaml). The command,
with the prompt on standard input::

    codex exec --sandbox read-only --skip-git-repo-check --ephemeral \\
      --ignore-user-config --ignore-rules \\
      --disable shell_tool --disable unified_exec --disable apps --disable plugins \\
      --disable multi_agent --disable image_generation --disable view_image \\
      --disable goals --disable sleep_tool --disable memories --disable browser_use \\
      --disable computer_use --disable in_app_browser --disable skill_search \\
      --disable tool_suggest --disable hooks --disable remote_plugin \\
      -c 'web_search="disabled"' \\
      --color never --json -C <empty temporary directory> \\
      --output-schema <schema.json> -o <answer.json> -m <model> -

What is known about it:

* Without the ``--disable`` flags the model is offered a shell, a patch
  tool, web access, sub-agents, MCP and plugin tools. With them it still
  has ``functions.exec`` and ``functions.wait`` (a JavaScript host for
  nested tools), ``functions.request_user_input`` and the
  ``collaboration.*`` tools. There is no documented switch for "no tools",
  and ``code_mode_host`` and ``collaboration_modes`` do not remove the
  rest (checked with ``codex debug prompt-input``, which renders the tool
  list without a model call).
* The ``--disable`` list is longer than it has to be today, on purpose.
  Leaving out one flag at a time (``codex debug prompt-input``, which
  reads the user configuration, unlike the real call) showed that with
  0.167.0 only ``shell_tool``, ``view_image`` and ``sleep_tool`` change
  the tools on offer (``exec_command`` and ``view_image`` as nested tools
  of ``functions.exec``, ``clock.sleep``), and ``apps`` and ``plugins``
  change the instructions; the other twelve change nothing. They stay as
  defence in depth: a feature that is off by default now can be on by
  default in a later version, and a flag too many costs nothing.
* ``web_search`` has to be switched off explicitly: its default is
  ``cached``, and the sandbox does not cover web search, apps and MCP.
* The read-only sandbox applies to shell commands: they can read the disk
  but cannot write or reach the network.
* ``--ignore-user-config`` skips ``config.toml`` only; the login is still
  read from ``CODEX_HOME``, which is left as it is. The command therefore
  runs as the owner of the machine and can read what the owner can read.

What remains, and what is done about it: a hostile input could make the
model call one of the remaining tools. Three things keep that from
mattering. The answer must be an enumeration (``serves`` and ``complete``
refuse every schema with free text, so there is no room to carry anything
out). Any tool call that shows up in the event stream discards the answer.
And nothing this backend returns is ever page text, so the grounding rule
of the page extractor is not needed here and the extractor never uses it.

``--json`` makes the client print its events as JSON lines. They are read
for the token usage, for error messages (to classify a failure) and for
tool calls; the answer itself is the file written by ``-o``.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from seminars.config import ENV_GITHUB_ACTIONS
from seminars.contracts import (
    LLMAuthError,
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProviderError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from seminars.llm.base import Completion, ProviderBase
from seminars.llm.cli import (
    VERSION_TIMEOUT_SECONDS,
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
from seminars.llm.failures import failure_for_text, is_model_problem, provider_failure
from seminars.llm.schema import is_closed_vocabulary, parse_answer, strict_json_schema
from seminars.textio import read_text, write_text_atomic

PROVIDER_ID: Final = "codex-cli"
EXECUTABLE: Final = "codex"
CODEX_HOME_ENV: Final = "CODEX_HOME"

DISABLED_FEATURES: Final = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "image_generation",
    "view_image",
    "goals",
    "sleep_tool",
    "memories",
    "browser_use",
    "computer_use",
    "in_app_browser",
    "skill_search",
    "tool_suggest",
    "hooks",
    "remote_plugin",
)
"""Feature flags switched off for every request; see the module docstring."""

MODEL_FALLBACK_WARNING: Final = (
    "the configured model is not available to this login; the CLI default is used"
)

_HARMLESS_ITEMS: Final = frozenset({"agent_message", "reasoning", "error"})
"""Items of the event stream that are not tool calls."""

_ITEM_NAME: Final = re.compile(r"[a-z_]{1,40}")
_CHATGPT_LOGIN: Final = re.compile(r"logged in using chatgpt", re.IGNORECASE)


class _ModelUnavailableError(Exception):
    """The CLI refused the requested model."""


def codex_argv(
    executable: str, *, model: str | None, workdir: Path, schema_file: Path, answer_file: Path
) -> tuple[str, ...]:
    """The command line of one request; see the module docstring for every flag."""
    disabled = tuple(part for feature in DISABLED_FEATURES for part in ("--disable", feature))
    selection = ("-m", model) if model else ()
    return (
        executable,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        *disabled,
        "-c",
        'web_search="disabled"',
        "--color",
        "never",
        "--json",
        "-C",
        str(workdir),
        "--output-schema",
        str(schema_file),
        "-o",
        str(answer_file),
        *selection,
        "-",
    )


def _events(stdout: str) -> Iterator[dict[str, Any]]:
    """The JSON objects among the lines of ``stdout``; other lines are skipped."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class _Stream:
    """What the event stream of one run says: usage, error messages and tool calls."""

    def __init__(self, stdout: str) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.failed = False
        self.messages: list[str] = []
        self.tools: list[str] = []
        for event in _events(stdout):
            self._read(event)

    def _read(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.input_tokens += _count(usage.get("input_tokens"))
            self.output_tokens += _count(usage.get("output_tokens"))
        if kind in ("error", "turn.failed"):
            self.failed = True
            error = event.get("error")
            detail = error.get("message") if isinstance(error, dict) else event.get("message")
            self.messages.append(str(detail or ""))
        item = event.get("item")
        if isinstance(kind, str) and kind.startswith("item.") and isinstance(item, dict):
            item_type = str(item.get("type", ""))
            if item_type == "error":
                self.messages.append(str(item.get("message", "")))
            elif item_type not in _HARMLESS_ITEMS and item_type not in self.tools:
                self.tools.append(item_type)

    def tool_names(self) -> str:
        """The kinds of tool call seen, as far as they are plain names of the client."""
        names = [name for name in self.tools if _ITEM_NAME.fullmatch(name)]
        return ", ".join(names) or "of an unknown kind"


class CodexCliProvider(ProviderBase):
    """``Provider`` over the Codex CLI; see the module docstring.

    ``env`` is the environment of this process; the child gets its general
    variables and ``CODEX_HOME`` when that is set. ``verified_version`` is
    the version recorded in config/llm.yaml; another installed version
    gives a warning.
    """

    def __init__(
        self,
        *,
        model: str | None,
        timeout: float,
        env: Mapping[str, str],
        verified_version: str | None = None,
        runner: CliRunner = run_cli,
        executable: str = EXECUTABLE,
    ) -> None:
        self._model = model or None
        self._timeout = timeout
        self._env = dict(env)
        self._verified_version = verified_version
        self._runner = runner
        self._executable = executable
        self._lock = threading.Lock()
        self._ready = False
        self._version_note: str | None = None
        self._use_default_model = False

    @property
    def name(self) -> str:
        return PROVIDER_ID

    @property
    def model(self) -> str | None:
        return self._model

    def serves(self, schema: type[BaseModel]) -> bool:
        """Only schemas whose answers are closed vocabularies; never free text."""
        return is_closed_vocabulary(strict_json_schema(schema))

    def _child_env(self) -> dict[str, str]:
        home = self._env.get(CODEX_HOME_ENV)
        return child_environment(self._env, {CODEX_HOME_ENV: home} if home else {})

    def _run(self, request: CliRequest) -> CliResult:
        try:
            return self._runner(request)
        except CliTimeoutError:
            detail = f"{request.timeout:g} s"
            raise provider_failure(LLMTimeoutError, self.name, detail) from None
        except CliStartError:
            detail = "the codex command could not be started"
            raise provider_failure(LLMNetworkError, self.name, detail) from None

    def _prepare(self) -> str | None:
        """Before the first request: refuse CI, require the ChatGPT login, note the version."""
        if self._env.get(ENV_GITHUB_ACTIONS, "").strip().lower() == "true":
            detail = "this provider is never used inside GitHub Actions"
            raise provider_failure(LLMProviderError, self.name, detail)
        with self._lock:
            if self._ready:
                return self._version_note
            env = self._child_env()
            with tempfile.TemporaryDirectory(prefix="seminars-codex-") as work:
                status = self._run(
                    CliRequest(
                        argv=(self._executable, "login", "status"),
                        stdin="",
                        env=env,
                        cwd=Path(work),
                        timeout=VERSION_TIMEOUT_SECONDS,
                    )
                )
                if _CHATGPT_LOGIN.search(f"{status.stdout}\n{status.stderr}") is None:
                    detail = "no ChatGPT login was found; run: codex login"
                    raise provider_failure(LLMAuthError, self.name, detail)
                if self._verified_version is not None:
                    found = installed_version(self._runner, self._executable, env, Path(work))
                    self._version_note = version_warning("codex", found, self._verified_version)
            self._ready = True
            return self._version_note

    def _attempt(self, prompt: str, schema: Mapping[str, Any]) -> tuple[str, _Stream]:
        """Run the client once in fresh temporary directories; the answer text and the stream."""
        model = None if self._use_default_model else self._model
        with tempfile.TemporaryDirectory(prefix="seminars-codex-") as scratch:
            work, files = Path(scratch) / "work", Path(scratch) / "io"
            work.mkdir()
            schema_file, answer_file = files / "schema.json", files / "answer.json"
            write_text_atomic(schema_file, json.dumps(schema) + "\n")
            request = CliRequest(
                argv=codex_argv(
                    self._executable,
                    model=model,
                    workdir=work,
                    schema_file=schema_file,
                    answer_file=answer_file,
                ),
                stdin=prompt,
                env=self._child_env(),
                cwd=work,
                timeout=self._timeout,
            )
            result = self._run(request)
            stream = _Stream(result.stdout)
            if result.returncode != 0 or stream.failed:
                text = "\n".join([*stream.messages, result.stderr])
                if model is not None and is_model_problem(text):
                    raise _ModelUnavailableError
                detail = f"exit status {result.returncode}"
                kind = failure_for_text(text) or LLMProviderError
                raise provider_failure(kind, self.name, detail)
            try:
                return read_text(answer_file), stream
            except (OSError, ValueError):
                message = "the CLI wrote no answer"
                raise LLMInvalidOutputError(message, provider=self.name) from None

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        """One request. ``max_tokens`` cannot be passed to the CLI."""
        strict = strict_json_schema(schema)
        if not is_closed_vocabulary(strict):
            raise LLMUnavailableError(
                "this provider only answers closed vocabularies, never free text",
                provider=self.name,
            )
        note = self._prepare()
        prompt = f"{system}\n\n{user}"
        try:
            answer, stream = self._attempt(prompt, strict)
        except _ModelUnavailableError:
            self._use_default_model = True  # for the rest of the run
            answer, stream = self._attempt(prompt, strict)
        if stream.tools:
            raise LLMInvalidOutputError(
                f"the model used a tool ({stream.tool_names()}); the answer is discarded",
                provider=self.name,
            )
        warnings = [note] if note else []
        if self._use_default_model:
            warnings.append(MODEL_FALLBACK_WARNING)
        return Completion(
            value=parse_answer(answer, schema, provider=self.name),
            input_tokens=stream.input_tokens,
            output_tokens=stream.output_tokens,
            warnings=tuple(warnings),
        )
