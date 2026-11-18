[English](ARCHITECTURE.md) | 中文

# 架构

本文说明这个项目是怎么搭起来的：包里的各个部分、每天一次运行做了什么、事件怎样保持稳定的标识、
LLM 层怎么用、GitHub workflow 做什么，以及已知的限制。怎么订阅、怎么运行项目见
[README](../README.zh-CN.md)，怎么添加数据源见 [ADDING_A_SOURCE.zh-CN.md](ADDING_A_SOURCE.zh-CN.md)，
怎么配置语言模型见 [LLM_CREDENTIALS.zh-CN.md](LLM_CREDENTIALS.zh-CN.md)。下文提到的各个模块，
完整的规则都写在它们的 docstring 里。

## 概览

没有服务器。GitHub Actions 的 workflow 每天读一遍每个数据源的公开网页，把内容转成事件，把结果提交到
`data/`，再把带日历 feed 的静态网站部署到 GitHub Pages。另一个 workflow 每周根据已经提交的数据开一期摘要
issue。git 就是数据库：每次运行读上一次运行写下的文件，再写出新的文件，每天的提交只包含真正变化的内容。

```
config/            sources, fields, interests, settings, LLM settings
   |
seminars run       fetch -> parse -> normalize -> filter -> merge -> classify
   |               -> scope -> picks -> persist -> health -> write -> check
   v
data/              events.json, uid_map.json, health.json, archive/, llm_cache/,
   |               last_run.json, run_report.json  (committed)
seminars build-site
   v
public/            index.html, status.html, events.json, status.json, feeds/<id>.ics
                   (deployed to GitHub Pages)
```

## 模块一览

各个功能包（`pipeline/`、`classify/`、`llm/`、`outputs/`、`sources/`）通过 `seminars.contracts` 里的
protocol 相互衔接，那里也写明了每个 protocol 由哪个模块实现；pipeline 通过 `load_implementation` 加载
实现。每个 contract 在测试里都有一个 fake，所以每个包都能单独测试。基础部分在需要它的地方直接导入：
`models/`、`config/`、`contracts/`、HTTP 模块（`http*.py`、`robots.py`）、`fixtures.py` 和文本模块
（`textrules.py`、`textio.py`、`i18n.py`）。少数共用的辅助代码也直接导入：

- `pipeline/collect.py` 从 `sources/base.py` 取 adapter 注册表和它的错误类型，从
  `sources/generic/common.py` 取 `detail_pages_optional`；
- `sources/generic/common.py` 从 `pipeline/normalize.py` 取 `MAX_SPEAKERS`；
- LLM 页面抽取器（`sources/generic/llm_page.py`）和 LLM 打标签模块（`classify/llm_tagger.py`）从
  `llm/base.py` 取 `record_skipped_too_long`，页面抽取器还用 `llm/extraction.py`、`llm/grounding.py`、
  `llm/privacy_filter.py` 和 `llm/secret_filter.py` 里的辅助代码；
- 命令行（`cli.py`）导入 pipeline 和 `github.py`。

| 部分 | 内容 |
|---|---|
| `cli.py` | 命令行，一个 Typer 应用 |
| `contracts/` | 包之间的 protocol，以及它们的实现在哪里 |
| `models/` | 数据模型：`SourceConfig`、`EventDraft`、`Event`、运行报告、健康记录、公开的数据格式 |
| `config/` | 读取并检查 `config/` 和环境变量；项目路径 |
| `http.py`、`robots.py` | 礼貌的联网客户端和离线客户端（加上各个 `http*.py` 模块） |
| `fixtures.py` | 录制、检查和提供 fixture（`python -m seminars.fixtures`） |
| `sources/` | adapter 的 protocol 和注册表（`sources/base.py`）、`generic/` 和 `custom/` 里的 adapter、格式探测 |
| `pipeline/` | 一次运行：collect、normalize、privacy、ids、dedup、tagging、diff、store、checks、publish |
| `classify/` | 领域规则、事件过滤、精选、LLM 打标签 |
| `llm/` | 各个 provider、provider 链、答案缓存、grounding、secret 和隐私过滤 |
| `outputs/` | ICS feed、网站数据、每周摘要、健康记录和相应的 issue |
| `github.py` | 健康记录和摘要共用的 issue 客户端 |
| `textrules.py` | 文本和 `data/` 的字符集规则 |

配置都在 `config/` 里：`settings.yaml`（时区、往前 14 天往后 120 天的时间窗口、`meeting_horizon`
数据源的会议往后最多 365 天、HTTP 上限、去重和健康的阈值）、
`sources.d/`（每组数据源一个文件）、`taxonomy.yaml`（七个领域、排除规则、院系标签）、
`interests.yaml`（精选的打分）和 `llm.yaml`（provider、模型、上限）。任何地方出现未知的 key 都会报错。
网站和摘要的文案在 `locales/en.json` 和 `locales/zh-CN.json` 里。

## 一次运行

`seminars run`（`pipeline/run.py`）在开始时确定这次运行的时间：联网运行用真实时间，离线运行用录制的
fixture 的参考时间。每个阶段都拿到这个时间，没有一个阶段自己读时钟。

1. **抓取**（`pipeline/collect.py`）。每个启用的数据源由它的 adapter 抓取页面，每个数据源一个工作线程，
   同时最多 8 个。HTTP 客户端让对同一个 host 的请求严格串行、按间隔发出，所以线程只在不同的 host 上重叠。
   一个数据源总共最多用 15 分钟。一个数据源出的任何问题（host 被拒绝、请求预算用完、页面解析不了、
   adapter 的 bug）都只算这个数据源失败。
2. **解析**。adapter 把抓到的文档转成 draft（`EventDraft`）。解析在调用线程上进行，按配置的顺序一个数据源
   接一个，所以结果不取决于哪个抓取先完成。对于 `llm_fallback: true` 或 `adapter: llm_page` 的数据源，
   规则什么都读不出来时，由 LLM 页面抽取器来读。
3. **规范化**（`pipeline/normalize.py`）。时间按数据源的时区解释。在窗口结束之后才开始的事件，
   以及在窗口开始之前就已经结束的事件（没有结束时间的：在窗口开始之前就开始了）被丢掉，所以一个三周的
   workshop 在进行期间一直保留。会议，也就是 conference 或者跨好几天或至少 3 小时的 workshop
   （`pipeline/meetings.py`），如果来自设置了 `meeting_horizon` 的数据源，开始时间最多可以在往后
   365 天；这个数据源的报告仍然用平常的窗口。全天或者至少 3 小时、标题里写着会议（symposium、
   conference、summit、workshop、boot camp、summer school 等）的 seminar、lecture 或 other 类型事件，
   先变成 conference 或 workshop。带时刻、跨一周以上的会议，如果每天的时段像会议（结束时刻比开始时刻
   至少晚 3 小时，或者两者都是午夜），按日期保留为全天事件；从第一周到最后一周列成一个事件、每周一小时的
   报告仍然是报告。
   每段文字都要清理（去掉标记、字符集规则、长度上限），去掉会议口令、邮箱地址和电话号码
   （`pipeline/privacy.py`），以及看起来像凭证的字符串（`pipeline/secret_scan.py`）。讲者那一行按人
   拆开（`pipeline/speakers.py`）；没有讲者的 draft，从标题里按几种常见写法读出讲者
   （`pipeline/title_speakers.py`）。事件得到它的 source key 和 uid（`pipeline/ids.py`）。
4. **过滤**（`classify/filters.py`）。`config/taxonomy.yaml` 里的短语排除不是学术报告的条目（招待会、
   office hour、课程之类），有些院系标签一律不在范围内。
5. **合并**（`pipeline/dedup.py`）。已经存储的 uid 交接给变了的 key，同一个报告的几个条目合并成一个事件。
6. **分类**（`pipeline/tagging.py`）。领域来自数据源和规则（`classify/rules.py`）；规则判断不了的，
   有 LLM 时交给 LLM 打标签（`classify/llm_tagger.py`）。
7. **范围**。一个领域都没有的事件按 `out_of_scope` 排除。
8. **精选**（`classify/picks.py`）。`config/interests.yaml` 里的关键词、系列和讲者加起来是一个分数；
   达到阈值的事件是精选，并附上理由。
9. **持久化**（`pipeline/diff.py`）。这次运行的事件和已存储的事件比较，见下文"已存储的事件"。
10. **健康**（`outputs/health.py`）。每个数据源和每个 LLM provider 的结果加进历史记录，`source-broken`
    和 `llm-auth` 两种 issue 随之打开、更新或关闭。
11. **写出**（`pipeline/store.py`）。先写数据文件，再写 feed 和网站数据，最后写这次运行的两份记录
    （`data/run_report.json`、`data/last_run.json`）。
12. **检查**（`pipeline/checks.py`）。secret 扫描和 `data/` 的字符集规则，在写出之前检查要写的文件，
    写出之后再检查磁盘上的 `data/` 和 `public/`。

`seminars run` 的 exit status，update workflow 按它行事：

| Status | 含义 | workflow 怎么做 |
|---|---|---|
| 0 | 运行成功，或者定时运行发现当天已经跑过 | 提交、构建、部署（跳过时什么都不做） |
| 1 | 所有数据源都失败，或者写输出失败（记录了 `ok: false`） | 只提交健康记录和这次运行的记录，部署，然后让 workflow run 失败 |
| 1 | 运行没能开始，或者被一个没有捕获的错误中断 | 什么都不提交、不部署 |
| 2 | 命令行用法错误 | 什么都不提交、不部署 |
| 3 | 输出检查发现了 secret，或者违反字符集规则的字符 | 什么都不提交、不部署 |

一次运行永远不会建立在 fixture 数据之上：`data/last_run.json` 记录的是离线运行时，联网运行从空的
已存储事件开始，并在警告里写明。`seminars run --dry-run` 在临时目录里做完所有事情，什么都不改。

## 数据源

数据源配置在 `config/sources.d/` 里，分成四层：

| 层 | 组 | 内容 | Adapter |
|---|---|---|---|
| 1 | `localist` | Stanford Events（events.stanford.edu），学校的总日历，通过它的 JSON API 读取 | `localist` |
| 2 | `stanford-sites` | 建在 Stanford Sites（Drupal）上的院系网站 | `drupal_jsonapi`、`rss` |
| 3 | `stanford-custom`、`researchseminars` | 有自己页面的实验室、中心和系列；researchseminars.org 上的系列 | `html_list` 和定制 adapter |
| 湾区 | `bayarea` | SLAC、CZ Biohub 和湾区其他研究机构 | `rss`、`html_list`、`drupal_jsonapi`、`slac_drupal` |

抽取方法按这个顺序选：结构化 API、feed、页面里的 JSON-LD、用 CSS 选择器读的 HTML 列表，最后才是
LLM 页面抽取器。Stanford Sites 的两种模板不一样：一种提供 Drupal JSON:API（`/jsonapi/node/hs_event`），
另一种提供一个当作索引用的 RSS feed，其余字段来自列表页或详情页。

一个 adapter 分两步：`fetch` 通过给它的 `HttpClient` 完成全部 I/O，`parse` 是抓取结果的纯函数。
通用 adapter（`localist`、`drupal_jsonapi`、`ics`、`rss`、`jsonld`、`html_list`、`llm_page`）
由 YAML 条目里的 `params` 驱动；`sources/custom/` 里的定制 adapter 各自处理一个特殊的网站。
这两个包里的每个模块都会自己注册，所以加一个 adapter 不需要改任何共享文件。adapter 只从自己数据源的
host（`url`、`urls`、`homepage` 和 `params.extra_hosts`）抓取，所以页面上的链接没法把请求引到第三方。

礼貌客户端（`http.py`）遵守 robots.txt，包括 `Crawl-delay`，对同一个 host 的两次请求之间至少间隔一秒，
从上一次请求结束时算起。对同一个 host 的请求严格串行，共用 HTTP 缓存目录的不同进程之间也是如此。
每个 host 每次运行最多 20 个请求，robots.txt 另有同样多的预算。robots.txt 取不到的 origin，
在这次运行剩下的时间里都会被跳过。在 `ignore_robots` 里列出某个 host 的数据源，拿到的是共享客户端的
一个视图（`PoliteClient.ignoring_robots_on`），它不遵守这个 host 的 `Disallow` 规则；其他规则照旧，
别的数据源也照旧遵守。客户端只访问公开地址：先解析域名，连接只连到检查过的那个地址。

离线时，同样的 adapter 按 URL 读取录制的 fixture（`tests/fixtures/<source id>/`）。测试和
`seminars run --offline` 用的就是它们，从不访问网络。

## 稳定的标识和去重

日历程序按 UID 区分事件，所以 uid 一旦给出就永远不变（`pipeline/ids.py`）。每个 draft 得到一个
source key，取自数据源能提供的最好的标识：

1. 数据源自己的编号（Localist instance、JSON:API resource、RSS guid、ICS UID）：
   `<source id>:<native id>`；
2. 它自己的详情页地址，前提是这次运行里同一个数据源没有别的 draft 指向这个页面，而且它不是一个
   网站的首页（会议网站的首页是它每一届共用的）；会议的页面只有在地址里写着会议开始那一年时才算，
   因为一个系列会议的页面是一届接一届共用的：
   `<source id>:url:<address>`；
3. 开始时间所在的本地日期，加上第一位讲者，没有讲者就用标题，再没有就用时刻：
   `<source id>:day:<date>:<words>`。会议先用标题。

`data/uid_map.json` 把见过的每个 key 绑定到它的 uid。已知的 key 每次运行都得到同一个 uid；新的 key
得到一个只由 key 本身推出的 uid。报告没变而 key 变了的时候（改期或讲者名字换了写法之后的日期 key、
改名的页面），已存储的 uid 交接给新的 key，所以 uid 不变。feed 里的 UID 是
`<uid>@seminars.fridrichmethod.github.io`。

两个条目算同一个报告（`pipeline/dedup.py`），条件是它们有相同的 uid 或 key；或者开始时间相差不超过
15 分钟，并且标题相似（token set ratio 至少 85）、有共同的讲者、一方的标题写出了另一方的讲者，或者
指向同一个详情页。一个全天条目和一个带时刻的条目，如果其中一个是会议、两者指向同一个详情页，而且
带时刻的那个在另一个的某一天开始、至少 3 小时，也算同一个：这是一个数据源按整天给出、另一个日历按时刻
列出（列一次或者每天列一次）的同一个会议。会议和别的条目只有在规范化后的标题完全相同时才按标题算同一个：
"ICML 2027" 和 "ICLR 2027" 用任何模糊度量都相似，而一个 symposium 的开幕 keynote 也不是这个 symposium
本身。一个带时刻、比会议先结束的条目是会议的一部分，比如这个 keynote，它和会议共用的页面或讲者不会让
两者合并。有几条防护让不相干的报告分开："TBA" 和 "TBA" 永远不算相似；只有一个词的标题不参与比较；
数据源本身被抓取的页面、列出整个系列的页面都不算详情页；同一个数据源里写着不同讲者的两个条目永远不是
同一个报告。合并后的事件保留最先见到的 uid、最好的标题（不是 "TBA"，也不是光秃秃的系列名）、最长的
摘要、全部讲者和 key，细节优先用主办方自己的条目，而不是聚合源（学校总日历、researchseminars.org）的。
条目里有会议的合并事件就是会议：它用那个会议的类型，领头的条目不是会议时也用那个会议的时间；天数更多的
全天条目给出日期，所以一个会议不会因为日历只列了它的第一天而变短。结果不取决于输入的顺序。

## 已存储的事件

`data/events.json` 保存当前窗口内的全部事件，以及设置了 `meeting_horizon` 的数据源在窗口之后的
会议，每行一个，按 uid 排序。已存储的事件再次出现、并且有读者
看得到的改动时，它的 `sequence` 加一，`last_changed` 变成这次运行的时间；两者都写进 feed，所以订阅的
日历会更新这个事件。这次运行没有给出的已存储事件，按已知的情况处理：在窗口开始之前就已经结束的
（没有结束时间的：在窗口开始之前就开始了），移进 `data/archive/<year>.jsonl`，所以一个持续好几周的
会议在进行期间一直保留；数据源被停用或删除的，立刻去掉；已经开始的或者已取消的，原样保留；
所有来源都完整读过却没有它的，记一次缺席，没有再出现、缺席累计 3 次后删除。失败了、返回空、或者没有完整读完的数据源，
对它的事件什么都不说明，所以一个坏掉的网站永远不会删掉报告。一个合并事件的某个来源失败时，
已存储的事件顶替那个缺掉的条目。

## 分类

领域（`math`、`cs`、`ai-ml`、`chem`、`bio`、`physics`、`med`）按这个顺序来自三个地方：数据源写明的
（`default_fields` 和 `config/taxonomy.yaml` 里的院系标签）、规则（每个领域的系列名和带权重的关键词，
在标题、系列和摘要里讲座本身的部分里查找），以及对规则判断不了的事件，LLM 打的标签。词语比较前
去掉重音、不分大小写，只匹配整个词。没有数据源领域的事件，单凭摘要永远得不到领域，因为全校日历上的
摘要常常顺带提到某个领域。发布者自己选的 tag 从不当作事实。没有领域的事件不在范围内，不进 feed；
运行报告里列出了它们。

精选是 `config/interests.yaml` 给出的分数：关键词按出现的位置加权，列出的系列和讲者有加分，排除的词扣分。
理由和事件一起存储，在 feed、网站和摘要里显示。

## LLM 层

LLM 只做两件事，没有它 pipeline 也能完整运行：给规则判断不了的事件标注领域，以及读没有结构的页面。

**provider 链**（`llm/resolve.py`、`llm/chain.py`）。默认顺序是 `claude-cli`（Claude 订阅 token）、
`codex-cli`（本机的 ChatGPT 登录；只有写进 `LLM_PROVIDERS` 才用，从不在 GitHub Actions 里用，而且只用来
打标签）、`anthropic`、`openai`、`openai-compat`（API key）。配置好的 provider 才参与：设置了凭证，
API 类的还要指定模型（`openai-compat` 还要有地址）。`LLM_PROVIDERS` 可以调整顺序和选择。
请求发给第一个仍然可用的 provider；失败的 provider（凭证、额度、限流、超时、网络）
在这次运行剩下的时间里被跳过，最后一个也失败之后，运行只靠规则继续。一次运行的第一个请求就是对 provider 链的探测；一个请求都没发的运行会发一次探测，所以凭证过期
在没有新事件的日子也会出现在健康记录里。一次运行最多发 `max_calls_per_run` 个请求（60），
不发长于 `max_input_chars` 的 prompt。

**打标签**（`classify/llm_tagger.py`）。数据源写明了领域的事件成批询问；没有的事件单独一个请求，
所以决定它领域的那个请求里没有别的事件。答案是封闭词表（领域 id，以及"是不是报告"的是或否），
按 schema 校验，所以页面文字没法让它说别的东西。

**读页面**（`sources/generic/llm_page.py`）。页面先变成可见文字，链接换成带编号的标记，再分段发出，
每段夹在页面拼不出来的标记行之间。答案里的每个值都必须出现在页面文字里（`llm/grounding.py`），
保留的是页面上的原文，从不是模型的措辞；链接是标记编号，日期和时间必须在文字里出现。

**存储的答案**（`llm/cache.py`）。每个用到的答案都存在 `data/llm_cache/<2 hex digits>/<key>.json`。
key 由 prompt 版本、任务和输入文字组成，模型和 provider 都不在其中，所以换凭证或换模型不会重新询问。
一个文件保存答案和它最后一次被使用的 UTC 日期；只有这个日期早于 30 天时才改写，`cache_ttl_days`（180）
天没有用到的条目被清掉。一个 provider 都没有的时候，存储的答案照样生效，只是不再询问新的。读页面存下的
答案里没有隐私数据和 secret：答案做 grounding 之前，页面文字已经清理过，清理不干净的条目换成空的占位。

## 输出

- **Feed**（`outputs/ics.py`）：九个日历，`all`、`picks` 和每个领域一个。有具体时间的事件按设置里的时区
  写出，VTIMEZONE 里每次 UTC 偏移的变化各占一个子组件；已取消的报告保留，带 `STATUS:CANCELLED`。
  文件内容只取决于事件，所以只有事件真的变了，日历才看到变化。`seminars validate` 用自己的校验器
  检查每个 feed。
- **网站**（`site/`、`outputs/site_data.py`）：不需要构建步骤的静态 HTML 和 JavaScript，读
  `events.json` 和 `status.json`；中英文文案来自 `locales/`。所有从网上抓来的文字都会转义。
  `seminars build-site` 只根据已经提交的数据组装 `public/`。
- **选中报告的下载**（`site/calendar.js`）：读者在列表里勾选的报告是页面状态，只保存在内存里，
  从不写进地址栏或浏览器存储。列表上方的选择栏是一个 region landmark；选中了报告而选择栏向上滚出屏幕时
  （由 IntersectionObserver 判断，它观察的范围一直延伸到屏幕下方很远处，所以还在屏幕下方的选择栏算作在屏幕上），
  列表底部的一条横栏显示第二个下载按钮，并以纯文本重复选择栏的状态（选中的数量，以及其中有几个被筛选条件隐藏）；
  文件正在准备、没能生成或者少了报告时，横栏里还以纯文本显示同一条提示（读屏软件只播报选择栏里的状态和提示）。
  横栏里的提示出现在数量和按钮的上方，所以按钮留在按下时的位置：横栏贴在屏幕底部时向上长高、盖住列表，
  停在最后一场报告下面时页面按提示的高度滚动。
  选择栏里关于一次性副本的说明链接到订阅面板；点这个链接会把焦点移到订阅面板的标题，并在浏览历史里添加一条地址不变的记录，
  所以按"返回"会回到列表；"返回"和"前进"会显示各条记录地址里的筛选条件。
  直接打开带 `#subscribe` 的地址（例如在新标签页中打开这个链接）时，页面在列表画好之后同样显示订阅面板，随后把片段从地址里去掉。
  下载时，页面从自己的网站取回 `feeds/all.ics`（`events.json` 里 feed
  `all` 的路径），把选中报告的 VEVENT 块按 feed 里的顺序原样复制进一个日历，连同 feed 的全部 VTIMEZONE
  块和基本的头部（`VERSION`、`PRODID`，以及存在时的 `CALSCALE`、`METHOD` 和 `X-WR-TIMEZONE`），
  但不带只有订阅才需要的头部属性（`X-WR-CALNAME`、`X-WR-CALDESC`、`REFRESH-INTERVAL`、
  `X-PUBLISHED-TTL`）。一个报告对应的 VEVENT，是 UID 在最后一个 `@` 之前的部分等于这个报告 uid 的那个。
  这个日历通过 Blob 存成 `seminars-selected.ics`；选中的报告有不在 feed 里的，就略过并计数；feed 取不到、
  读不了，或者里面没有任何一个选中的报告时，不生成文件。JavaScript 里不写任何 ICS，后端和 `events.json` 也没有专门为下载准备的东西：
  文件取自 `outputs/ics.py` 写出的 feed。
- **摘要**（`outputs/digest.py`）：一周（周一到周日）的报告，先列精选，然后每个领域一张表，再列上一期
  以来新增的报告和数据源的状态，每个标签都有英文和中文。不保存任何状态："新增"从上一期摘要 issue
  打开的时间算起，开 issue 之前先找有没有本周标题的 issue。
- **健康**（`outputs/health.py`）：`data/health.json` 保存每个数据源和 provider 最近 30 次运行。
  连续 3 次失败、给出过报告之后连续 3 次为空、或者连续 3 次没有完整读完的数据源，会有一个
  `source-broken` issue，恢复后关闭。连续 2 次失败的 LLM provider 会有一个 `llm-auth` issue，
  写明要换哪个 secret。

## 隐私和 secret

`data/` 和网站都是公开的。所以规范化时从每段文字里去掉会议密码和口令（链接里的和写出来的）、邮箱地址
和电话号码，会议链接去掉凭证后保留，带凭证的直播地址整个丢掉。这些规则宁可多删。secret 扫描
（GitHub、Anthropic 和 OpenAI 的 token、bearer token、JSON web token、私钥，以及进程拿到的每个凭证的
原值）把页面上出现的这类内容替换掉，并在一次运行要发布它们时让运行停下，exit status 3。
录制的 fixture 在第一次提交之前也遵守同样的规则（`tests/fixtures/README.md`）。

## GitHub workflow

每个 action 都固定到某个 release 的 commit SHA，Claude Code CLI 固定到 `config/llm.yaml` 里记录的版本。
能看到凭证的 workflow 从不为 pull request 运行。

**`ci.yml`**：push 到 `main`、pull request 和手动触发时运行。只读 token，没有任何凭证，checkout 不保留
token。两个 job：

- `checks`：`uv sync --locked`、`scripts/check_ascii.py`、ruff、格式检查、pyright、带覆盖率的测试
  （离线）、actionlint；
- `browser`：网站在 Chromium 里的 Playwright 冒烟测试。

**`update.yml`**：每天太平洋时间 04:23 运行，10:23 补跑一次，也可以手动触发，模式是 `full` 或
`llm-smoke`。定时运行带 `--skip-if-done-today`，所以当天已经成功跑过时，补跑什么都不做。所有运行共用
一个 concurrency group，所以两次运行从不同时进行。正在进行的运行从不取消；GitHub 在每个 group 里只保留
一个等待中的运行，所以较新的运行会取代仍在等待的那一个。顶层 token 是只读的，每个 job 只拿到它需要的
权限：

| Job | 权限 | 做什么 |
|---|---|---|
| `fetch` | `contents: read`、`issues: write` | 带着 LLM 凭证运行 pipeline，校验，把 `data/` 作为 artifact 上传 |
| `commit` | `contents: write` | 以 `github-actions[bot]` 的身份提交这个 artifact；不安装任何东西，只运行 git |
| `build` | 只读 | 从 `main` 的新 tip 构建网站并校验 |
| `deploy` | `pages: write`、`id-token: write` | 把网站部署到 GitHub Pages |
| `outage` | 只读 | 一次运行把自己记录为失败之后，让 workflow run 失败 |
| `llm-smoke` | `contents: read` | 探测每个已配置的 provider；不提交、不部署 |

LLM 凭证只交给运行 pipeline 的那一步和探活的那一步。项目在单独的一步里安装，早于任何拿着凭证的步骤；
能 push 的 job 不运行项目的任何代码。

**`digest.yml`**：每周一太平洋时间 07:07 运行，周二补跑一次，也可以手动触发（带 `dry_run` 时只打印摘要）。
一个 job，权限是 `contents: read` 和 `issues: write`；它读已经提交的数据，不抓取，也不用 LLM。

## 已知限制

- **没有收集的数据源。** Bio-X 对所有自动化请求都返回 HTTP 403（浏览器验证），所以它的数据源停用了。
  Information Theory Forum 停用了：它几乎不再活动，列表里也没有年份。SAIL 的活动日历自 2023 年起
  没有报告，Theory Seminar 目前不定期举办（它页面上的其他日历属于已从各自页面收录的系列）。
  SLAC Advanced Instrumentation Seminars 和斯坦福代数几何 seminar 已停办，SUNCAT 只列内部组会。
  状态页会写明每个数据源停用的原因。Arc Institute 没有公开的 seminar 日历。Bioengineering 和 Computer Science 的院系网站停用了，因为上面的活动都是从学校总日历导入的
  （Computer Science 还有 advising session），而总日历本来就在收集。七个领域以外的报告（比如地球与行星科学、人文）按设计不在范围内。
- **GitHub 的 runner 可能连不上的 host。** DBDS workshop 和 NLP seminar 的 robots.txt 从 GitHub 的
  runner 访问可能超时，从 Stanford 的网络访问则正常。robots.txt 连不上时，这个数据源在那次运行里算失败；
  连续 3 次这样的运行之后，这个数据源会有一个 `source-broken` issue。
- **从自由文本里读讲者和单位**以精度优先：宁可缺一位讲者，也不给错的。能读的写法，以及不给讲者或给错
  讲者的那些很窄的情况，列在 `pipeline/title_speakers.py` 的 docstring 和
  `config/sources.d/stanford-sites.yaml` 里 `sites-systemx` 的注释中。
- **没有 LLM 时的领域标注**只靠规则。学校总日历上七个领域内的报告，规则能认出大约 75%；
  开了 LLM 打标签时，测得的比例大约是 96%。规则判断不了的报告，在 LLM 给它打上标签之前不在范围内。
- **LLM 的上限。** 一次运行最多发 `max_calls_per_run`（60）个请求，规则判断不了的事件每个单独占一个
  请求，所以空的缓存要几次运行才能填满。
- **没有编号的报告可能重复。** 数据源列出时既没有编号、也没有单独页面的报告，按日期、人和标题来辨认。
  这样的报告如果在同一次更新里既改了时间又改了标题或讲者，或者时间移动超过 60 天，或者同一位讲者的
  两场相似报告同时改动，日历里最多会有两天显示两次。
- **回来的报告。** 一个离开了 feed 又回来的报告保留原来的 uid，但从 `SEQUENCE:0` 重新开始；日历程序
  按 UID 刷新订阅的事件。
- **有文档写明边界的启发式规则。** 隐私规则会删掉一些不是地址的文字（`.org` host 上的模块路径），
  也会留下少数可能是地址的；去重规则在同一个数据源同一时间的两场报告里有一场还没写讲者时，可能在一次
  运行里把它们合成一个。`pipeline/privacy.py` 和 `pipeline/dedup.py` 的 docstring 列出了这些情况。
  secret 扫描只把看起来是生成出来的通用 token 当作 secret，所以只由字母组成的长 token 可能漏过；
  进程自己的凭证按原值精确匹配。
- **需要代理的网络。** 联网客户端不读环境里的代理和证书设置，直接连接，所以联网运行需要一个不强制走代理的网络。
