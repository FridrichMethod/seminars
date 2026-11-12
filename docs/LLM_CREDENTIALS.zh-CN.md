[English](LLM_CREDENTIALS.md) | 中文

# LLM 凭证

每天的运行可以用大语言模型做两件事：

* **领域打标签**：给规则判断不了的事件打标签。回答是封闭词表：`config/taxonomy.yaml` 里的领域 id，以及"这是不是一场学术报告"的是或否。
* **读没有结构的页面**：`adapter: llm_page` 或 `llm_fallback: true` 的源，页面既没有 feed 也没有可解析的标记。回答是从页面里抄出来的文字。

两件事都不是必需的。没有任何 LLM 时，pipeline 只用规则，照常跑完。本文说明有哪些 provider、怎么设置、更换和删除它们的凭证、怎么关掉 LLM、每条路线怎么计费、每种凭证的有效期，以及两个命令行 provider 是怎么锁住的。

## Provider 一览

| Provider | 计费 | 凭证 | 在哪里运行 | 用途 |
|---|---|---|---|---|
| `claude-cli` | Claude 订阅（Pro、Max、Team、Enterprise） | secret `CLAUDE_CODE_OAUTH_TOKEN`，由 `claude setup-token` 生成 | CI 和本地 | 打标签和读页面 |
| `codex-cli` | ChatGPT 订阅的额度 | 本机 codex CLI 的 ChatGPT 登录；没有 secret | 只在本地，需显式启用 | 只打标签 |
| `anthropic` | Anthropic API，按 token 计费 | secret `ANTHROPIC_API_KEY` | CI 和本地 | 打标签和读页面 |
| `openai` | OpenAI API（Responses API），按 token 计费 | secret `OPENAI_API_KEY` | CI 和本地 | 打标签和读页面 |
| `openai-compat` | 看你选的服务 | secret `LLM_API_KEY`，变量 `LLM_BASE_URL` 和 `LLM_MODEL_OPENAI_COMPAT` | CI 和本地 | 打标签和读页面 |
| `none` | 无 | 无 | 任何地方 | 只用规则 |

模型名写在 `config/llm.yaml` 里，从不写在代码里。repo 变量 `LLM_MODEL_<PROVIDER>`（例如 `LLM_MODEL_CLAUDE_CLI`）可以覆盖其中一个。订阅登录不能用配置的模型时，CLI provider 退回 CLI 的默认模型，run report 里会有一条警告。

## Provider 链

provider 按这个顺序尝试：

```
claude-cli -> codex-cli -> anthropic -> openai -> openai-compat -> none
```

* 只有配了凭证的 provider 才进链。`anthropic` 和 `openai` 还需要配模型，`openai-compat` 需要地址和模型。
* `codex-cli` 只有被显式点名时才进链，而且在 GitHub Actions 里永远不进链。所以 CI 里的链是 `claude-cli -> anthropic -> openai -> openai-compat -> none`。
* repo 变量 `LLM_PROVIDERS`（逗号分隔）可以替换顺序或者只选其中几个，例如 `claude-cli,anthropic`。列表里的 `none` 表示到此为止，所以 `LLM_PROVIDERS=none` 会关掉 LLM。
* 每次运行对第一个 provider 探活一次：运行的第一个请求就是探活；如果整次运行一个请求都没发（所有回答都来自缓存），就在生成 report 时发一个很小的探活请求。所以即使某天没有新事件，过期的凭证也会出现在 health 数据里。provider 遇到认证失败、额度用完、限流、超时或网络错误时，在本次运行剩下的时间里被标记为不可用，请求交给下一个 provider。全部失败后，运行只用规则继续。
* 格式不符合要求的回答会被丢弃。这不是换 provider 的理由。
* 每次失败都记进 health 数据。同一个 provider 连续失败两次，就为它开一个 `llm-auth` issue，写明要换哪个 secret、要跑什么命令；provider 恢复后 issue 自动关闭。

凭证从环境变量读取，去掉首尾空白，只交给需要它的 provider，永远不写进日志，连前缀也不写。

## GitHub Actions 怎么传凭证

* 只有 `update.yml` 拿到 LLM secret，而且只在两步里：`fetch` job 里运行 `seminars run` 的那一步，以及 `llm-smoke` job 里运行 `seminars llm-smoke` 的探活那一步。`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY` 和 `LLM_API_KEY` 在这两步里作为环境变量传入（secret 不存在时是空值）。`ci.yml` 和 `digest.yml` 拿不到任何 LLM secret。
* 拿着 LLM secret 的 job 不能写 repo。`fetch` job 把 `data/` 交给 `commit` job，由它 push；这个 job 没有 LLM secret，不安装任何东西，也不运行项目代码，只运行 git。
* 在拿着 secret 的步骤之前，先由一个不带 secret 的单独步骤安装锁定的依赖并构建项目（`uv sync --locked`）；构建后端的版本固定在 `pyproject.toml` 里。workflow 里的每个 action 都固定到某个 release 的 commit。
* `LLM_PROVIDERS`、`LLM_MODEL_<PROVIDER>` 和 `LLM_BASE_URL` 是 repo 变量，不是 secret。
* 只有 `CLAUDE_CODE_OAUTH_TOKEN` 存在时才安装 Claude Code，版本严格等于 `config/llm.yaml` 里 `cli_versions` 记录的版本。npm 不运行任何安装脚本（`--ignore-scripts`）；包自己的脚本本来会用平台包（`@anthropic-ai/claude-code-linux-x64`）里的二进制文件替换一个占位命令，脚本被跳过后，由 workflow 自己把这个二进制文件链接过去。
* 任何 workflow 里都没有 Codex 的步骤：不安装 Codex，也不把 Codex 的登录存成 secret。
* 这些 workflow 只由 `schedule` 和 `workflow_dispatch` 触发，从不由 pull request 触发，所以 fork 的代码永远碰不到 secret。GitHub 原话："With the exception of `GITHUB_TOKEN`, secrets are not passed to the runner when a workflow is triggered from a forked repository."

## 设置和更换凭证

用 `scripts/set-llm-credential.sh`。它需要 GitHub CLI（`gh`），并且已登录、有权限修改本 repo 的 secret；它作用于当前 checkout 对应的 repo（或用 `--repo OWNER/NAME` 指定的 repo）。

值是怎么传的：不加 `--from-file` 时，由 `gh` 提示输入，输入或粘贴的内容不显示。加 `--from-file PATH` 时，脚本去掉文件里的换行，把其余内容通过管道从标准输入交给 `gh secret set`。值永远不会出现在任何命令的参数里，不打印，也不写到别的地方。group 或 others 可以读写的文件会被拒绝（`chmod 600 PATH` 可以修正），空文件也会被拒绝。

### claude-cli

1. 生成 token：`claude setup-token`。需要 Pro、Max、Team 或 Enterprise 计划，有效期一年，只能发模型请求。
2. 把它存进只有你能读的文件，例如 `~/.secrets/claude-token` 并 `chmod 600`；或者准备好直接粘贴。
3. 上传：

   ```
   scripts/set-llm-credential.sh claude-cli --from-file ~/.secrets/claude-token
   ```

   如果要粘贴，就运行 `scripts/set-llm-credential.sh claude-cli`。
4. 可选：马上检查一下，加上 `--smoke`（见下面的"检查"）。

换计划以后，重新运行 `claude setup-token`，再运行一次这个脚本。代码不用改。

本地运行时，在运行 pipeline 的 shell 里导出 token：`CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.secrets/claude-token)" seminars run`。

### anthropic 和 openai

```
scripts/set-llm-credential.sh anthropic --from-file ~/.secrets/anthropic-key
scripts/set-llm-credential.sh openai --from-file ~/.secrets/openai-key
```

模型来自 `config/llm.yaml`；`--model NAME` 会改为设置 `LLM_MODEL_ANTHROPIC` 或 `LLM_MODEL_OPENAI`。

### openai-compat

任何提供 OpenAI 风格 chat completions 接口的服务：

```
scripts/set-llm-credential.sh openai-compat --from-file ~/.secrets/llm-key \
  --base-url https://llm.example.org/v1 --model <model>
```

这会设置 secret `LLM_API_KEY` 和变量 `LLM_BASE_URL`、`LLM_MODEL_OPENAI_COMPAT`。这个 provider 没有默认模型。

`LLM_BASE_URL`（或 `config/llm.yaml` 里的 `base_url`）必须是 `https://` 地址，因为每个请求都会把 `LLM_API_KEY` 发到那里。只有本机地址才接受明文的 `http://`：`localhost` 或 `127.0.0.1` 这样的 loopback 地址；其他地址算配置错误，运行不会开始。

### codex-cli（只在本地）

1. 登录：`codex login`，选 "Sign in with ChatGPT"。
2. 检查本机状态：`scripts/set-llm-credential.sh codex-cli`。它报告 `codex` 是否已安装、是否用 ChatGPT 登录、版本是否和 `config/llm.yaml` 里记录的一致。它不设置任何 GitHub secret。
3. 在一次运行里启用它，因为它永远不会自动启用：

   ```
   LLM_PROVIDERS=claude-cli,codex-cli seminars run
   ```

   或者 `seminars run --llm codex-cli`。即使启用了，它也只打标签；读页面时会跳过它。

### 删除 secret、选择顺序

* 删除不再使用的 secret：`scripts/set-llm-credential.sh openai --delete`。
* 设置链的顺序：任何一次调用都可以加上 `--providers claude-cli,anthropic`。要回到 `config/llm.yaml` 的顺序，运行 `gh variable delete LLM_PROVIDERS`。

### 检查

`--smoke` 在修改之后触发 `update.yml` 的 `llm-smoke` 模式，等同于 `gh workflow run update.yml -f mode=llm-smoke`。它给每个已配置的 provider 发一个很便宜的请求并报告每个结果；不抓取也不发布任何东西。本地的同一个检查是 `seminars llm-smoke`。

## 关掉 LLM

* CI 里：`scripts/set-llm-credential.sh none`，它把变量设成 `LLM_PROVIDERS=none`。要重新打开，运行 `gh variable delete LLM_PROVIDERS`，或者用 `--providers` 设一个顺序。
* 本地：`seminars run --no-llm`，或者 `LLM_PROVIDERS=none seminars run`。

没有 provider 时（没有凭证，或者 `LLM_PROVIDERS=none`），不会发出新的请求，但以前的运行存在 `data/llm_cache/` 里的回答仍然有效：被某个回答打过标签、或被判为不是学术报告的事件，只要文字不变，就保留这个回答。凭证被拒绝的运行得到同样的数据。这时不往缓存里加入新条目，也不删除旧条目；命中只会像平时一样更新这个条目的日期。其余的领域标签来自规则，需要读页面的源在 provider 恢复之前不产生事件。`seminars run --no-llm` 只用规则，也不读缓存。

## 费用

| 路线 | 怎么付费 |
|---|---|
| `claude-cli` | 计入 Claude 订阅的用量上限；没有按次账单。CLI 会报一个折算价格：用 Haiku 模型打一次标签约 $0.0004（2026-11-10 实测）。 |
| `codex-cli` | 用 ChatGPT 计划的额度。每个请求带着约 23k input token 的固定开销（CLI 的系统 prompt 和工具定义），是 `claude-cli` 请求的几十倍。 |
| `anthropic`、`openai` | 按配置模型的 API 价格按 token 计费。`config/llm.yaml` 里的默认模型约为每百万 input token $0.10、每百万 output token $0.50（定价页，2026-11）。 |
| `openai-compat` | 按所选服务的收费。 |
| `none` | 不花钱。 |

控制费用的设置，都在 `config/llm.yaml` 里：

* `batch_size`（20）：每个打标签请求里的事件数，用一个 JSON 列表回答。只有从源那里带着领域的事件才放进同一个请求；这样的请求给出的回答，如果和源说的不一样，就针对这个事件单独再问一次（见"页面文字是数据"）。规则定不了的事件从源那里得不到领域，所以这些事件每个都单独用一个请求去问：缓存为空时，第一次运行大约每个待定事件一个请求，到 `max_calls_per_run` 为止，剩下的由之后的运行去问。之后只问新的和改过的事件。
* `max_calls_per_run`（60）：一次运行里的请求数上限；超出的跳过并写进 report。
* `max_input_chars`（12000）：发送的 prompt 的最大长度；长页面按这个大小分段读，每个源最多读六段。任何请求都放不下的事件，以及六段之后的页面文字，都会跳过并写进 report。
* `data/llm_cache/` 里的缓存：key 是 prompt 版本、任务和规范化输入的 SHA-256。模型和 provider 不在 key 里，所以换 key、换计划、换模型都不会重复请求。`cache_ttl_days`（180）天没用过的条目会被删除。读页面的回答只保存页面上有的文本值，所以页面让模型写出的其他内容永远进不了提交到 repo 的缓存。secret 扫描在其中找到凭证形状字符串的文本值也进不了，即使是页面自己显示出来的：保存回答之前就把它去掉，讲座的其余信息照常保留。

run report 列出请求数、token 和每个 provider 的结果；费用只在后端自己报告时才有，目前只有 `claude-cli` 报告。`codex-cli` 和 API 路线（`anthropic`、`openai`、`openai-compat`）只报告 token 数，所以它们在 report 里的费用是 0；实际花了多少要看服务的计费页面。

## 凭证的有效期

* `CLAUDE_CODE_OAUTH_TOKEN`：从 `claude setup-token` 起一年。过期或被吊销后，`claude-cli` 报认证失败，第二次失败后会出现 `llm-auth` issue。生成新 token 再运行一次脚本即可；在生成后大约十一个月时设一个提醒，可以避免中断。
* API key：直到被吊销。余额用完时表现一样：额度失败和 `llm-auth` issue。
* `codex-cli` 的 ChatGPT 登录由本机的 CLI 自己续期。失效后重新运行 `codex login`。

## 两个命令行 provider 是怎么锁住的

输入是网页里的文字，而这两个 CLI 都是能用工具的 agent。两者的运行方式相同：

* 命令是参数列表，从不经过 shell；
* 带页面文字的 prompt 走标准输入，从不放进参数；
* 环境变量只有 `PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR` 和这个 provider 自己需要的（见下一节），所以 CLI 看不到其他凭证；
* 工作目录是一个新建的空临时目录；
* 超时（`timeout_seconds`，120 秒）会结束 CLI 和它启动的所有进程；
* 只读取标准输出里最终的 JSON，回答必须符合 schema；
* 标准错误只用来区分凭证过期和限流，然后丢弃：它可能包含账号信息，所以永远不进日志、report 或错误信息。

### CLI 拿到哪些环境变量

CLI 的环境变量是一个 allow-list：`seminars run` 的环境变量里，它只拿到下表列出的那些，而且只拿已设置、非空的。`--version` 和 `codex login status` 也一样。

| 变量 | `claude-cli` | `codex-cli` | 原因 |
|---|---|---|---|
| `PATH` | 是 | 是 | 找到 CLI 本身和它启动的程序 |
| `HOME` | 是 | 是 | home 目录；没有设置 `CODEX_HOME` 时，`codex-cli` 在其中的 `~/.codex` 里找它的登录 |
| `LANG`、`LC_ALL` | 是 | 是 | locale，让页面文字和回答都按 UTF-8 读写 |
| `TMPDIR` | 是 | 是 | 放临时文件的地方 |
| `CLAUDE_CODE_OAUTH_TOKEN` | 是 | 否 | `claude-cli` 的登录，也是 CLI 拿到的唯一凭证 |
| `CLAUDE_CONFIG_DIR` | 是 | 否 | 一个新建的空临时目录，请求结束后删除：CLI 既不读也不改 home 目录里的配置 |
| `DISABLE_AUTOUPDATER`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 是 | 否 | 由 `seminars` 设成 1，不是从外面传进来的：不自我更新，不顺带发送任何东西 |
| `CODEX_HOME` | 否 | 设置了才有 | `codex-cli` 的 ChatGPT 登录（`auth.json`）所在的目录 |

`claude-cli` 的 `--version` 既拿不到 token，也拿不到 `CLAUDE_CONFIG_DIR`。其余一律不传，特别是：

* `GITHUB_TOKEN` 和 `GH_TOKEN`。update workflow 为了 health issue 把 `GITHUB_TOKEN` 交给 `seminars run`；任何 CLI 都看不到它。
* `ANTHROPIC_API_KEY`、`OPENAI_API_KEY` 和 `LLM_API_KEY`，即 API provider 的 key。环境里有 API key 时，CLI 会改走 API 计费，而不是订阅。
* 会把 CLI 引到别处、或者让别人读到它的流量的变量：`ANTHROPIC_BASE_URL`、`OPENAI_BASE_URL`、`HTTPS_PROXY` 和其他代理变量（代理地址里可能带密码），以及 `SSL_CERT_FILE` 和 `NODE_EXTRA_CA_CERTS`。需要代理或自己的证书机构的网络用不了 CLI provider，这和本项目的 HTTP 客户端忽略这些变量是一样的。
* `NODE_OPTIONS`（它能往运行在 node 上的程序里加载代码），以及 agent 和密钥环的 socket，例如 `SSH_AUTH_SOCK` 和 `DBUS_SESSION_BUS_ADDRESS`。

测试用替身代替两个 CLI，从一个包含上面所有变量的环境启动它们，并把每个子进程拿到的环境变量和这张表逐一比对。

### claude-cli：Claude Code 2.1.322

```
claude -p --model <model> \
  --tools "" --strict-mcp-config --disallowedTools "mcp__*" \
  --setting-sources "" --disable-slash-commands \
  --permission-prompts none --no-session-persistence \
  --max-budget-usd 1.00 \
  --output-format json --json-schema '<schema>' \
  --system-prompt '<system prompt>' < prompt
```

* `--tools ""` 关掉全部内置工具。它不包括 MCP 工具，所以还要 `--strict-mcp-config`（不加载任何 MCP 配置）和 `--disallowedTools "mcp__*"`。
* `--setting-sources ""` 不读任何 user、project 或 local 设置，`--disable-slash-commands` 不加载 skill 和命令。
* `--permission-prompts none` 拒绝任何会请求权限的操作。
* `--no-session-persistence` 不在磁盘上留下页面文字的记录。
* `--max-budget-usd` 和超时限制单个请求；2.1.322 版没有 `--max-turns`。
* 不用 `--bare`：bare 模式不读 `CLAUDE_CODE_OAUTH_TOKEN`。
* 有 token 时，`CLAUDE_CONFIG_DIR` 指向一个临时目录，请求结束后删除。`DISABLE_AUTOUPDATER=1` 和 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 防止 CLI 自我更新或顺带发送任何东西。
* 只有 `is_error` 为 false、`subtype` 为 `success` 并且有 `structured_output` 时，回答才算数。

### codex-cli：Codex CLI 0.167.0

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

* 不加 `--disable` 那一串时，模型可以用约 26 个工具，其中有 shell、打补丁工具、联网、子 agent、MCP 和 plugin 工具。
* 加上之后还剩 `functions.exec` 和 `functions.wait`、`functions.request_user_input` 以及 `collaboration.*` 工具。没有文档记载的"零工具"开关，`code_mode_host` 和 `collaboration_modes` 这两个 feature 也去不掉剩下的工具。
* 每个 `--disable` 都单独去掉试过一次，用 `codex debug prompt-input` 检查结果（它不调用模型就能列出工具，但会读用户配置）：在 0.167.0 下只有 `shell_tool`、`view_image` 和 `sleep_tool` 会改变可用的工具，`apps` 和 `plugins` 会改变指令，其余十二个目前没有任何影响。整串保留是有意的纵深防御：现在默认关闭的 feature 在以后的版本里可能默认打开，多一个 flag 也没有任何代价。
* `web_search` 必须显式关掉：它的默认值是 `"cached"`，而 sandbox 不管 web search、app 和 MCP。read-only sandbox 只管 shell 命令：可以读盘，不能写，也不能联网。
* `--ignore-user-config` 只跳过 `config.toml`。登录照常从 `CODEX_HOME` 读取，这个目录保持不变。
* `--json` 打印运行中的事件；读取它们是为了 token 数、错误信息和工具调用。回答是 `-o` 写出的文件。

**残余风险。** 这条命令以你的身份在你的机器上运行，能读到你能读的文件，包括 codex CLI 的登录文件（`$CODEX_HOME/auth.json`）。恶意页面可能让模型调用剩下的某个工具去读本机文件。挡在这件事和泄密之间的是：sandbox 不能联网；回答必须是枚举值（领域 id、数字、是或否），没有夹带文字的余地，带自由文本的请求在发送之前就被拒绝；事件流里出现任何工具调用的回答都会被丢弃；所有内容在写入 `data/` 或 `public/` 之前都经过 secret 扫描。所以 `codex-cli` 只打标签，从不读页面，而且不点名就永远不启用。

### 为什么 codex-cli 不在 GitHub Actions 里运行

OpenAI 关于在 CI 里给 Codex 认证的文档写道："The right way to authenticate automation is with an API key." 以及 "Do not use this workflow for public or open-source repositories."，前提是 runner 属于 "trusted private infrastructure"。本项目是 GitHub 托管 runner 上的公开 repo，正好是它警告的情况。此外：

* 这个 CLI 不能在没有工具的情况下运行，剩下的工具能读 runner 的磁盘，包括登录文件；
* 每个请求带着约 23k token 的开销；
* ChatGPT 账号可能属于一个受管理的 workspace，它的规定可能不允许把登录放进公开的自动化里；
* 在 Ubuntu 24.04 runner 上 sandbox 需要额外设置（unprivileged user namespace 和 AppArmor）；
* 登录大约八天后刷新，刷新会轮换它，所以 CI 每次运行后都得把它写回 repo secret，这需要一个能改写本 repo 任意 secret 的长期 token，而且同一份登录不能同时供两个 job 使用。

所以 CI 里的 GPT 模型走 API key（`openai` 或 `openai-compat`）。

## 页面文字是数据

无论用哪个 provider，页面都左右不了结果：

* 页面文字放在两行标记之间交给模型，标记由文字本身推出，页面拼不出来；system prompt 声明这段文字是数据，其中的任何指令都不执行。
* 回答必须通过 schema 校验。多了字段或字段类型不对的回答整体丢弃；打标签的回答只能包含 taxonomy 里的领域 id。
* 一个事件决定不了另一个事件。打标签时一个请求最多发 `batch_size` 个事件，模型仍然可能照其中一个事件里的指令去处理其他事件。所以从源那里没有领域的事件（也就是所有由 LLM 决定领域的事件）总是单独用一个请求去问。来自多事件请求的回答，只有在它说的正是源本来就说的（是学术报告，领域恰好是源的领域）时才直接采用。其他回答（不是学术报告、或者领域不同）都会针对这一个事件单独再问一次，只采用并缓存单独问到的回答；这次请求失败时，这个事件没有回答，由规则决定。
* 读页面得到的每个文本值（标题、讲者、单位、系列、地点、摘要）规范化后必须出现在页面文字里，或者最多差几个字符（`grounding_min_similarity`，90）。保留下来的是页面自己的文字，不是模型的措辞，所以模型加不进任何一个它自己的字符。日子必须在文字里紧挨着它的月份名出现，或者写成数字形式的日期；小时和分钟只要求在文字里作为数字出现，这只能说明它们合理，不能证明它们正确。不满足的一律丢弃。
* 模型从不写 URL。链接是页面文字里带编号的标记，回答只写标记的编号；只有页面上真实存在的链接才能出现在结果里。
* 写入 `data/` 或 `public/` 之前，secret 扫描会查找 API key、OAuth token、JWT 之类的字符串。一旦命中，运行停止：不提交，也不部署。读页面时，回答的每个文本值在缓存之前都先经过同样的扫描，所以页面上显示的 token 永远进不了 `data/llm_cache/`；讲座的其余信息照常保留。

**打标签的残余风险。** 恶意事件仍然可以让模型给同一批的其他事件一个"和源一样"的回答（是学术报告，领域是源的领域）。这样的回答会被采用并缓存。同一批里的事件都从源那里带着领域，规则也按这些领域给它们分类，所以它们保留自己的领域、留在 feed 里，就像没有问过 LLM 一样：只要文字不变，其中不是学术报告的事件也会留在 feed 里。关于一个事件的回答不能把另一个事件从 feed 里去掉，也不能拿走它的领域。另外，页面本来就可以把自己的事件写错，有没有 LLM 都一样。
