[English](ADDING_A_SOURCE.md) | 中文

# 添加数据源

一个数据源就是一个列出报告的公开日历、feed 或网页。如果它的格式属于通用格式之一，添加它只需要一条
YAML；网站结构特殊时，再加一个小的 adapter 模块。无论哪种，都还需要录制这个数据源的页面
（fixture）并写测试，因为测试和 `seminars run --offline` 从不访问网络。adapter 之后的一次运行
怎样处理一个数据源，见 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)。

## 开始之前

- 只用公开页面：不读需要登录的页面，不读邮件列表，也不收集公开页面以外的个人信息。
- 所有请求都走项目自己的礼貌 HTTP 客户端。它遵守 robots.txt（包括 `Crawl-delay`），
  对同一个 host 的两次请求之间至少间隔一秒，对每个 host 一次只发一个请求，并限制每个 host
  每次运行的请求数。不要用 curl、wget 或浏览器脚本访问数据源网站。
- 如果项目所有者决定收录一个 robots.txt 禁止抓取的公开页面，就在该数据源的 `ignore_robots`
  里列出这个 host，并用注释写明原因。只有这个 host 的 `Disallow` 规则被搁置，而且只对这个
  数据源有效：`Crawl-delay`、请求间隔和请求数上限照旧。
- 按下面的顺序选择抽取方式：结构化 API（Localist、Drupal JSON:API）、feed（ICS、RSS）、
  页面里嵌的 JSON-LD、HTML 列表，其他办法都不行时才用 LLM 页面抽取。

## 1. 先看看网站

先让格式嗅探看一眼。它只抓取这个地址一次，靠便宜的特征认出 Localist、Drupal JSON:API、
RSS、ICS 和 JSON-LD，并打印一条建议的 YAML：

```
uv run seminars add-source "https://www.example.edu/talks"
```

更多的试探请求用 `fetch`。它走同一个礼貌客户端，显示每个响应的状态、跳转后的地址、
内容类型和大小，不写 fixture。加上 `--save-dir` 时，它还会把响应内容存进一个 git 忽略的
临时目录：

```
uv run python -m seminars.fixtures fetch --save-dir .scratch/example \
    "https://www.example.edu/talks" \
    "https://www.example.edu/robots.txt"
```

有些网站要求两次请求之间间隔 30 秒，所以同一个 host 的地址放进同一条命令，并做好等一会儿的准备。
在 shell 里地址一定要加引号。

## 2. 用 YAML 描述数据源

数据源写在 `config/sources.d/` 里，每组一个文件。数据源的 id 由小写单词和连字符组成，
以所在组的前缀开头，同时也是它的 fixture 目录名 `tests/fixtures/<id>/`。

| 组 | 文件 | id 前缀 | 放什么 |
|---|---|---|---|
| `localist` | `localist.yaml` | `localist-` | Stanford Events，学校的 Localist 日历 |
| `stanford-sites` | `stanford-sites.yaml` | `sites-` | 建在 Stanford Sites（Drupal）上的院系网站 |
| `stanford-custom` | `stanford-custom.yaml` | `custom-` | 有自己页面的 Stanford 实验室和中心 |
| `bayarea` | `bayarea.yaml` | `bayarea-` | SLAC、CZ Biohub 和其他湾区研究机构 |
| `researchseminars` | `researchseminars.yaml` | `rsorg-` | researchseminars.org 上的系列报告 |
| `conferences` | `conferences.yaml` | `conf-` | 会议登记表：人工维护的会议和 workshop |

使用通用 adapter 的一条记录：

```yaml
sources:
  - id: sites-chemistry
    name: Stanford Chemistry
    group: stanford-sites
    adapter: drupal_jsonapi
    url: https://chemistry.stanford.edu/jsonapi/node/hs_event
    institution: stanford
    host_org: Stanford Chemistry
    default_fields: [chem]
    params: {page_limit: 50}
    research: {verified: 2026-11-10, sample_events: 12, has_jsonapi: true}
```

- `adapter` 是 adapter 的名字，见第 3 步。
- `url`（或者列表 `urls`）是 adapter 开始抓取的地址。要写网站跳转之后的最终地址：adapter
  只能访问 `url`、`urls` 和 `homepage` 所在的 host，跳转到的 host 算作外部 host。
  数据源确实需要另一个 host 的页面时（例如 `events.stanford.edu` 上的详情页），把那个 host
  写进 `params.extra_hosts`。
- `institution` 取 `stanford`、`slac`、`arc`、`biohub`、`other` 之一。
- `default_fields` 是这个数据源所有报告的领域（`math`、`cs`、`ai-ml`、`chem`、`bio`、
  `physics`、`med`）；各种学科混在一起的日历留空，它的报告由规则标注。
- `params` 放只有 adapter 才读的内容；每个 adapter 在自己模块的 docstring 里说明它的 key。
- `research` 记录调研结果：检查的日期、样本里有多少事件、网站提供哪些格式、它的
  `Crawl-delay`、备注。
- 其他 key：`homepage`、`series`、`default_event_type`、`timezone`、`aggregator`、
  `llm_fallback`、`meeting_horizon`、`ignore_robots`、`enabled`。完整的 schema 和每个 key 的说明在
  `src/seminars/models/source.py` 的 `SourceConfig` 里；未知的 key 会报错。
- `meeting_horizon: true` 让这个数据源的会议（conference，以及跨好几天或者至少 3 小时的
  workshop，即 `Event.is_meeting`）保留到往后 `meetings.days_ahead` 天（365 天，在
  `config/settings.yaml` 里），而不是 `window.days_ahead` 的 120 天；它的报告仍然用平常的窗口。
  标题里写着会议（symposium、conference、summit、workshop、boot camp、summer school 等）、并且是
  全天或者至少 3 小时的 seminar、lecture 或 other 类型事件会变成 conference 或 workshop，因而按上面的
  规则算会议；只有一天的全天 workshop（比如黑客松的某一天）不算。只给提前几个月公布会议的
  数据源设置它，不要给报告日历设置：这个数据源会用更长的窗口抓取，所以按日期范围查询、翻列表页
  或者每个事件读一个详情页的 adapter 会发出更多请求，而每个 host 的请求预算和每个数据源 15 分钟的
  上限都不变。提交之前，对比加和不加这个 key 时 `seminars sources check <id>` 报告的请求数和用时。
- `ignore_robots` 列出这个数据源自己的 host（来自 `url`、`urls`、`homepage` 或
  `params.extra_hosts`），这个数据源不遵守这些 host 的 robots.txt `Disallow` 规则；见“开始之前”。
  列出别的网站的 host 会报错。
- `enabled: false` 停用一个数据源，但保留它的条目。停用数据源或删除它的条目之后，下一次运行时
  它的未来报告会从 feed 和网站上一并去掉：它的 key 从存储的事件里删掉，只由它列出的报告立即去掉，
  其中已经开始的报告移进存档。其他数据源也列出的报告保留那些数据源的 key，只按它们来判断。
- 停用的数据源要在状态页上写明原因：在 `enabled: false` 上方写注释 `# Off: <原因>`，并在
  `locales/en.json` 和 `locales/zh-CN.json` 的 `site.status.disabled_reasons.<id>` 下写给读者看的原因。
  测试（`tests/test_locales.py`）要求每个停用的数据源都有原因、其他数据源都没有，所以数据源重新启用时
  要删掉这条消息。

## 3. 选择 adapter

### 通用 adapter

大多数数据源只需要那条 YAML，因为它们的格式是下面之一：

| Adapter | 适用于 |
|---|---|
| `localist` | events.stanford.edu 的 Localist JSON API |
| `drupal_jsonapi` | Drupal JSON:API（Stanford Sites 模板的事件节点） |
| `ics` | iCalendar feed |
| `rss` | RSS 和 Atom feed，可以同时读每一条的详情页 |
| `jsonld` | 以 JSON-LD 形式嵌有 schema.org `Event` 数据的页面 |
| `html_list` | HTML 列表，用 `params` 里写的 CSS selector 读取 |
| `llm_page` | 完全没有结构的页面：只抓取，由 LLM 页面抽取器读取 |

`llm_page` 依赖 LLM 凭证，是最后的手段。对其他 adapter，`llm_fallback: true` 表示规则从这个
数据源的页面里什么都没读出来、或者读取失败时，允许 LLM 接手。

### 定制 adapter

没有合适的通用 adapter 时，在 `src/seminars/sources/custom/` 下写一个模块。那里的每个模块都会被
自动导入，所以不需要改其他文件。adapter 以所在模块命名，数据源用 `adapter: <模块名>` 选择它：

```python
"""Talks of the Example Lab seminar (https://www.example.edu/talks)."""

from __future__ import annotations

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.httpbase import HttpClient
from seminars.models import EventDraft, RawPayload, SourceConfig, TimeWindow
from seminars.sources.base import ParseError, absolute_url, fetch_urls, register_adapter


def _text(node: LexborNode | None) -> str | None:
    return None if node is None else node.text(strip=True) or None


@register_adapter("example_lab")
class ExampleLabAdapter:
    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        drafts: list[EventDraft] = []
        for document in payload.documents:
            items = LexborHTMLParser(document.text).css("li.talk")
            if not items:
                raise ParseError(f"{source.id}: no talk list in {document.url}")
            for item in items:
                link = item.css_first("a.title")
                when = item.css_first("time")
                if link is None or when is None:
                    continue  # one malformed item: skip it, keep the rest
                try:
                    drafts.append(
                        EventDraft(
                            source_id=source.id,
                            extraction="html",
                            url=absolute_url(document.final_url, link.attributes.get("href")),
                            title=_text(link),
                            start=when.attributes.get("datetime") or when.text(strip=True),
                            speaker_text=_text(item.css_first(".speaker")),
                            location=_text(item.css_first(".location")),
                        )
                    )
                except ValidationError:
                    continue
        return drafts
```

规则如下，完整版在 `src/seminars/sources/base.py` 的 docstring 里：

- `fetch` 负责全部 I/O，只用传进来的 `HttpClient`，离线和联网时请求的地址完全相同。它只访问
  自己数据源的 host：`fetch_urls` 会检查这一点；要跟着链接抓取的 adapter，先用
  `on_source_host` 逐个检查。
- `parse` 是 payload 和数据源配置的纯函数。它照抄数据源的内容，清理文本是 pipeline 的事。
  需要"现在"的时候（例如推断缺失的年份）用 `payload.fetched_at`。
- 时间和链接由 adapter 负责，因为只有它知道格式。不带时区的时间是数据源所在时区的当地时间；
  只有日期、没有时间的是全天事件，它的 `end` 是最后一天（包含这一天）。每个链接都用
  `absolute_url(document.final_url, link)` 变成绝对地址。
- 响应完全看不懂时抛 `ParseError`；单条格式错误的条目跳过即可。违反规则的 draft 在构造时会抛
  `pydantic.ValidationError`，也算单条格式错误。
- 类的构造函数不接受参数，两次调用之间不保留状态。

## 4. 录制 fixture

用礼貌客户端录制 adapter 要抓取的页面：

```
uv run python -m seminars.fixtures record --source-id custom-example-lab \
    "https://www.example.edu/talks"
uv run python -m seminars.fixtures check --source-id custom-example-lab
```

这会写出 `tests/fixtures/<id>/manifest.json`，每个地址一个内容文件。

- 只录 adapter 实际请求的地址，同一个 host 的地址放进同一条命令。每次运行都会变的 query
  参数（例如时间窗口的日期）用 `--volatile-param NAME` 标出来。
- 每个内容文件最大 500 KB。更大的文件手动删减到测试需要的部分，保持格式有效，并在同一目录的
  `README.md` 里写明删了什么。
- 不要手动改 manifest，也不要把 `.scratch/` 里的文件复制进 fixture 目录：要用 record 录制。
- fixture 是原样的快照，保留网站发来的所有字符。
- `record` 和运行时一样，遵循 `--source-id` 对应数据源配置里的 `ignore_robots`。
- 离线运行的时间是所有 fixture 里最晚的录制时间。新的录制会把它往后移，所以要同时更新
  `tests/test_pipeline_offline_integration.py` 里的 `RUN_TIME` 和该测试选用的报告。

所有选项，以及客户端如何等待同时访问同一个 host 的其他进程，见
[tests/fixtures/README.md](../tests/fixtures/README.md)。

## 5. 测试

定制 adapter 有一个测试文件 `tests/test_custom_<模块名>.py`，读取 fixture，检查真实条目的标题、
时间、speaker 和地点：

```python
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from seminars.config import load_sources
from seminars.fixtures import OfflineHttpClient
from seminars.models import TimeWindow
from seminars.sources.base import get_adapter

ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
WINDOW = TimeWindow(
    start=datetime(2026, 11, 1, tzinfo=PACIFIC), end=datetime(2027, 3, 1, tzinfo=PACIFIC)
)


def test_the_list_page_gives_title_time_speaker_and_location(
    offline_http: Callable[..., OfflineHttpClient],
) -> None:
    sources = load_sources(ROOT / "config" / "sources.d")
    source = next(s for s in sources if s.id == "custom-example-lab")
    adapter = get_adapter(source.adapter)
    payload = adapter.fetch(offline_http(source.id), source, window=WINDOW)
    first = adapter.parse(payload, source)[0]
    assert first.title == "Learning to fold"
    assert first.start == datetime(2026, 11, 17, 16, 0)
    assert first.speaker_text == "Jane Doe (MIT)"
    assert first.location == "Clark Center S361"
```

使用通用 adapter 的数据源由那个 adapter 的测试覆盖；用只针对这一个数据源的离线运行做端到端检查。
数据源合并之后，联网检查会显示它在真实网站上的情况：

```
uv run seminars run --offline --source custom-example-lab
uv run seminars sources check custom-example-lab
```

每次运行都记录每个数据源的情况（`data/health.json`、状态页和每周摘要）。一个数据源连续三次运行失败，
或者以前列出过报告、现在连续三次没有报告，或者连续三次没有完整读取（有页面无法获取，或者列表远小于平时），
就会开一个 `source-broken` issue；数据源再次完整读取时，issue 自动关闭。列表远小于平时的情况最多按不完整
算 28 次运行；之后把较小的规模当作通常规模，它不再列出的已存报告开始计为缺失，issue 关闭时附上一条说明这一点的评论。

## 6. 字符集规则

本仓库的所有文件都是不带 BOM 的 UTF-8、LF 换行、末尾有换行，而且几乎全部是可打印的 ASCII：
代码、注释、docstring、YAML、脚本、workflow、测试、提交信息、英文文档和 `locales/en.json`。

- 用 `"`、`'`、`-`、`--`、`...`、`->` 和 `*` 代替弯引号、破折号、省略号字符、箭头和项目符号。
  不用 emoji、连字、不换行空格、零宽字符和全角标点。
- 测试需要的非 ASCII 文本（例如带重音的人名）用转义写：`"Jos\u00e9"`。
- 只有这些文件可以包含其他字符：`*.zh-CN.md` 和 `locales/zh-CN.json`（中文，但不能有 emoji）、
  `data/**`（按数据源原样写的人名，但不能有 emoji、不可见字符和控制字符）、
  `tests/fixtures/**`（原样的录制内容）。
- `scripts/check_ascii.py` 检查每个被 git 跟踪的文件。它作为 pre-commit 钩子和在 CI 里运行；
  自己运行用 `python3 scripts/check_ascii.py`。

## 7. 提 pull request 之前

运行 CI workflow 里的检查：

```
python3 scripts/check_ascii.py
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

提交 YAML 记录、adapter 模块（如果有）、fixture 和测试。不要提交本地运行改写的 `data/`：
它归每日更新管。pull request 只运行 CI workflow，那里没有任何凭证；改动合并之后，新数据源就会
加入每日更新。

## 没有数据源的会议和 workshop

有些会议提前几个月就在没有任何数据源读取的页面上公布：某个学会在 San Francisco 的年会、
某个中心只发一次的 symposium、网站要靠浏览器渲染的研究所的 workshop。会议登记表（registry）
人工维护这些会议。它是 `config/sources.d/conferences.yaml` 里的数据源 `conf-registry`，
adapter 是 `registry`（`src/seminars/sources/custom/registry.py`，它的 docstring 说明每个键）。
这个 adapter 不发任何请求：每个会议是 `params.meetings` 里的一条记录；数据源的 `url` 是这个文件在
GitHub 上的页面，状态页链接到它，但没有任何东西抓取它；也没有 fixture 可录。这个数据源设了
`meeting_horizon: true`，所以它的会议最多提前一年保留（只有一天的 workshop 不算会议，见下面的
`event_type`）；还设了 `allow_empty: true`，所以会议全部结束的登记表不算坏掉的数据源。

只登记没有任何已启用数据源提供的会议：先在 `data/events.json` 和网站上搜它的标题。一条记录只写
一届会议的事实，每一项都取自组织方这一届的页面，别的都不写：不写简介、不写联系方式、不写任何人的
地址，也绝不写按往届推算出来的日期。

```yaml
- id: iclr-2027
  title: "ICLR 2027: The Fifteenth International Conference on Learning Representations"
  series: ICLR
  host_org: International Conference on Learning Representations (ICLR)
  start: 2027-04-26
  end: 2027-04-30
  region: bay-area
  location: Moscone Center, San Francisco, CA
  url: https://iclr.cc/Conferences/2027
  fields: [ai-ml, cs]
  event_type: conference
  status: confirmed
  verified: {date: 2026-11-18, url: "https://iclr.cc/Conferences/2027"}
  milestones:
    - kind: workshop_paper
      date: 2027-02-01
      label: Suggested workshop paper deadline
      url: https://iclr.cc/Conferences/2027/CallForWorkshops
    - kind: notification
      date: 2027-02-26
      time: "23:59"
      zone: AoE
      label: Workshop paper notification deadline
      url: https://iclr.cc/Conferences/2027/CallForWorkshops
```

- `id` 写成 `<series slug>-<year>`，以这一届开始的年份结尾。它是事件的键（`conf-registry:<id>`），
  所以永远不改名：改了名的记录是一个新事件，旧事件要到第三次没有它的运行时才离开。事件的 uid 是
  `conf-registry.<id>`，除非另一个数据源的条目和这条记录合并（见 `url`）：事件保留最先出现的 uid，
  条目比这条记录先存下时是条目的 uid，两个 uid 在同一次运行里出现时是排在前面的那个。
- `short_title`（可选）是这一届在 `deadlines.ics` 和摘要里的名字，一行、最多 40 个字符，比如标题很长的
  workshop 写 `GEM at ICLR 2027`。不写时，名字是 `series` 加上这一届开始的年份（`ICLR 2027`），`series`
  本身已经含有四位数的年份时则不加年份。会因此同名的几届（比如同一个研究所同一年的几个 workshop）改用各自的
  `title` 作名字；标题很长时，给这样的一届写一个 `short_title`。有 milestone 的两届如果仍然同名（比如两个相同的
  `short_title`），`tests/test_custom_registry_deadlines.py` 会失败。
- `start` 和 `end` 是当地日期，`end` 包含在内。只有 `region: bay-area` 的记录会成为事件；
  `world` 的记录只带它的 milestone。
- `event_type` 写 `conference` 或 `workshop`。只有一天（`start` 等于 `end`）的 `workshop` 不算会议
  （`Event.is_meeting`），就像黑客松的某一天不算一样：运行把它当作类型为 workshop 的报告，等它进入
  `window.days_ahead` 的 120 天之内才保留；它在 `all.ics` 和它的领域的 feed 里，不在 `conferences.ics`
  里，在网站和摘要里列在报告当中。只有一天的 `conference` 算会议。不论哪种，它的 milestone 都是
  `deadlines.ics` 里的截止日期。
- `url` 是组织方这一届的页面：这一届自己的网站，或者公布它的系列或项目的页面（记录的 notes 会
  说明）。另一个数据源列出同一个会议时，它的条目只要指向同一个页面、给出具体时间、在这条记录的
  某一天开始、并且至少 3 小时长，就和这条记录合并成一个事件（`pipeline/dedup.py`），不必覆盖整个会议。开始时间和
  这条记录相差不超过 15 分钟的条目也会合并（`dedup.max_start_difference_minutes`；这条记录从第一天的
  午夜开始，那一天的全天条目就是这样）：在同一个页面上，或者标题和这条记录相同（不计大小写、重音
  和标点）时在任何页面上。否则这个会议会出现两次：指向别的页面（Localist 日历链接到它自己的事件
  页面）、标题或开始时间又不同的条目，不到 3 小时的条目，较晚某一天的全天条目。`url` 是网站首页
  （`https://2027.naacl.org/`）时，首页不算详情页，只有第一天、标题和这条记录相同的全天条目会和它合并。
- `fields` 是这个会议肯定属于的领域。领域标注器保留它们，并可能加上标题提到的领域。
- `location` 写页面给出的场地和城市。页面还没写时填 `To be announced`，事件会显示为没有地点。
- `status: tentative` 表示只是预告了日期（save the date），还没有场地和日程。
- `verified` 写明事实最后一次是在哪个页面、哪一天核对的。每学期开始时把每条记录重新核对一遍：核对
  组织方页面上的事实，并查看现在是否已有数据源列出这个会议（在 `data/events.json` 里搜它的标题）。
  一旦有数据源列出这个会议，就删掉这条记录。一届会议在 120 天内开始、但上次核对已超过 90 天，或者
  前面没有任何湾区会议时，`tests/test_custom_registry.py` 会失败，并列出需要查看的记录。
- `milestones` 是读者需要据此行动的日期，不晚于这一届的最后一天：截止日期、开放日期、通知日期，带上
  页面给出的时间和时区（`AoE`、`PT`、`MT`、`ET`、`UTC` 或 IANA 时区名）。只有时区没有时间时，时区
  说的是那一天，比如 1 月 22 日 `AoE` 截止。label 用我们自己的几个词。事件的描述按日期列出这些
  milestone；过去的 milestone 等运行窗口（向前 14 天）越过它之后才会从描述里去掉。每个 milestone
  也是 `deadlines.ics` 里的一个截止日期（见下文）。它的 `url`（可选）是写明它的页面，在这个页面不是
  `verified` 的页面时填写。
- YAML 把不加引号的 `on`、`off`、`yes`、`no` 读成布尔值，把不加引号的 `23:59` 读成数字：所以核对日期的键叫
  `date`，qualifier 写 `exact` 或 `by`，时间要加引号。

已启用的登记表数据源里的每个 milestone，不论记录是 `bay-area` 还是 `world`，都是 feed `deadlines.ics`
和摘要里的一个截止日期（`seminars.pipeline.deadlines`）。每次运行都从配置里读取它们，所以改动会出现在
下一次运行里，不管那次运行抓取哪些数据源。整理它们的规则：

- 每个 milestone 都取自这一届组织方的某个页面，带上页面写明的时间和时区，别的都不写：页面没写的日期
  不录，按往届推算出来的日期也不录。这个页面就是 `verified` 写的那个页面，除非 milestone 在 `url` 里
  写了它自己的页面（征稿通知、摘要页面、合办方的页面）。feed 和摘要都链接这个页面，并提醒读者先查看它。
  每学期开始时在各自的页面上和其他事实一起核对 milestone。
- 不录机器学习和计算机科学会议主会（NeurIPS、ICML、ICLR、ACL、KDD 等）的投稿截止日期：README 把这些留给
  ccfddl 的日历。这类会议的 workshop 如果有自己的页面和截止日期，可以登记（`event_type: workshop`），会议
  本身的 workshop 截止日期也可以录（`workshop_paper`）。一届不是 workshop、`series` 或 `id` 里有这类会议
  名字的会议，如果有 `paper` 或 `abstract` 类型的 milestone，`tests/test_custom_registry_deadlines.py` 会失败。
- `world` 的记录只为它的 milestone 而存在，永远不会成为事件：只有当湾区以外的会议有这里的读者需要
  据此行动的截止日期时，才登记它。
- 截止日期的 uid 是 `dl.<id>.<kind>`；一届会议里同一类型的第 n 个 milestone 后面加 `-<n>`
  （`-2`、`-3`），按日期计数。同一类型较晚的一轮要加在较早的几轮后面；只要还列着较晚的一轮，较早的就
  留在记录里：在同类型的另一个 milestone 之前插入或删除一个 milestone，会让它后面每一个的 uid 都变，
  日历里看到的就是删掉了一个截止日期、又新增了一个。
- feed 显示的事实（日期、时间、时区、qualifier、label，这一届的名字、标题、日期、地点或页面，核对的日期，
  milestone 的 `url`，没有 `url` 时核对的页面）有改动，订阅的日历里的截止日期就会更新：它的 `SEQUENCE` 加一。只改
  `series`（有 `short_title` 时）、`region` 或 `fields`，改动会存下来，但哪里都不显示。删掉的 milestone
  在下一次运行时离开 `deadlines.ics`，过去的 milestone 在它的日期之后 14 天离开。

结束了的会议会自己离开登记表的列表，所以删掉它的记录不会改变什么。还没开始的会议的记录删掉后，在第三次
没有它的运行时从 feed 里移除，和数据源不再列出的报告一样；已经开始的一届会议会一直保留到结束。有两种情况
会让删掉的记录保留得更久：

- 登记表一个会议都没列出的运行对它的事件不下任何结论，所以这时删掉的记录会保留到会议结束。
- 每个数据源的每次运行都会和它列表的通常大小比较（`seminars.pipeline.collect`）。一次删掉一半以上、
  并且至少五条列出的记录，看起来就像列表丢了一部分：删掉的事件会保留，第三次这样的运行会开一个
  `source-broken` issue，两者一直持续到第 29 次运行把变小的列表当作通常大小并关闭 issue；删掉的事件在
  第 31 次运行时离开。一次最多删掉列出的记录的一半，或者少于五条，并且隔六次运行再删下一批。

数据源和登记的会议的地址都不能在掠夺性或低质量的会议聚合网站上（waset.org、wikicfp.com、10times.com
之类；`tests/test_source_denylist.py` 检查这一点）。
