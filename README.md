# Football Odds Prematch Skill

This repository hosts a Codex CLI skill for auditable pre-match football odds analysis. The skill is in [football-odds-prematch](football-odds-prematch/).

它说明如何收集并核验赛程、基本面、欧赔、亚盘、大小球及可核验的必发量价，在赛前固定时点计算胜平负概率，并隔离赛果做盲评。目录中还包含原网页留存、截点整理、预测与赛后评分脚本，以及 CSV 表头示例。内容是研究流程，不含《足球财富》原文。

本地 Codex CLI 安装：

```bash
git clone https://github.com/15757115515chen-design/chen.git
mkdir -p "$HOME/.agents/skills"
cp -R chen/football-odds-prematch "$HOME/.agents/skills/"
```

确认 `$HOME/.agents/skills/football-odds-prematch/SKILL.md` 存在，然后开启新的 Codex CLI 会话并输入：

```text
$football-odds-prematch 分析这场比赛，先核验赛前一小时可见的盘口和数据覆盖情况。
```

已用先前的 40 场隔离输入复算：40/40 场覆盖，最高概率项命中 25/40；候选规则平均 log loss 0.88806，赔率基线 0.88746。此前的 7.55% 等额赔率收益率只是赛后补算的探索性假设，不是可验证的策略收益。
