[English](README.md) | 中文

# seminars

每天从公开网页收集 Stanford、SLAC、CZ Biohub 及周边其他研究机构的学术
seminar、talk 和 colloquium，以三种形式发布：

- **日历订阅**（ICS），可以在日历应用里订阅：每个领域一个、个人精选一个、全部一个；
- 一个**静态网站**，列出即将举行的报告，可以按领域和来源过滤，支持全文搜索；
- **每周摘要**：每周一早上（太平洋时间）开一个 GitHub issue。

全部运行在 GitHub Actions 和 GitHub Pages 上，没有服务器。数据就存在本仓库里
（`data/`），每天的更新是一个 commit。

## 内容

### 日历订阅

共 9 个 feed。一个报告可以同时出现在几个领域的 feed 里；`all` 包含至少属于一个领域的全部报告。

| Feed | 内容 | 订阅 |
|---|---|---|
| `all` | 从 Stanford、SLAC、CZ Biohub 及周边研究机构收集到的全部报告 | [all.ics](https://fridrichmethod.github.io/seminars/feeds/all.ics) |
| `picks` | 符合个人兴趣规则（`config/interests.yaml`）的报告 | [picks.ics](https://fridrichmethod.github.io/seminars/feeds/picks.ics) |
| `math` | 纯数学、应用数学和统计 | [math.ics](https://fridrichmethod.github.io/seminars/feeds/math.ics) |
| `cs` | 计算机科学 | [cs.ics](https://fridrichmethod.github.io/seminars/feeds/cs.ics) |
| `ai-ml` | 人工智能和机器学习 | [ai-ml.ics](https://fridrichmethod.github.io/seminars/feeds/ai-ml.ics) |
| `chem` | 化学和化学工程 | [chem.ics](https://fridrichmethod.github.io/seminars/feeds/chem.ics) |
| `bio` | 生物、生物工程、结构生物学和遗传学 | [bio.ics](https://fridrichmethod.github.io/seminars/feeds/bio.ics) |
| `physics` | 物理和应用物理 | [physics.ics](https://fridrichmethod.github.io/seminars/feeds/physics.ics) |
| `med` | 医学和生物医学研究 | [med.ics](https://fridrichmethod.github.io/seminars/feeds/med.ics) |

每个 feed 是网站上的文件 `feeds/<feed>.ics`；表里链接的是它的 `https://` 地址，
"订阅"一节列出了它的 `webcal://` 地址。每个事件都有稳定的标识符，所以标题或时间变了，
日历里的事件会被更新，而不会多出一个重复的事件。数据源列出时既没有编号、也没有单独页面的报告，
按日期、人和标题来辨认。这样的报告如果在同一次更新里既改了时间又改了标题或报告人，或者时间移动超过
60 天，或者同一位报告人的两场相似报告同时改动，日历里最多会有两天显示两次。

### 网站

网站的地址是
[https://fridrichmethod.github.io/seminars/](https://fridrichmethod.github.io/seminars/)。
它按时间顺序列出即将举行的报告，可以按领域和来源过滤，支持全文搜索，提供英文和中文界面。
列表里的每个报告都有一个复选框，勾选的报告可以存成一个日历文件，见"下载选中的报告"一节。
它的[状态页](https://fridrichmethod.github.io/seminars/status.html)显示最近一次更新的时间和每个数据源的情况。

### 每周摘要

每周一早上开一个带 `digest` 标签的新 issue，列出本周（周一到周日）的报告和上一期以来新增的报告，
并关闭上一期摘要。

## 订阅

把 feed 地址开头的 `https://` 换成 `webcal://`，大多数日历应用就会订阅它，而不是只导入一次。
GitHub 不会把 `webcal://` 地址变成链接，所以把它们列在这里，方便复制：

```
webcal://fridrichmethod.github.io/seminars/feeds/all.ics
webcal://fridrichmethod.github.io/seminars/feeds/picks.ics
webcal://fridrichmethod.github.io/seminars/feeds/math.ics
webcal://fridrichmethod.github.io/seminars/feeds/cs.ics
webcal://fridrichmethod.github.io/seminars/feeds/ai-ml.ics
webcal://fridrichmethod.github.io/seminars/feeds/chem.ics
webcal://fridrichmethod.github.io/seminars/feeds/bio.ics
webcal://fridrichmethod.github.io/seminars/feeds/physics.ics
webcal://fridrichmethod.github.io/seminars/feeds/med.ics
```

一个报告属于几个领域，就会出现在这几个领域各自的 feed 里，而日历应用按 feed 显示事件，每个 feed 一次：
同时订阅其中两个 feed，这个报告就会显示两次。想让每个报告只出现一次，就只订阅 `all`（或者只订阅 `picks`）。
报告结束 14 天后，feed 会把它删掉，它也会随之从你的日历里消失。

### Apple Calendar

1. 在 Mac 的"日历"里选择"文件 > 新建日历订阅"。
2. 粘贴 feed 的 `webcal://` 地址，点"订阅"。
3. "位置"选 iCloud，这样 iPhone 和 iPad 也会同步这个日历；再选择自动刷新的频率。
   每天一次就够了，因为数据每天只更新一次。

### Google Calendar

在 calendar.google.com 打开"其他日历 > 通过网址添加"，粘贴 feed 的 `https://` 地址。
Google 按自己的节奏刷新订阅的日历，可能要几个小时。

### 其他日历应用

能订阅 ICS 地址的应用都可以用。也可以把文件导入一次，但之后的改动就不会同步过来。

## 下载选中的报告

网站还可以把挑出来的报告存成一个日历文件，导入一次。在列表里勾选想要的报告，也可以点"全选当前显示的报告"，
一次勾选列表在当前筛选和搜索条件下显示的全部报告。列表上方的选择栏显示选中了几个报告，
以及其中有几个被筛选条件隐藏了。改筛选条件、搜索或者切换语言时，选中的报告保持不变；点"清空选择"或者重新加载页面会清空选择。
点"下载所选报告（.ics）"，浏览器保存文件 `seminars-selected.ics`，里面是全部选中的报告，被筛选条件隐藏的也在内。
文件里的事件就是 feed `all` 里的事件，原样复制，UID 也相同。页面读不到 feed，或者 feed 里没有任何一个选中的报告时，
不保存文件，并给出提示；有些选中的报告不在 feed 里时，文件里是其余的报告，页面会说明少了几个。

这个文件是一次性的副本：之后报告改了时间或者取消了，导入过这个文件的日历不会知道。
订阅（见"订阅"一节）则会一直保持最新。

导入这个文件：

- Apple Calendar：打开这个文件，或者选择"文件 > 导入"，再选择把报告加进哪个日历。
- 网页版 Google Calendar：打开"设置 > 导入和导出"，选"导入"，选择文件和日历，然后点"导入"。
- Outlook：打开这个文件，或者把它作为日历导入。

## 覆盖范围

下面的数字在第一次公开运行时测得，那次运行从太平洋时间 2026-11-12 23:06 开始。"未来四周"统计的是
当时还没有结束、并且在之后 28 天内开始的报告。网站的状态页随时显示每个数据源的当前状态。

### 数据源及其状态

配置的 42 个数据源里启用了 32 个。第一次公开运行中，29 个给出了报告，1 个为空，2 个失败，
共存储了 348 个报告。一个报告被几个数据源列出，在这张表里就为每个数据源各算一次。"抽取方式"是这次
运行读取一个数据源的方法：api 取自网站的 JSON 接口，feed 取自它的 RSS 或 iCalendar feed，html 按规则从页面本身
读取；失败或为空的数据源写的是它的 adapter 所用的方法。

| 数据源 | 组 | 状态 | 未来四周 | 抽取方式 |
|---|---|---|---:|---|
| Stanford Events (`localist-stanford`) | `localist` | 正常 | 43 | api |
| Stanford AIMI (`sites-aimi`) | `stanford-sites` | 正常 | 3 | feed |
| Stanford Biochemistry (`sites-biochemistry`) | `stanford-sites` | 正常 | 3 | feed |
| Stanford Biology (`sites-biology`) | `stanford-sites` | 正常 | 4 | api |
| Stanford Chemical Engineering (`sites-cheme`) | `stanford-sites` | 正常 | 2 | feed |
| Stanford ChEM-H (`sites-chemh`) | `stanford-sites` | 正常 | 4 | feed |
| Stanford Chemistry (`sites-chemistry`) | `stanford-sites` | 正常 | 6 | api |
| Stanford ICME (`sites-icme`) | `stanford-sites` | 正常 | 2 | feed |
| Stanford Mathematics (`sites-mathematics`) | `stanford-sites` | 正常 | 20 | api |
| Stanford Mathematics Research Center (`sites-mrc`) | `stanford-sites` | 正常 | 0 | api |
| Stanford Materials Science and Engineering (`sites-mse`) | `stanford-sites` | 正常 | 2 | feed |
| Stanford Physics (`sites-physics`) | `stanford-sites` | 正常 | 2 | api |
| Stanford Statistics (`sites-statistics`) | `stanford-sites` | 正常 | 6 | api |
| Stanford SystemX Alliance (`sites-systemx`) | `stanford-sites` | 正常 | 1 | feed |
| AI + Data for Science Seminar (`custom-ai4science`) | `stanford-custom` | 正常 | 2 | html |
| Stanford Chemical and Systems Biology Events (`custom-csb-events`) | `stanford-custom` | 正常 | 2 | html |
| Stanford Workshop in Biomedical Data Science (`custom-dbds-workshop`) | `stanford-custom` | 失败：从 GitHub 访问 robots.txt 超时 | 0 | html |
| Stanford HAI Events (`custom-hai-events`) | `stanford-custom` | 正常 | 3 | html |
| Stanford ISL Colloquium (`custom-isl-colloquium`) | `stanford-custom` | 正常 | 2 | html |
| Stanford MLSys Seminar (`custom-mlsys-seminar`) | `stanford-custom` | 空：页面上的报告都已过去 | 0 | html |
| Stanford NLP Seminar (`custom-nlp-seminar`) | `stanford-custom` | 失败：从 GitHub 访问 robots.txt 超时 | 0 | html |
| Stanford RAIN/OR Seminar (`custom-rain-seminar`) | `stanford-custom` | 正常 | 3 | html |
| Stanford Synthetic Biology Events (`custom-synbio-events`) | `stanford-custom` | 正常 | 1 | html |
| Stanford CS Theory Lunch (`custom-theory-lunch`) | `stanford-custom` | 正常 | 3 | html |
| Topos Institute Colloquium (`rsorg-topos-colloquium`) | `researchseminars` | 正常 | 3 | feed |
| Biohub events (`bayarea-biohub`) | `bayarea` | 正常 | 1 | html |
| Condensed Matter Physics Seminars (GLAM) (`bayarea-glam`) | `bayarea` | 正常 | 3 | feed |
| KIPAC Seminars and Events (`bayarea-kipac`) | `bayarea` | 正常 | 12 | feed |
| Leinweber Institute for Theoretical Physics (LITP, formerly SITP) seminars (`bayarea-litp`) | `bayarea` | 正常 | 4 | api |
| SLAC Photon Science Seminar Series (Stanford PULSE Institute) (`bayarea-pulse`) | `bayarea` | 正常 | 1 | feed |
| Q-FARM Seminars and Events (`bayarea-qfarm`) | `bayarea` | 正常 | 5 | feed |
| SLAC Colloquium Series (`bayarea-slac-colloquium`) | `bayarea` | 正常 | 3 | html |

### 每个 feed 的报告数

"feed 里的总数"统计运行之后 feed 里的全部报告：过去 14 天的和未来 120 天的。

| Feed | 未来四周 | feed 里的总数 |
|---|---:|---:|
| `all` | 133 | 348 |
| `picks` | 14 | 31 |
| `math` | 35 | 92 |
| `cs` | 20 | 49 |
| `ai-ml` | 24 | 62 |
| `chem` | 16 | 37 |
| `bio` | 33 | 95 |
| `physics` | 38 | 95 |
| `med` | 31 | 86 |

### 已知缺口

- 没有收集的：Bio-X 对自动化请求返回 HTTP 403；Information Theory Forum 已停用（几乎不再活动，
  列表里也没有年份）。SAIL 的活动和 Theory Seminar 只发布在 Google Calendar 上，而它的 robots.txt
  禁止抓取这些 feed。Arc Institute 没有公开的 seminar 日历。七个领域以外的报告（比如地球与行星科学）
  不在范围内。
- DBDS workshop 和 NLP seminar 从 GitHub 的 runner 上可能失败，因为那里不一定连得上它们的 robots.txt；
  从 Stanford 的网络访问两者都正常。状态页会显示这样的失败，连续三次会开一个 `source-broken` issue。
- MLSys seminar 的页面没有公布新的报告时，它返回空。
- 从自由文本里读讲者和单位以精度优先：宁可缺一位讲者，也不给错的。
- 没有 LLM 时，领域只靠规则标注：学校总日历上七个领域内的报告，规则能认出大约 75%，
  开了 LLM 打标签大约 96%。一次运行最多给 LLM 发 60 个请求，所以空的答案缓存要几次运行才能填满。
- 被删掉又回来的报告从 `SEQUENCE:0` 重新开始。

[docs/ARCHITECTURE.zh-CN.md](docs/ARCHITECTURE.zh-CN.md) 详细说明了这些限制和其他限制。

## 每日更新怎么运行

### 时间安排

workflow `.github/workflows/update.yml` 每天太平洋时间 04:23 运行，10:23 再补跑一次，
因为 GitHub 有时会推迟甚至丢掉定时任务。定时运行先看 `data/last_run.json`：
如果当天（按太平洋时间的日期）已经成功跑过，就直接结束，什么都不改。实际效果是每天更新一次。
时间随夏令时调整。

### 一次运行的步骤

1. 礼貌地抓取每个数据源：遵守 robots.txt（包括 `Crawl-delay`），对每个 host 一次只发一个请求，
   每个 host 的请求数有上限。
2. 把每个数据源的内容转成事件，去掉重复（同一个报告被两个来源列出），去掉不是学术报告的条目。
3. 给每个事件标注领域，靠规则；配置了凭证时再借助 LLM；然后计算精选分数。
4. 写出 `data/`、9 个 feed 和网站数据，然后检查输出：secret 扫描和 `data/` 的字符集规则。
   有任何一项不通过，这次运行以 exit status 3 结束，什么都不提交、不部署。
5. 校验数据和 feed（`seminars validate`），以 `github-actions[bot]` 的身份提交 `data/`，
   提交信息是 `chore(data): daily update YYYY-MM-DD`，构建网站（`seminars build-site`），
   部署到 GitHub Pages。

单个数据源失败不会中断整次运行。连续三次不正常时会开一个带 `source-broken` 标签的 issue，
数据源恢复后自动关闭。LLM 凭证出问题时会开一个带 `llm-auth` 标签的 issue。

### 每周摘要

workflow `.github/workflows/digest.yml` 每周一太平洋时间 07:07 运行，周二同一时间补跑一次。
它只读已经提交的数据，不抓取，也不用 LLM。同一周永远不会开出两期摘要。

### LLM 辅助（可选）

LLM 只做两件事：规则读不懂的页面由它兜底抽取事件，以及标注领域。没有 LLM 也能完整运行。
在 GitHub Actions 里按下面的顺序尝试，只有配了凭证的才参与：`claude-cli`（Claude 订阅 token）、
`anthropic`、`openai`、`openai-compat`（API key）。凭证是 repository secret
（`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`LLM_API_KEY`），
顺序和模型是 repository variable（`LLM_PROVIDERS`、`LLM_MODEL_<PROVIDER>`、`LLM_BASE_URL`）。
API provider 还要配了模型（来自它的 variable 或 `config/llm.yaml`）才会参与。
换凭证或换 plan 不需要改代码，见 [docs/LLM_CREDENTIALS.zh-CN.md](docs/LLM_CREDENTIALS.zh-CN.md)。
手动运行 update workflow 并选 `llm-smoke` 模式，会检查每个已配置的 provider，不做任何改动。
某次运行里装不上 Claude Code CLI 时（比如 npm 暂时不可用），workflow 只给出警告，其余 provider 照常参与。

只有运行 pipeline 的那一步和探活的那一步能拿到 LLM 凭证。能看到凭证的 workflow 从不为 pull request 运行，
所以 fork 里的代码碰不到它们。

### 如果 GitHub 停用了定时任务

public 仓库 60 天没有活动时，GitHub 会停用它的定时 workflow。每天的数据提交应该能避免这种情况，
但 GitHub 没有保证它算作活动。如果收到通知，打开 Actions 页面，选中这个 workflow，
点 Enable workflow 即可。

## 本地运行

### 需要的工具

- [uv](https://docs.astral.sh/uv/)：它会安装 `.python-version` 指定的 Python 和 `uv.lock`
  锁定的依赖。
- git，以及运行脚本用的 bash。

### 命令

```
uv sync --locked
uv run seminars run --offline       # recorded pages from tests/fixtures/, no network
uv run seminars run --no-llm        # the real sites, rules only
uv run seminars build-site          # assembles public/
uv run seminars validate            # checks data/ and public/
uv run python -m http.server --directory public 8000
uv run seminars digest --dry-run    # prints the digest instead of opening an issue
```

真实联网的运行会改写你本地的 `data/`。离线运行只在 `data/` 里没有真实运行的结果时才写进去；
提交的 `data/` 是真实运行的结果，所以在刚 clone 的仓库里，离线运行在一个临时目录里进行，并会说明这一点。
真实联网的运行很礼貌，所以很慢：有些网站要求两次请求之间间隔 30 秒。`uv run seminars --help`
列出所有命令和选项。

### 检查

这些就是 CI workflow（`.github/workflows/ci.yml`）里的检查，push 之前先跑一遍：

```
python3 scripts/check_ascii.py
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

测试从不访问网络。网站的浏览器冒烟测试默认不跑，这样运行：

```
uv sync --locked --extra browser
uv run --extra browser playwright install chromium
uv run --extra browser pytest -m browser
```

## 添加数据源

大多数数据源只需要几行 YAML；结构特殊的网站再加一个小的 adapter 模块。
[docs/ADDING_A_SOURCE.zh-CN.md](docs/ADDING_A_SOURCE.zh-CN.md) 讲了这两种做法、
怎样用礼貌客户端录制测试 fixture，以及字符集规则。

## 仓库结构

```
config/            settings, LLM settings, fields, interests, sources.d/ (one file per group)
data/              the committed data: events, stable ids, health, last run, LLM cache, archive
docs/              documentation in English and Chinese
locales/           every text of the web site and the digest (en, zh-CN)
scripts/           check_ascii.py, set-llm-credential.sh
site/              the static web site (no build step)
src/seminars/      the package: sources/, pipeline/, classify/, llm/, outputs/, cli.py
tests/             tests, and fixtures/ with recorded pages of every source
.github/workflows/ ci.yml, update.yml, digest.yml
```

## 文档

- [docs/ARCHITECTURE.zh-CN.md](docs/ARCHITECTURE.zh-CN.md)：项目是怎么搭起来的、一次运行做了什么、
  已知的限制。
- [docs/ADDING_A_SOURCE.zh-CN.md](docs/ADDING_A_SOURCE.zh-CN.md)：怎样添加数据源。
- [docs/LLM_CREDENTIALS.zh-CN.md](docs/LLM_CREDENTIALS.zh-CN.md)：LLM provider 和它们的凭证。
- [CHANGELOG.md](CHANGELOG.md)：每个版本包含什么（英文）。

## 许可和数据

代码使用 MIT 许可，见 [LICENSE](LICENSE)。事件信息属于发布它的主办方，每个事件都链接到原始页面。
`data/` 里的一切和本仓库的其他内容一样是公开的。只读取公开页面：不读需要登录的页面，
不读邮件列表，也不收集公开页面以外的个人信息。
