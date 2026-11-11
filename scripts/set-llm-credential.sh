#!/usr/bin/env bash
# Set, replace or remove the LLM credential of one provider for the GitHub Actions runs.
#
#   scripts/set-llm-credential.sh <provider> [options]
#
# provider: claude-cli | codex-cli | anthropic | openai | openai-compat | none
# Run with --help for the options. Full guide: docs/LLM_CREDENTIALS.md.
#
# How a value travels: from your terminal (gh asks for it and does not show it) or from a
# file (--from-file) through a pipe into "gh secret set" on standard input. It is never an
# argument of any command, never printed, and never stored anywhere else. A file that
# anyone but its owner may read or write is refused.

set -euo pipefail
umask 077

readonly WORKFLOW="update.yml"
readonly PROVIDERS="claude-cli codex-cli anthropic openai openai-compat none"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT
readonly LLM_CONFIG="${ROOT}/config/llm.yaml"

usage() {
    cat <<'EOF'
Usage: scripts/set-llm-credential.sh <provider> [options]

Providers and what is set for them:
  claude-cli      secret CLAUDE_CODE_OAUTH_TOKEN (create it first: claude setup-token)
  anthropic       secret ANTHROPIC_API_KEY
  openai          secret OPENAI_API_KEY
  openai-compat   secret LLM_API_KEY (and, with the options below, LLM_BASE_URL and the model)
  codex-cli       nothing: local use only. Checks the codex CLI on this machine.
  none            variable LLM_PROVIDERS=none: the daily run goes on without any LLM.

Options:
  --from-file PATH   read the value from PATH (newlines are removed). The file must not be
                     readable or writable by group or others (chmod 600 PATH).
  --delete           remove the secret of the provider instead of setting it.
  --providers LIST   also set the variable LLM_PROVIDERS, e.g. claude-cli,anthropic.
  --model NAME       also set the variable LLM_MODEL_<PROVIDER> (the model of the provider).
  --base-url URL     openai-compat only: also set the variable LLM_BASE_URL (https://...).
  --repo OWNER/NAME  the repository; default: the one gh finds for this checkout.
  --smoke            afterwards start the llm-smoke mode of update.yml (one probe per provider).
  -h, --help         show this help.

Without --from-file, gh asks for the value and does not show what you type or paste.
EOF
}

fail() {
    printf 'set-llm-credential: %s\n' "$1" >&2
    exit 2
}

note() {
    printf '%s\n' "$1"
}

secret_name() {
    case "$1" in
        claude-cli) printf 'CLAUDE_CODE_OAUTH_TOKEN' ;;
        anthropic) printf 'ANTHROPIC_API_KEY' ;;
        openai) printf 'OPENAI_API_KEY' ;;
        openai-compat) printf 'LLM_API_KEY' ;;
        *) printf '' ;;
    esac
}

model_variable() {
    local upper
    upper="$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
    printf 'LLM_MODEL_%s' "$upper"
}

is_known_provider() {
    local name
    for name in $PROVIDERS; do
        [[ "$name" == "$1" ]] && return 0
    done
    return 1
}

check_plain_value() {
    # Values of repository variables: printable ASCII without spaces, at most 200 characters.
    local what="$1" value="$2"
    [[ -n "$value" ]] || fail "$what must not be empty"
    ((${#value} <= 200)) || fail "$what is too long"
    if [[ "$value" =~ [^!-~] ]]; then
        fail "$what may only hold printable ASCII characters without spaces"
    fi
}

check_provider_list() {
    local list="$1" item
    local -a items
    check_plain_value "--providers" "$list"
    IFS=',' read -r -a items <<<"$list"
    for item in "${items[@]}"; do
        is_known_provider "$item" || fail "unknown provider in --providers: $item"
    done
}

file_mode() {
    # Permission bits of the file a path points to, in octal (GNU stat, then BSD stat).
    stat -L -c '%a' "$1" 2>/dev/null || stat -L -f '%Lp' "$1" 2>/dev/null
}

check_secret_file() {
    local path="$1" mode
    [[ -e "$path" ]] || fail "no such file: $path"
    [[ -f "$path" ]] || fail "not a regular file: $path"
    [[ -r "$path" ]] || fail "cannot read: $path"
    mode="$(file_mode "$path")" || fail "cannot read the permissions of $path"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] || fail "cannot read the permissions of $path"
    if (((8#$mode & 8#077) != 0)); then
        fail "refusing $path: group or others may access it (mode $mode); run: chmod 600 $path"
    fi
    if [[ "$(tr -d '\r\n\t ' <"$path" | head -c 1 | wc -c | tr -d ' ')" == "0" ]]; then
        fail "the file is empty: $path"
    fi
}

gh_cmd() {
    gh "$@" ${REPO:+--repo "$REPO"}
}

require_gh() {
    command -v gh >/dev/null 2>&1 || fail "the GitHub CLI (gh) is not installed"
    gh auth status >/dev/null 2>&1 || fail "gh is not logged in; run: gh auth login"
}

set_secret() {
    local name="$1"
    if [[ -n "$FROM_FILE" ]]; then
        tr -d '\r\n' <"$FROM_FILE" | gh_cmd secret set "$name"
    else
        [[ -t 0 ]] || fail "no terminal to type the value in; use --from-file PATH"
        note "gh asks for the value of $name now; what you paste is not shown."
        gh_cmd secret set "$name"
    fi
    note "Secret $name is set."
}

set_variable() {
    gh_cmd variable set "$1" --body "$2"
    note "Variable $1 is set to $2."
}

recorded_codex_version() {
    sed -n '/^cli_versions:/,/^[^ #]/p' "$LLM_CONFIG" 2>/dev/null |
        sed -n 's/^ *codex-cli: *"\{0,1\}\([0-9][0-9.]*\)"\{0,1\}.*/\1/p' | head -n 1
}

check_codex() {
    # The codex CLI is never used in GitHub Actions, so there is no secret to set. This only
    # reports the local state; nothing the CLI prints is shown, as it can name the account.
    local status=0 installed recorded
    if ! command -v codex >/dev/null 2>&1; then
        note "codex: not installed (npm install -g @openai/codex)"
        return 1
    fi
    note "codex: installed"
    if codex login status 2>&1 | grep -qi 'logged in using chatgpt'; then
        note "login: ChatGPT"
    else
        note "login: no ChatGPT login found; run: codex login (choose Sign in with ChatGPT)"
        status=1
    fi
    installed="$(codex --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
    recorded="$(recorded_codex_version || true)"
    if [[ -z "$installed" ]]; then
        note "version: unknown (verified: ${recorded:-none recorded})"
    elif [[ -n "$recorded" && "$installed" != "$recorded" ]]; then
        note "version: $installed differs from the verified $recorded in config/llm.yaml"
    else
        note "version: $installed"
    fi
    note "To use it locally (field tagging only): LLM_PROVIDERS=claude-cli,codex-cli seminars run"
    note "No GitHub secret is set for codex-cli: it is never used inside GitHub Actions."
    return "$status"
}

PROVIDER=""
FROM_FILE=""
DELETE=0
SET_PROVIDERS=""
MODEL=""
BASE_URL=""
REPO=""
SMOKE=0

while (($# > 0)); do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --from-file)
            (($# >= 2)) || fail "--from-file needs a path"
            FROM_FILE="$2"
            shift 2
            ;;
        --delete)
            DELETE=1
            shift
            ;;
        --providers)
            (($# >= 2)) || fail "--providers needs a list"
            SET_PROVIDERS="$2"
            shift 2
            ;;
        --model)
            (($# >= 2)) || fail "--model needs a name"
            MODEL="$2"
            shift 2
            ;;
        --base-url)
            (($# >= 2)) || fail "--base-url needs a URL"
            BASE_URL="$2"
            shift 2
            ;;
        --repo)
            (($# >= 2)) || fail "--repo needs OWNER/NAME"
            REPO="$2"
            shift 2
            ;;
        --smoke)
            SMOKE=1
            shift
            ;;
        -*)
            fail "unknown option: $1 (see --help)"
            ;;
        *)
            [[ -z "$PROVIDER" ]] || fail "only one provider at a time"
            PROVIDER="$1"
            shift
            ;;
    esac
done

[[ -n "$PROVIDER" ]] || {
    usage >&2
    exit 2
}
is_known_provider "$PROVIDER" || fail "unknown provider: $PROVIDER (see --help)"
SECRET="$(secret_name "$PROVIDER")"

if [[ -z "$SECRET" ]]; then
    [[ -z "$FROM_FILE" ]] || fail "$PROVIDER has no secret; --from-file does not apply"
    ((DELETE == 0)) || fail "$PROVIDER has no secret; --delete does not apply"
    [[ -z "$MODEL" ]] || fail "$PROVIDER takes no --model"
    [[ -z "$SET_PROVIDERS" ]] || fail "$PROVIDER takes no --providers"
fi
[[ "$PROVIDER" != "codex-cli" ]] || ((SMOKE == 0)) || fail "codex-cli takes no --smoke"
[[ -z "$BASE_URL" || "$PROVIDER" == "openai-compat" ]] || fail "--base-url is for openai-compat"
if [[ -n "$BASE_URL" ]]; then
    check_plain_value "--base-url" "$BASE_URL"
    [[ "$BASE_URL" == https://* ]] || fail "--base-url must start with https://"
fi
[[ -z "$MODEL" ]] || check_plain_value "--model" "$MODEL"
[[ -z "$SET_PROVIDERS" ]] || check_provider_list "$SET_PROVIDERS"
[[ -z "$REPO" ]] || [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "--repo must be OWNER/NAME"
((DELETE == 0)) || [[ -z "$FROM_FILE" ]] || fail "--delete and --from-file do not go together"
[[ -z "$FROM_FILE" ]] || check_secret_file "$FROM_FILE"

if [[ "$PROVIDER" == "codex-cli" ]]; then
    check_codex || exit 1
    exit 0
fi

require_gh

if [[ "$PROVIDER" == "none" ]]; then
    set_variable LLM_PROVIDERS none
    note "The LLM is off for the next runs. To turn it on again: gh variable delete LLM_PROVIDERS"
elif [[ -n "$SECRET" ]] && ((DELETE == 1)); then
    gh_cmd secret delete "$SECRET"
    note "Secret $SECRET is removed."
elif [[ -n "$SECRET" ]]; then
    if [[ "$PROVIDER" == "claude-cli" && -z "$FROM_FILE" ]]; then
        note "Create the token first in another terminal: claude setup-token"
        note "(it needs a Pro, Max, Team or Enterprise plan and is valid for one year)."
    fi
    set_secret "$SECRET"
fi

[[ -z "$BASE_URL" ]] || set_variable LLM_BASE_URL "$BASE_URL"
[[ -z "$MODEL" ]] || set_variable "$(model_variable "$PROVIDER")" "$MODEL"
[[ -z "$SET_PROVIDERS" ]] || set_variable LLM_PROVIDERS "$SET_PROVIDERS"

if ((SMOKE == 1)); then
    gh_cmd workflow run "$WORKFLOW" -f mode=llm-smoke
    note "Started the llm-smoke run of $WORKFLOW; see the Actions tab for the result."
fi
