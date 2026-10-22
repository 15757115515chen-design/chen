# 数据契约与赛前隔离

本 Skill 的分析输入是 UTF-8 CSV，时间统一写为**北京时间 `YYYY-MM-DD HH:MM`**；在取得来源数据时，先明确源时区并换算，不能凭服务器时区猜测。目标比赛的比分只放在独立的评估目录。所有表按 `match_id` 唯一连接，并核对联赛、主客队和开球时间。

## 1. `fixtures.csv`：比赛清单，必需

| 字段 | 含义 |
| --- | --- |
| `match_id` | 来源站的唯一比赛 ID；不同站 ID 分开保存、映射后再合并。 |
| `league_code` | 联赛标识，例如 `E0`、`SP1`、`D1`、`I1`、`F1`。 |
| `home_team`, `away_team` | 确认主客方向后的队名。 |
| `kickoff_beijing` | 比赛计划开球的北京时间；推迟时保存原计划与版本，不用赛后开球反推旧预测。 |

可附 `league`、`match_date_local`，**不得附目标场比分、胜平负结果或赛后统计**。

## 2. `prior_season.csv`：上赛季背景，必需但可留未知

每场一行，与 `fixtures.csv` 的 `match_id,league_code,home_team,away_team` 相同。双方各有：

- `{home|away}_prior_division`：`1` 表示上季就在本联赛顶级组；升班马或未知写 `2`/`unknown`。
- `{home|away}_promoted_from_second_tier`：`true`/`false`；不确定写 `unknown`。
- `{home|away}_prior_played` 与 `{home|away}_prior_points`：只用在目标场决策时点之前已公布、完整的上季常规赛；未知留空。

当任何一侧不符合顶级组或统计不全时，只停用 1 个百分点的候选修正，仍可计算赔率基线。上季最终名次、进失球、积分来源、赛季版本和升降级判断可放入分析注释及来源审计表。

## 3. `market_asof.csv`：带时点证据的盘口快照，必需

每场×公司×市场×阶段最多一行。脚本读取的列为：

| 字段 | 要求 |
| --- | --- |
| `match_id`, `league_code`, `home_team`, `away_team`, `kickoff_beijing` | 必须与赛程表一致。 |
| `market` | `x12` 为全场胜平负，`ah` 为全场亚洲让球，`ou` 为全场大小球。 |
| `company_id`, `bookmaker_label` | 保存站内 ID 与显示名；遮盖名不自行扩写。 |
| `phase` | `T_minus_24h`、`T_minus_3h`、`T_minus_1h`；均相对**计划开球**计算。 |
| `asof_beijing`, `quote_time_beijing` | 阶段截点与原始报价时间；`quote_time ≤ asof ≤ kickoff−1h`。 |
| `quote_age_minutes`, `status` | 报价距截点的分钟数；`即`、`早`、`初盘`、`(初盘)` 等非滚球状态才可准入。 |
| `home_odds`, `draw_odds`, `away_odds` | `x12` 的三项十进制赔率，必须完整且都大于 1。 |
| `line_value`, `home_hk`, `away_hk` | `ah` 的主队补偿线和两侧香港水位；主让半球为 `-0.5`。 |
| `line_value`, `over_hk`, `under_hk` | `ou` 的总进球线和两侧香港水位。 |

所有报价均须取自**该截点前最新的可证明可见记录**，不是开球前最后一笔、聚合页静态“终”、无时间的“收盘赔率”或赛后页面的整表。`T_minus_1h` 决定主预测；`T_minus_24h` 只用于走势注释。最长报价年龄默认 72 小时，超出则该公司在该阶段缺失。最早可见记录不自动等于真实初盘。同分钟多次变价必须有可靠页面顺序或更高精度时间，否则标歧义并弃用该截面。

从原始页逐行整理 `raw_quotes.csv` 时，在盘口字段之外必须记下 `source_url, source_line, fetched_at_utc, raw_sha256, parser_version, quote_time_source, timezone_basis`。`source_url` 是整页下载后的**最终 URL**，发生重定向时不填原请求模板；`source_line` 是原页可复查的正整数定位；`raw_sha256` 是页面原文 SHA-256；`fetched_at_utc` 要用整页下载**完成**的 UTC 时刻；后面三个字段须说明手工或程序解析版本、赔率时刻的原文字段，以及将其解释为北京时间的依据。`examples/raw_quotes_template.csv` 给出表头。`scripts/capture_titan.py` 的每次采集目录保存 `manifest.json` 和原始页面；严格模式的 `scripts/prepare_packet.py --capture-manifest /path/to/manifest.json` 会核对 ID、公司、市场、最终 URL、抓取时刻、网页文件与散列，并只收录截点前采集完成的记录。T−24h 与 T−1h 若来自不同采集批次，可重复传入 `--capture-manifest`，每个参数对应一个原始采集目录；两个批次都要在各自截点前完成。这一核对证明页面留存的完整性和时点，不自动证明人工抄录或解析程序对页面语义的解读正确，关键盘口须抽样复核。解析后的来源字段另存独立审计目录，不给盲分析员含赛果链接。如果只有赛后抓到的历史页，显式使用 `--historical-replay` 并标记 `retrospective_timestamp_unverified`，只能做有限的回顾性回放。

## 4. 可选信息与评估表

- `team_news.csv`：球队、人员状态分类、正文来源、发表时间、最后修改时间、抓取时间、正文 SHA；三种时间均早于 T 才可进入正式赛前注释。预计阵容与确认首发分开。
- `recent_form.csv`：每项近期统计必须追溯到组成它的历史比赛 ID，并证明其**最终赛果发布时间 ≤ T**。不足六场记录实际样本数，不补零。
- `exchange.csv`：Betfair `MATCH_ODDS` 三项**已撮合**成交量与成交价、市场 ID、快照时间、币种及来源档次；挂单量另列，不与成交混用。
- `results.csv`：赛后才解封，至少含 `match_id,home_goals,away_goals`；可附结果公布时间、来源及纠错版本。不可放进赛前输入目录。
- `bet_rules.json`：若要计算策略 ROI，事前固定下注触发条件、公司、价格选择、每笔金额、费用、走盘/退款与缺价处理；赛后补定规则只可报“探索性假设收益”。

`examples/` 提供最小表头模板。所有缺失用空值或明确的 `unknown`，不可把未知写成 0。输出至少包括逐场覆盖/弃判原因、报价年龄、各公司数据、`p0`、候选概率、质量等级和文件 SHA-256。
