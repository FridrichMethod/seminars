English | [Chinese](LLM_CREDENTIALS.zh-CN.md)

# LLM credentials

The daily run can use a large language model for two jobs:

* **Field tagging** of the events that the rules cannot decide. The answer is a closed
  vocabulary: the field ids of `config/taxonomy.yaml` and a yes or no for "is this a talk".
* **Reading pages without structure**: sources with `adapter: llm_page` or
  `llm_fallback: true`, whose page gives no feed and no markup to parse. The answer is
  text copied from the page.

Neither job is required. Without any LLM the pipeline runs on its rules and completes
normally. This document says which providers exist, how to set, change and remove their
credentials, how to switch the LLM off, what each route costs, how long each credential
lasts, and how the two command line providers are locked down.

## Providers

| Provider | Billed to | Credential | Runs | Used for |
|---|---|---|---|---|
| `claude-cli` | Claude subscription (Pro, Max, Team, Enterprise) | secret `CLAUDE_CODE_OAUTH_TOKEN`, made by `claude setup-token` | CI and local | tagging and page reading |
| `codex-cli` | ChatGPT subscription credits | the ChatGPT login of the local codex CLI; no secret | local only, opt-in | tagging only |
| `anthropic` | Anthropic API, per token | secret `ANTHROPIC_API_KEY` | CI and local | tagging and page reading |
| `openai` | OpenAI API (Responses API), per token | secret `OPENAI_API_KEY` | CI and local | tagging and page reading |
| `openai-compat` | the service you choose | secret `LLM_API_KEY`, variables `LLM_BASE_URL` and `LLM_MODEL_OPENAI_COMPAT` | CI and local | tagging and page reading |
| `none` | nothing | nothing | everywhere | rules only |

Models are named in `config/llm.yaml`, never in code. A repository variable
`LLM_MODEL_<PROVIDER>` (for example `LLM_MODEL_CLAUDE_CLI`) overrides one. When a
subscription login may not use the configured model, the CLI provider falls back to the
default model of the CLI and the run report shows a warning.

## The provider chain

The providers are tried in this order:

```
claude-cli -> codex-cli -> anthropic -> openai -> openai-compat -> none
```

* Only a provider whose credential is configured joins the chain. `anthropic` and `openai`
  also need a model, `openai-compat` needs its address and its model.
* `codex-cli` joins only when it is named explicitly, and never inside GitHub Actions. In
  CI the chain is therefore `claude-cli -> anthropic -> openai -> openai-compat -> none`.
* The repository variable `LLM_PROVIDERS` (comma separated) replaces the order or selects
  providers, for example `claude-cli,anthropic`. `none` in the list ends it, so
  `LLM_PROVIDERS=none` switches the LLM off.
* The first request of a run plays the role of a probe. When a provider fails with an
  authentication failure, an exhausted quota, a rate limit, a timeout or a network error,
  it is marked unavailable for the rest of the run and the request goes to the next
  provider. When all of them have failed, the run goes on with the rules only.
* An answer that does not match the expected format is discarded. That is not a reason to
  switch providers.
* Every failure is recorded in the health data. When the same provider fails twice in a
  row, an `llm-auth` issue for that provider says which secret to replace and which command
  to run; it is closed when the provider works again.

Credentials are read from the environment, stripped of surrounding whitespace, handed only
to the provider that needs them, and never logged, not even a prefix.

## How GitHub Actions passes credentials

* Only `update.yml` receives LLM secrets, and only in the step that runs `seminars run`:
  `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `LLM_API_KEY` are
  passed as environment variables there (empty when a secret does not exist). `ci.yml` and
  `digest.yml` get no LLM secret.
* `LLM_PROVIDERS`, `LLM_MODEL_<PROVIDER>` and `LLM_BASE_URL` are repository variables, not
  secrets.
* Claude Code is installed only when `CLAUDE_CODE_OAUTH_TOKEN` exists, at exactly the
  version recorded under `cli_versions` in `config/llm.yaml`.
* There is no Codex step in any workflow: Codex is not installed and no Codex login is
  stored as a secret.
* These workflows start only on `schedule` and `workflow_dispatch`, never on pull requests,
  so code from a fork never runs with the secrets. GitHub: "With the exception of
  `GITHUB_TOKEN`, secrets are not passed to the runner when a workflow is triggered from a
  forked repository."

## Setting and changing credentials

Use `scripts/set-llm-credential.sh`. It needs the GitHub CLI (`gh`), logged in with the
right to change the secrets of the repository, and it works on the repository of the
current checkout (or the one given with `--repo OWNER/NAME`).

How the value travels: without `--from-file`, `gh` asks for it and does not show what you
type or paste. With `--from-file PATH` the script removes the line endings of the file and
pipes the rest into `gh secret set` on standard input. The value is never an argument of a
command, never printed and never written anywhere else. A file that the group or others may
read or write is refused (`chmod 600 PATH` fixes that), and so is an empty file.

### claude-cli

1. Create a token: `claude setup-token`. It needs a Pro, Max, Team or Enterprise plan, is
   valid for one year and can only make model requests.
2. Either keep it in a file that only you can read, for example `~/.secrets/claude-token`
   with `chmod 600`, or have it ready to paste.
3. Upload it:

   ```
   scripts/set-llm-credential.sh claude-cli --from-file ~/.secrets/claude-token
   ```

   or, to paste it, `scripts/set-llm-credential.sh claude-cli`.
4. Optionally check it right away: add `--smoke` (see "Checking" below).

After a change of plan, run `claude setup-token` again and then the script again. The code
does not change.

For a local run, export the token in the shell that runs the pipeline:
`CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.secrets/claude-token)" seminars run`.

### anthropic and openai

```
scripts/set-llm-credential.sh anthropic --from-file ~/.secrets/anthropic-key
scripts/set-llm-credential.sh openai --from-file ~/.secrets/openai-key
```

The model comes from `config/llm.yaml`; `--model NAME` sets `LLM_MODEL_ANTHROPIC` or
`LLM_MODEL_OPENAI` instead.

### openai-compat

Any service with an OpenAI style chat completions endpoint:

```
scripts/set-llm-credential.sh openai-compat --from-file ~/.secrets/llm-key \
  --base-url https://llm.example.org/v1 --model <model>
```

This sets the secret `LLM_API_KEY` and the variables `LLM_BASE_URL` and
`LLM_MODEL_OPENAI_COMPAT`. There is no default model for this provider.

### codex-cli (local only)

1. Log in: `codex login` and choose "Sign in with ChatGPT".
2. Check the local state: `scripts/set-llm-credential.sh codex-cli`. It reports whether
   `codex` is installed, whether it is logged in with ChatGPT and whether its version is
   the one recorded in `config/llm.yaml`. It sets no GitHub secret.
3. Enable it for a run, because it is never enabled by itself:

   ```
   LLM_PROVIDERS=claude-cli,codex-cli seminars run
   ```

   or `seminars run --llm codex-cli`. Even then it only tags fields; page reading skips it.

### Removing a secret, choosing the order

* Remove a secret you no longer use: `scripts/set-llm-credential.sh openai --delete`.
* Set the order of the chain: add `--providers claude-cli,anthropic` to any call. Go back
  to the order of `config/llm.yaml` with `gh variable delete LLM_PROVIDERS`.

### Checking

`--smoke` starts the `llm-smoke` mode of `update.yml` after the change, which is the same
as `gh workflow run update.yml -f mode=llm-smoke`. It sends one cheap request to every
configured provider and reports each result; it does not fetch or publish anything. Locally
the same check is `seminars llm-smoke`.

## Switching the LLM off

* In CI: `scripts/set-llm-credential.sh none`, which sets the variable
  `LLM_PROVIDERS=none`. Switch it on again with `gh variable delete LLM_PROVIDERS`, or set
  an order with `--providers`.
* Locally: `seminars run --no-llm`, or `LLM_PROVIDERS=none seminars run`.

Without the LLM, field tags come from the rules alone and sources that need page reading
yield no events until it is back. Nothing else changes.

## What it costs

| Route | How it is paid |
|---|---|
| `claude-cli` | Counts against the usage limits of the Claude subscription; there is no bill per request. The CLI reports an equivalent price: about $0.0004 for one tagging request with the Haiku model (measured on 2026-11-10). |
| `codex-cli` | Uses the credits of the ChatGPT plan. Every request carries about 23k input tokens of fixed overhead (the system prompt and tool definitions of the CLI), dozens of times the size of a `claude-cli` request. |
| `anthropic`, `openai` | Billed per token at the API price of the configured model. The defaults in `config/llm.yaml` cost about $0.10 per million input tokens and $0.50 per million output tokens (price pages, 2026-11). |
| `openai-compat` | Whatever the chosen service charges. |
| `none` | Nothing. |

What keeps the cost down, all set in `config/llm.yaml`:

* `batch_size` (20): events per tagging request, answered as one JSON list.
* `max_calls_per_run` (60): requests in one run; the rest is skipped and reported.
* `max_input_chars` (12000): the longest prompt that is sent; a long page is read in
  pieces of this size.
* The cache in `data/llm_cache/`: the key is the SHA-256 of the prompt version, the task and
  the normalized input. Model and provider are not part of it, so a new key, plan or model
  never repeats a request. An entry that has not been used for `cache_ttl_days` (180) is
  removed.

The run report lists the requests, tokens, cost and the result of every provider.

## How long credentials last

* `CLAUDE_CODE_OAUTH_TOKEN`: one year from `claude setup-token`. When it expires or is
  revoked, `claude-cli` fails with an authentication failure, which shows up as an
  `llm-auth` issue after the second failure. Create a new token and run the script again; a reminder
  about eleven months after creating it avoids the gap.
* API keys: until they are revoked. An exhausted balance shows up the same way, as a quota
  failure and an `llm-auth` issue.
* The ChatGPT login of `codex-cli` is renewed by the CLI on the local machine. When it has
  lapsed, run `codex login` again.

## How the two command line providers are locked down

The input is text from web pages, and both CLIs are agents that can use tools. Both are
run the same way:

* the command is a list of arguments and never goes through a shell;
* the prompt with the page text goes to standard input, never into an argument;
* the environment holds only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR` and what the
  provider itself needs, so no other credential is visible to the CLI;
* the working directory is a new, empty temporary directory;
* a timeout (`timeout_seconds`, 120 s) ends the CLI and everything it started;
* only the final JSON on standard output is read, and the answer must match the schema;
* standard error is read only to tell an expired credential from a rate limit and is then
  dropped: it can name the account, so it never reaches a log, a report or an error message.

### claude-cli: Claude Code 2.1.322

```
claude -p --model <model> \
  --tools "" --strict-mcp-config --disallowedTools "mcp__*" \
  --setting-sources "" --disable-slash-commands \
  --permission-prompts none --no-session-persistence \
  --max-budget-usd 1.00 \
  --output-format json --json-schema '<schema>' \
  --system-prompt '<system prompt>' < prompt
```

* `--tools ""` switches every built-in tool off. It does not cover MCP tools, hence
  `--strict-mcp-config` (no MCP configuration is loaded) and `--disallowedTools "mcp__*"`.
* `--setting-sources ""` reads no user, project or local settings, and
  `--disable-slash-commands` loads no skills or commands.
* `--permission-prompts none` denies anything that would ask for a permission.
* `--no-session-persistence` keeps no transcript of the page text on disk.
* `--max-budget-usd` and the timeout bound one request; version 2.1.322 has no
  `--max-turns`.
* `--bare` is not used: bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`.
* With a token, `CLAUDE_CONFIG_DIR` points to a temporary directory that is removed after
  the request. `DISABLE_AUTOUPDATER=1` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` keep
  the CLI from updating itself or sending anything on the side.
* An answer counts only when `is_error` is false, `subtype` is `success` and
  `structured_output` is present.

### codex-cli: Codex CLI 0.167.0

```
codex exec --sandbox read-only --skip-git-repo-check --ephemeral \
  --ignore-user-config --ignore-rules \
  --disable shell_tool --disable unified_exec --disable apps --disable plugins \
  --disable multi_agent --disable image_generation --disable view_image \
  --disable goals --disable sleep_tool --disable memories --disable browser_use \
  --disable computer_use --disable in_app_browser --disable skill_search \
  --disable tool_suggest --disable hooks --disable remote_plugin \
  -c 'web_search="disabled"' \
  --color never --json -C <empty temporary directory> \
  --output-schema <schema.json> -o <answer.json> -m <model> - < prompt
```

* Without the `--disable` flags the model is offered about 26 tools, among them a shell, a
  patch tool, web access, sub-agents, MCP and plugin tools.
* With them it still has `functions.exec` and `functions.wait`,
  `functions.request_user_input` and the `collaboration.*` tools. There is no documented
  switch for "no tools", and the features `code_mode_host` and `collaboration_modes` do not
  remove the rest.
* `web_search` has to be switched off explicitly: its default is `"cached"`, and the
  sandbox does not cover web search, apps and MCP. The read-only sandbox applies to shell
  commands, which can read the disk but cannot write or reach the network.
* `--ignore-user-config` skips `config.toml` only. The login is still read from
  `CODEX_HOME`, which is left as it is.
* `--json` prints the events of the run; they are read for token counts, error messages and
  tool calls. The answer is the file written by `-o`.

**Residual risk.** The command runs as you, on your machine, and can read what you can
read, including the login file of the codex CLI (`$CODEX_HOME/auth.json`). A hostile page
could make the model call one of the remaining tools to read a local file. What stands
between that and a leak: the sandbox has no network; the answer must be an enumeration
(field ids, numbers, yes or no), so it has no room to carry text out, and requests with
free text are refused before anything is sent; an answer whose event stream shows any tool
call is discarded; and everything is checked by the secret scan before it is written to
`data/` or `public/`. That is why `codex-cli` only tags fields, never reads pages, and is
never enabled unless you name it.

### Why codex-cli does not run in GitHub Actions

OpenAI's documentation on authenticating Codex in CI says: "The right way to authenticate
automation is with an API key." and "Do not use this workflow for public or open-source
repositories.", for a runner that is "trusted private infrastructure". This project is a
public repository on GitHub's hosted runners, exactly the case it warns against. In
addition:

* the CLI cannot be run without tools, and the remaining tools can read the runner's disk,
  the login file included;
* every request carries about 23k tokens of overhead;
* the ChatGPT account may belong to a managed workspace whose rules do not allow its login
  in public automation;
* the sandbox needs extra set-up on Ubuntu 24.04 runners (unprivileged user namespaces and
  AppArmor);
* the login is refreshed after about eight days and the refresh rotates it, so CI would have
  to write it back into the repository secrets after every run, which needs a long-lived
  token that can rewrite any secret of the repository, and one login could not serve two
  jobs at the same time.

GPT models in CI therefore go through an API key (`openai` or `openai-compat`).

## Page text is data

Whatever the provider, a page cannot steer the result:

* The page text reaches the model between two marker lines that are derived from the text
  itself, so a page cannot spell them, and the system prompt says that the text is data and
  that instructions in it are never followed.
* The answer must validate against its schema. An answer with an extra or mistyped field is
  discarded as a whole; tagging answers can only hold the field ids of the taxonomy.
* Every text value of a page reading (title, speaker, affiliation, series, location,
  abstract) must occur in the page text after normalization, or differ from it by a few
  characters at most (`grounding_min_similarity`, 90). Dates and times must show up in the
  text as well. Anything else is dropped.
* The model never writes a URL. Links are numbered markers in the page text, and an answer
  names a marker; only links that are really on the page can come out.
* Before anything is written to `data/` or `public/`, a secret scan looks for API keys,
  OAuth tokens, JWTs and similar strings. A finding stops the run: nothing is committed or
  deployed.
