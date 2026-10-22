#!/usr/bin/env python3
"""Score sealed 1X2 forecasts after results become available; optional flat-stake ROI."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dist(row: dict[str, str], prefix: str) -> list[float]:
    values = [float(row[prefix + suffix]) for suffix in ("home", "draw", "away")]
    if not all(0 < p < 1 for p in values) or not math.isclose(sum(values), 1, abs_tol=1e-7):
        raise ValueError("prediction probabilities must be positive and sum to 1")
    return values


def measure(p: list[float], y: int) -> tuple[str, float, float]:
    pick = ("1", "X", "2")[max(range(3), key=lambda i: p[i])]
    return pick, -math.log(p[y]), sum((p[i] - (i == y)) ** 2 for i in range(3))


def beijing_minute(value: str) -> datetime:
    """Parse the packet's minute-precision Beijing time without host timezone assumptions."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def eligible_quote(q: dict[str, str] | None, p: dict[str, str]) -> bool:
    """Check a quoted 1X2 snapshot against the sealed T-1h decision point."""
    if not q or q.get("status") not in {"即", "早", "初盘", "(初盘)"}:
        return False
    if (q.get("league_code"), q.get("home_team"), q.get("away_team"), q.get("kickoff_beijing")) != (
        p["league_code"], p["home_team"], p["away_team"], p["kickoff_beijing"]
    ):
        return False
    try:
        kickoff = beijing_minute(p["kickoff_beijing"])
        cutoff = beijing_minute(p["cutoff_beijing"])
        asof = beijing_minute(q["asof_beijing"])
        quote_time = beijing_minute(q["quote_time_beijing"])
        age_minutes = int((asof - quote_time).total_seconds() / 60)
        declared_age = int(q["quote_age_minutes"])
        odds = [float(q[name]) for name in ("home_odds", "draw_odds", "away_odds")]
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    return (
        cutoff == kickoff - timedelta(hours=1)
        and asof == cutoff
        and 0 <= age_minutes <= 72 * 60
        and declared_age == age_minutes
        and all(math.isfinite(o) and o > 1 for o in odds)
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--predictions", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True, help="Separate postmatch match_id,home_goals,away_goals table")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--bet-quotes", type=Path, help="Optional predeclared T-1h market_asof.csv to compute flat-stake descriptive ROI")
    ap.add_argument("--company-id", help="Fixed bookmaker ID for ROI; choose before viewing results")
    ap.add_argument("--stake", type=float, default=1.0)
    args = ap.parse_args()
    if not math.isfinite(args.stake) or args.stake <= 0:
        ap.error("stake must be finite and positive")
    if bool(args.bet_quotes) != bool(args.company_id):
        ap.error("--bet-quotes and --company-id must be specified together")
    manifest_path = args.predictions.with_name("lock_manifest.json")
    output_paths = {(args.out_dir / name).resolve() for name in ("evaluated.csv", "metrics.json")}
    input_paths = [args.predictions, args.results]
    if args.bet_quotes:
        input_paths.append(args.bet_quotes)
    if manifest_path.exists():
        input_paths.append(manifest_path)
    if output_paths.intersection(path.resolve() for path in input_paths):
        ap.error("output file would overwrite a prediction, result, quote, or lock-manifest input")
    if manifest_path.exists():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
        if locked.get("output_sha256", {}).get(args.predictions.name) != sha(args.predictions):
            raise ValueError("prediction hash disagrees with lock manifest")
    predictions = read(args.predictions)
    result_rows = read(args.results)
    if len({r["match_id"] for r in predictions}) != len(predictions):
        raise ValueError("duplicate prediction match IDs")
    if len({r["match_id"] for r in result_rows}) != len(result_rows):
        raise ValueError("duplicate result match IDs")
    by_id = {r["match_id"]: r for r in result_rows}
    if not set(r["match_id"] for r in predictions) <= set(by_id):
        raise ValueError("missing result for a predicted fixture")
    quotes = {}
    if args.bet_quotes:
        for r in read(args.bet_quotes):
            if r["market"] == "x12" and r["phase"] == "T_minus_1h" and r["company_id"] == args.company_id:
                key = r["match_id"]
                if key in quotes:
                    raise ValueError("duplicate company T-1h quote")
                quotes[key] = r
    evaluated = []
    stake_total = gross = 0.0
    no_price = 0
    for p in predictions:
        s = by_id[p["match_id"]]
        if p.get("status") == "abstain":
            continue
        if p.get("status") != "predicted":
            raise ValueError("prediction status must be predicted or abstain")
        hg, ag = int(s["home_goals"]), int(s["away_goals"])
        if hg < 0 or ag < 0:
            raise ValueError("final scores must be non-negative")
        y = 0 if hg > ag else 1 if hg == ag else 2
        actual = ("1", "X", "2")[y]
        baseline, ll0, bs0 = measure(dist(p, "p0_"), y)
        pick, ll1, bs1 = measure(dist(p, "p_"), y)
        if p["pick"] != pick:
            raise ValueError("stored pick differs from probabilities")
        row = {
            "match_id": p["match_id"], "home_team": p["home_team"], "away_team": p["away_team"],
            "home_goals": hg, "away_goals": ag, "actual": actual,
            "baseline_pick": baseline, "pick": pick,
            "baseline_correct": int(baseline == actual), "correct": int(pick == actual),
            "baseline_log_loss": f"{ll0:.10f}", "log_loss": f"{ll1:.10f}",
            "baseline_brier": f"{bs0:.10f}", "brier": f"{bs1:.10f}",
            "bet_odds": "", "bet_net": "",
        }
        if args.bet_quotes:
            q = quotes.get(p["match_id"])
            if not eligible_quote(q, p):
                no_price += 1
            else:
                odds = float(q[{"1": "home_odds", "X": "draw_odds", "2": "away_odds"}[pick]])
                stake_total += args.stake
                pay = args.stake * odds if pick == actual else 0.0
                gross += pay
                row["bet_odds"] = f"{odds:.4f}"
                row["bet_net"] = f"{pay - args.stake:.4f}"
        evaluated.append(row)
    if not evaluated:
        raise ValueError("no forecasts eligible for scoring")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "evaluated.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(evaluated[0]))
        writer.writeheader()
        writer.writerows(evaluated)
    n = len(evaluated)
    summary = {
        "scored": n, "abstained": len(predictions) - n,
        "baseline_accuracy": sum(r["baseline_correct"] for r in evaluated) / n,
        "accuracy": sum(r["correct"] for r in evaluated) / n,
        "baseline_log_loss": sum(float(r["baseline_log_loss"]) for r in evaluated) / n,
        "log_loss": sum(float(r["log_loss"]) for r in evaluated) / n,
        "baseline_brier": sum(float(r["baseline_brier"]) for r in evaluated) / n,
        "brier": sum(float(r["brier"]) for r in evaluated) / n,
        "roi": None if not args.bet_quotes else {
            "type": "descriptive_flat_stake_quoted_price",
            "company_id": args.company_id, "bet_count": int(round(stake_total / args.stake)),
            "no_eligible_price": no_price, "total_stake": stake_total,
            "gross_return": gross, "net_profit": gross - stake_total,
            "roi": None if stake_total == 0 else (gross - stake_total) / stake_total,
            "interpretation": "Descriptive flat-stake return at archived quotes; not executable or realized ROI. No fills, fees, or limits are evidenced.",
        },
        "predictions_sha256": sha(args.predictions), "results_sha256": sha(args.results),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (args.out_dir / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
