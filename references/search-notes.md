# 参数、输出与检索边界

## 筛选与排序

配置优先级：`defaults` → 选中的 `presets` → 命令行参数。`active_days=0` 是普通搜索的默认值，不加 `pushed:` 条件。`--active-days 365` 仍可显式启用一年内有推送的硬筛选；`--active-days 0` 关闭它。最后更新时间仅供判断，不会自动把旧项目变为成熟项目。

`--query` 可重复，取并集，每个查询叠加共同的硬条件；它替代类别查询种子，但不会禁用类别对应的相关性检查。`--profile` 可单独指定相关性规则，默认 `auto` 选择与类别同名的 profile。多个 `--topic` 表示 AND。多个语言或许可证可分别运行、合并去重。

`--per-query` 限制每条查询纳入处理的原始条数（1–1000）；`--readme-budget` 限制本次 README 请求数（默认 30）；`--limit` 是初选上限。`--sort stars|updated|best-match` 决定检索顺序，以及同一相关性层内的排序。初选中直接相关始终先于通用工具；最终由 agent 阅读资料并按用户的场景或显式排序要求整理。

`--license` 接受具体 SPDX ID 或 GitHub license key，例如 `mit`、`apache-2.0`；不接受许可证家族简称 `gpl`。语言筛选指主要编程语言，不能当作界面语言或中文文档筛选。

## 相关性配置

`categories` 为分类到查询种子列表的映射。`relevance_profiles` 按类别定义：

- `direct_groups`：项目全名+描述，或清理后的 README 开头 600 字符，必须在同一来源满足每组至少一个专业词，建立主要用途依据。
- `readme_groups`：README 中 480 字符的短段必须同时满足每组至少一个词，建立功能证据。
- `adjacent_phrases`：README 开头明确描述的通用能力，只标为间接相关。
- `exclude_metadata`：当前领域中明确跑题的主要用途短语；仅检查仓库名与描述的前 1,200 字符，不因文档里的偶然提及而排除。
- `review_only_metadata`：泛用引擎、桌面框架、资源合集等较宽的主要用途，放入待复核队列，不因目录中的 UI/动物词而自动判为专用工具。这些候选不消耗自动 README 检查预算。

英文词采用词边界匹配，避免 `cat` 命中 `category`；中文短语字面匹配。代码块、徽章、图片和 URL 不充当贴题证据。脚本将 README 最多前 200,000 字符用于清理，最多前 60,000 字符用于证据检索；它并不理解所有语义、否定句或新的专业表达。规则未覆盖、证据位置较后或未知类型的项目进入待复核队列，不能把这类保守漏检称为真正不相关。由 agent 阅读 README 与官方文档做最终判断。

新增专业领域应在临时配置中增加少量准确查询和对应概念组，不把动物/游戏领域的排除词套用到其他领域。不要为命中少数仓库而加入无关的全局黑名单。

## JSON schema v2

| 字段 | 含义 |
| --- | --- |
| `raw_repositories` | 检索后满足硬条件的全部去重原始候选 |
| `repositories` | 读取 README 后，保守初筛为直接或间接相关的候选 |
| `shortlist` | 相关性优先的初选；需继续核查价值，不是最终推荐 |
| `rejected` | 明确跑题的候选与原因 |
| `review_queue` | 未读、缺 README、读取失败或证据不足，需人工/agent 语义复核 |
| `relevance` | `direct`、`adjacent`、`needs_review` 或 `rejected`，含理由与证据片段 |
| `readme_url` / `readme_sha` | 本次检查的官方文件来源与版本 |
| `activity` | 距离推送天数与时间标签；不是成熟度或维护质量评分 |
| `queries` / `warnings` | 实际查询、API 匹配数、读取条数、失败与覆盖限制 |
| `review_summary` | 原始、初筛通过、淘汰、待复核与 README 请求数量 |

无 profile 时 `repositories`/`shortlist` 为空，候选与已抓取的 README 摘录在 `review_queue`；agent 继续阅读并语义筛选。`--seen` 兼容旧 JSON，v2 优先读取 `raw_repositories`，也可用 `["owner/repo", ...]`。不自动修改历史。

## API 限制与恢复

仓库搜索每页最多 100 条，单条搜索最多访问 1,000 条，且受索引范围与超时约束。脚本记录 API `incomplete_results` 和预算截断，不声称搜遍 GitHub。需要更大覆盖时，可按不重叠星数/创建日期范围拆分。

搜索或 README 请求 401/403/429 时停止后续该类请求并保留结果，记录服务端恢复头。搜索失败、API 标记不完整、非 404 的 README 读取错误使 `status=partial`、退出码 2；README 404 标为待确认，不当作项目不相关。`status=ok` 仅表示计划请求完成，仍应查看截断与待复核警告。

## 官方依据

- [仓库搜索语法](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
- [搜索 API 与限制](https://docs.github.com/en/rest/search/search#search-repositories)
- [读取 README](https://docs.github.com/en/rest/repos/contents#get-a-repository-readme)
- [API 版本](https://docs.github.com/en/rest/about-the-rest-api/api-versions)：固定 `2026-03-10`，升级时重新核实接口。
