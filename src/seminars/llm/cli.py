"""Running a command line client as a child process, the way both CLI backends do it.

The input of an LLM request is text from web pages, and the two clients are
agents with tools. Whatever the flags of a backend switch off, the process
itself is started with as little as possible:

* the command is a list of arguments and never goes through a shell;
* the prompt is written to the standard input of the child, so page text is
  never part of a command line (which other users of the machine can read);
* the environment holds only what the client needs (``child_environment``),
  not the environment of this process, which has every other credential;
* the working directory is an empty temporary directory;
* a timeout ends the child and everything it started;
* the standard error output is returned to the backend, which reads it to
  tell an expired credential from a rate limit and then drops it. It can
  name the account, so it is never logged, reported or put into an error.

The environment of a child
--------------------------

It is an allow-list. Of the environment of this process a child gets only
the variables of ``PASSED_THROUGH``, and only those that are set and not
empty:

* PATH, to find the client and whatever it starts (a client installed
  with npm can be a script that runs on ``node``);
* HOME, the home directory, where a client keeps its login and its
  settings unless a variable of its own points elsewhere;
* LANG and LC_ALL, the locale, so that the page text on standard input
  and the answer on standard output are read and written as UTF-8;
* TMPDIR, the place for temporary files.

None of them holds a credential. Each backend adds the few variables of its
own, listed with the reason for each in its module docstring (``claude_cli``
and ``codex_cli``, each as ``CHILD_ENV_NAMES``), and nothing else reaches the
child. In particular, none of these:

* GITHUB_TOKEN and GH_TOKEN: the update workflow hands GITHUB_TOKEN to
  ``seminars run``, which needs it for the health issues, and a client has
  no business with it;
* ANTHROPIC_API_KEY, OPENAI_API_KEY and LLM_API_KEY, the credentials of the
  API providers: with an API key in its environment a subscription client
  bills the API instead of the subscription;
* what sends a client elsewhere or lets someone else read its traffic:
  ANTHROPIC_BASE_URL, OPENAI_BASE_URL, HTTPS_PROXY and the other proxy
  variables (a proxy address can hold a password), SSL_CERT_FILE and
  NODE_EXTRA_CA_CERTS (other certificate authorities). A network that needs
  a proxy or its own certificate authority cannot use the CLI providers,
  just as the HTTP client of this project ignores those variables;
* NODE_OPTIONS, which can load code into a program that runs on node, and
  the sockets of agents and key rings, such as SSH_AUTH_SOCK and
  DBUS_SESSION_BUS_ADDRESS.

tests/test_llm_cli_env.py starts both backends from an environment that
holds all of these and compares what each child gets with these lists.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

PASSED_THROUGH: Final = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
"""Variables of the parent environment that every child gets, when they are set.

The only ones; see "The environment of a child" in the module docstring.
"""

VERSION_TIMEOUT_SECONDS: Final = 20.0
"""Limit for the quick local commands (``--version``, ``login status``)."""

_VERSION: Final = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")


class CliTimeoutError(Exception):
    """The child did not finish within the timeout and was ended."""


class CliStartError(Exception):
    """The child could not be started (command not found, not executable)."""


@dataclass(frozen=True)
class CliRequest:
    """One run of a command: arguments, standard input, environment, directory, time limit.

    The environment can hold a credential (CLAUDE_CODE_OAUTH_TOKEN), so the
    ``repr`` of a request names its variables and never shows their values.
    """

    argv: tuple[str, ...]
    stdin: str
    env: Mapping[str, str] = field(repr=False)
    cwd: Path
    timeout: float

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.env))
        return (
            f"CliRequest(argv={self.argv!r}, env names=[{names}], cwd={self.cwd!r}, "
            f"timeout={self.timeout!r})"
        )


@dataclass(frozen=True)
class CliResult:
    """What a finished child left behind. ``stderr`` is for classification only.

    The standard error output can name the account, and the standard output
    of the claude CLI carries details of the login (when it expires, the id
    of the session) next to the answer. The ``repr`` of a result, which a
    traceback or a failed assertion prints, therefore gives only the length
    of either.
    """

    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"CliResult(returncode={self.returncode!r}, stdout=<{len(self.stdout)} characters>, "
            f"stderr=<{len(self.stderr)} characters>)"
        )


CliRunner = Callable[[CliRequest], CliResult]
"""Runs a ``CliRequest``; raises ``CliTimeoutError`` or ``CliStartError``. Tests pass a fake."""


def child_environment(parent: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """The environment of a child: the general variables of ``parent`` plus ``extra``.

    Nothing else of the parent environment is passed on, in particular no
    credential of another provider and no GitHub token (see the module
    docstring). ``extra`` is what the backend itself sets.
    """
    env = {name: parent[name] for name in PASSED_THROUGH if parent.get(name)}
    env.update(extra)
    return env


def end_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and, where the platform has process groups, all of its children."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def run_cli(request: CliRequest) -> CliResult:
    """Run the command and return its exit status and output.

    The child is the leader of a new session, so the timeout ends its whole
    process group. Output that is not UTF-8 is decoded with replacement
    characters. Raises ``CliStartError`` when the command cannot be started
    and ``CliTimeoutError`` when it runs longer than ``request.timeout``
    seconds.
    """
    try:
        process = subprocess.Popen(
            list(request.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(request.env),
            cwd=request.cwd,
            shell=False,
            start_new_session=True,
        )
    except OSError as error:
        raise CliStartError(type(error).__name__) from None
    try:
        stdout, stderr = process.communicate(
            request.stdin.encode("utf-8", "replace"), timeout=request.timeout
        )
    except subprocess.TimeoutExpired:
        end_process_tree(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=5)
        raise CliTimeoutError from None
    except BaseException:
        end_process_tree(process)
        raise
    return CliResult(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


def parse_version(text: str) -> str | None:
    """The first ``x.y.z`` version number in ``text`` (the output of ``--version``)."""
    match = _VERSION.search(text)
    return match.group() if match else None


def installed_version(
    runner: CliRunner, executable: str, env: Mapping[str, str], cwd: Path
) -> str | None:
    """The version ``executable --version`` reports; ``None`` when it cannot be found out."""
    request = CliRequest(
        argv=(executable, "--version"), stdin="", env=env, cwd=cwd, timeout=VERSION_TIMEOUT_SECONDS
    )
    try:
        result = runner(request)
    except (CliStartError, CliTimeoutError):
        return None
    return parse_version(result.stdout) if result.returncode == 0 else None


def version_warning(tool: str, installed: str | None, verified: str | None) -> str | None:
    """A note for the run report when the installed client is not the verified version.

    The flags that switch the tools of a client off change between
    versions, so a different version is worth a look; it is not an error.
    """
    if verified is None:
        return None
    if installed is None:
        return f"the version of the {tool} CLI could not be determined (verified: {verified})"
    if installed != verified:
        return f"{tool} CLI version {installed} differs from the verified version {verified}"
    return None
