# GitHub Scout · GitHub 项目搜集器

一个可复用的 Codex skill：按星数、类型、语言等条件发现项目，先检查 README 是否贴题，再比较实际用途与价值。

普通搜索默认至少 **1,000 Stars**，**不限制最后更新时间**。稳定、成熟的工具可以保留，兼容性与维护情况单独核查。

## 在聊天中使用

安装 skill 后，直接描述需求：

> 用 $github-scout 找一些 1000 星以上的动物 3D 建模项目，兼顾手工建模软件和图生 3D，成熟但很久没更新的也保留。

> 用 $github-scout 找游戏 UI 设计与实现项目，优先 Unity，至少 1000 星；先确认 README 贴题，再给推荐理由。

默认自动发现 skill，也支持显式 `$github-scout` 调用。最终清单由 agent 阅读官方资料后整理；脚本初筛不能替代语义判断或效果实测。

## 安装

要求 Python 3.10+。脚本仅用标准库；GitHub CLI 为可选项，可复用已有登录态。

将本仓库放入 Codex 的 `skills/github-scout` 目录。Windows PowerShell 示例：

```powershell
$scoutSkillParent = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex/skills' }
git clone https://github.com/realchenchenluo/github-scout.git (Join-Path $scoutSkillParent 'github-scout')
```

已有同名技能时，更新其内容即可。技能入口为 [SKILL.md](SKILL.md)，可修改的默认值和专业词在 [config.json](config.json)。

## 直接运行

在仓库目录执行：

```powershell
python scripts/search_repos.py --category animal-modeling --min-stars 1000 --output outputs/animals.json
python scripts/search_repos.py --category game-ui --min-stars 1000 --output outputs/game-ui.json
python scripts/search_repos.py --category game-ui --active-days 365 --dry-run
```

自然语言调用时由 agent 传入会话日期；直接运行时默认使用主机日期，也可 `--as-of YYYY-MM-DD` 指定。

内置通用检索分类：`ai`、`devtools`、`automation`、`self-hosted`、`data`、`learning`。内置专业检索与相关性规则：`animal-modeling`、`game-ui`。通用分类尚无专用规则时，候选进入 `review_queue`，由 agent 语义判断后形成最终推荐。

## 怎样减少跑题

1. 用专业短语、同义词和用途词检索，避免仅凭 animal 或 game + UI。
2. 先剔除主要用途明确不符的元数据，例如动物医疗、实体机器人、书单或服务器管理面板。
3. 读取 README，检查项目主要用途与功能段落；保留来源链接、版本和证据。
4. 将直接相关、可借用的通用工具、待复核和明确无关分开。相关性优先于星数，之后再比较价值。

脚本采用保守的关键词规则，可能漏掉特殊术语，也不能理解所有语义。未读 README、证据不足、预算耗尽或读取失败的项目不会自动进入初选。保留原始候选和排除原因，方便复核。

默认每条查询处理最多 50 条候选、读取最多 30 份 README。可用 `--per-query`、`--readme-budget` 调整。未更新不会自动淘汰，也不会自动证明“成熟”；仍需核对文档、兼容性、已知问题和许可证。

## 输出与认证

输出 JSON 包含 `raw_repositories`、`repositories`、`shortlist`、`rejected`、`review_queue` 和证据。最终 Markdown 推荐报告由 agent 整理。`--seen` 可排除上次已出现的仓库。字段与自定义规则详见 [参数说明](references/search-notes.md)。

依次使用已有 `GH_TOKEN`、`GITHUB_TOKEN`、`gh` 登录态；没有则匿名访问。不需要将令牌写入配置文件。遇限流保存已有结果并停止当前批次，不密集重试。

## 验证

```powershell
python -B -m unittest discover -s tests -v
```

测试覆盖硬条件边界、分页去重、成熟项目保留、书单/医疗/机器人排除、README 必需、相关性先于星数、读取预算与限流恢复。测试不访问 GitHub。
