# 安装到本地 Codex CLI

本目录是自包含的 Agent Skill。需要 Python 3.10 或更新版本；所附工具只使用 Python 标准库。它不会内置或下载《足球财富》原文。

把整个 `football-odds-prematch` 目录放到：

```text
~/.agents/skills/football-odds-prematch/
```

确认 `~/.agents/skills/football-odds-prematch/SKILL.md` 存在，然后开启新的 Codex CLI 会话并输入：

```text
$football-odds-prematch 分析本周末五大联赛，赛前一小时截断，先给我数据覆盖审计和胜平负概率。
```

也可从 shell 启动：

```bash
codex '$football-odds-prematch 分析这些比赛，先核实盘口原始时间和赛前信息。'
```

该安装位置与 `$skill-name` 调用语法已从 Codex CLI `0.159.2` 对应的官方源码核对；其他版本若发现 Skill 路径不同，按本机 Codex 的 Skill 文档调整。`references/` 记录字段、数据来源和回测口径；`scripts/` 提供原页留存、规范化截点、概率分析与赛后评分工具；`examples/` 是 CSV 表头模板。

本 Skill 不保证能从 Titan 自动解析所有比赛。`capture_titan.py` 留存公开页面原文，仍需核查页面结构并把赔率转成规范 CSV；遇站点失败应记录缺失，不绕过限制或编造数据。正式预测应在赛前留存原始页和预测散列。
