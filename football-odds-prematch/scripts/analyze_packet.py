#!/usr/bin/env python3
"""Deterministic prematch 1X2 baseline and experimental PPG correction.

Reads only three explicitly named, outcome-free CSV inputs. European company
IDs 1/8/9/14 reproduce the audited reference roster; changes to this roster
must be fixed before new outcomes are known. The T-minus-24h checkpoint
annotates market movement/quality; it never changes probabilities.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


INPUT = Path(".")
OUTPUT = Path(".")
FILES = ("fixtures.csv", "prior_season.csv", "market_asof.csv")
FORBIDDEN_INPUT_COLUMNS = {
    "home_goals", "away_goals", "score", "result", "result_home_perspective",
    "full_time_result", "fthg", "ftag", "ftr", "source_url",
}
ALLOWED_STATUS = {"即", "早", "初盘", "(初盘)"}
EURO_COMPANIES = {"1", "8", "9", "14"}
ASIAN_COMPANIES = {"1", "8", "9", "14", "31"}
MAX_AGE = timedelta(hours=72)
QUANT = Decimal("0.00000001")
MIN_REMAINING_PROBABILITY = 0.000001


def read_csv(name: str) -> list[dict[str, str]]:
    with (INPUT / name).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        bad = {x.lower() for x in (reader.fieldnames or [])} & FORBIDDEN_INPUT_COLUMNS
        if bad:
            raise ValueError(f"{name}: forbidden outcome or live-link columns: {sorted(bad)}")
        return list(reader)


def dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def finite_positive(value: str, *, min_value: float = 0.0) -> float:
    x = float(value)
    if not math.isfinite(x) or x <= min_value:
        raise ValueError("invalid quoted numeric value")
    return x


def fair_prices(row: dict[str, str]) -> tuple[float, float, float]:
    odds = [finite_positive(row[k], min_value=1.0) for k in
            ("home_odds", "draw_odds", "away_odds")]
    inverse = [1.0 / o for o in odds]
    total = sum(inverse)
    return tuple(x / total for x in inverse)


def asian_prices(row: dict[str, str]) -> tuple[float, float, float]:
    line = float(row["line_value"])
    home = float(row["home_hk"])
    away = float(row["away_hk"])
    if not all(math.isfinite(x) for x in (line, home, away)) or home < 0 or away < 0:
        raise ValueError("invalid Asian line or payout")
    return line, home, away


def valid_snapshot(row: dict[str, str], fixture: dict[str, str]) -> bool:
    """Strict source/as-of check; only two explicitly named phases are admitted."""
    phase = row["phase"]
    if phase not in {"T_minus_1h", "T_minus_24h"}:
        return False
    if row["market"] not in {"x12", "ah"}:
        return False
    if row["company_id"] not in (EURO_COMPANIES if row["market"] == "x12" else ASIAN_COMPANIES):
        return False
    if row["status"] not in ALLOWED_STATUS:
        return False
    if (row["league_code"], row["home_team"], row["away_team"], row["kickoff_beijing"]) != (
        fixture["league_code"], fixture["home_team"], fixture["away_team"], fixture["kickoff_beijing"]
    ):
        raise ValueError("market/fixture identifiers disagree")
    try:
        kickoff = dt(fixture["kickoff_beijing"])
        asof_expected = kickoff - timedelta(hours=1 if phase == "T_minus_1h" else 24)
        asof_actual = dt(row["asof_beijing"])
        quote = dt(row["quote_time_beijing"])
        if asof_actual != asof_expected or quote > asof_actual or quote >= kickoff:
            return False
        if row.get("quote_age_minutes") and int(row["quote_age_minutes"]) != int((asof_actual - quote).total_seconds() / 60):
            return False
        fair_prices(row) if row["market"] == "x12" else asian_prices(row)
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def age(row: dict[str, str]) -> timedelta:
    return dt(row["asof_beijing"]) - dt(row["quote_time_beijing"])


def recent(row: dict[str, str] | None) -> bool:
    return row is not None and timedelta(0) <= age(row) <= MAX_AGE


def direction(x: float, threshold: float) -> int:
    return 1 if x >= threshold - 1e-10 else -1 if x <= -threshold + 1e-10 else 0


def asian_move(older: dict[str, str], newer: dict[str, str]) -> int:
    old_line, old_home, old_away = asian_prices(older)
    new_line, new_home, new_away = asian_prices(newer)
    delta = new_line - old_line
    # A lower home handicap means stronger home favoritism.
    if delta <= -0.25 + 1e-10:
        return 1
    if delta >= 0.25 - 1e-10:
        return -1
    if abs(delta) > 1e-10:
        return 0
    home_falls = old_home - new_home >= 0.05 - 1e-10
    away_falls = old_away - new_away >= 0.05 - 1e-10
    if home_falls and not away_falls:
        return 1
    if away_falls and not home_falls:
        return -1
    return 0


def as_decimal(x: float) -> Decimal:
    return Decimal(str(x)).quantize(QUANT, rounding=ROUND_HALF_UP)


def export_triplet(p: tuple[float, float, float]) -> tuple[str, str, str]:
    h, d = as_decimal(p[0]), as_decimal(p[1])
    a = Decimal(1) - h - d
    rounded = [h, d, a]
    # Very long valid odds can round a tiny positive probability to zero.
    # Transfer the necessary last decimal units from the largest outcome so
    # the serialized forecast remains scoreable without changing its sum.
    for i in range(3):
        if rounded[i] < QUANT:
            needed = QUANT - rounded[i]
            donor = max((j for j in range(3) if j != i), key=lambda j: rounded[j])
            if rounded[donor] - needed < QUANT:
                raise ValueError("cannot export positive forecast probabilities")
            rounded[i] = QUANT
            rounded[donor] -= needed
    raw_winner = max(range(3), key=lambda i: p[i])
    saved_winner = max(range(3), key=lambda i: rounded[i])
    if raw_winner != saved_winner and p[raw_winner] == p[saved_winner]:
        # The residual can make the last outcome appear to win an exact raw
        # tie (e.g. 3/3/3 odds). Keep the declared 1, X, 2 tie order.
        rounded[raw_winner] += QUANT
        rounded[saved_winner] -= QUANT
    if min(rounded) < QUANT or sum(rounded) != 1:
        raise ValueError("invalid exported probability triplet")
    return tuple(f"{x:.8f}" for x in rounded)


def company_label(company: str, row: dict[str, str]) -> str:
    """Use the name actually recorded with this quote, preserving masked labels."""
    return (row.get("bookmaker_label") or "").strip() or f"ID {company}"


def prior_ppg(row: dict[str, str], side: str) -> float | None:
    """Unknown prior-season values disable the optional correction."""
    points_raw = (row.get(f"{side}_prior_points") or "").strip()
    played_raw = (row.get(f"{side}_prior_played") or "").strip()
    if not points_raw or not played_raw or "unknown" in {points_raw.lower(), played_raw.lower()}:
        return None
    try:
        points, played = float(points_raw), float(played_raw)
    except ValueError as exc:
        raise ValueError(f"invalid {side} prior-season points/played") from exc
    if not math.isfinite(points) or not math.isfinite(played) or points < 0 or played <= 0:
        raise ValueError(f"invalid {side} prior-season points/played")
    return points / played


def md_float(x: float, places: int = 3) -> str:
    return f"{x:.{places}f}"


def main() -> None:
    global INPUT, OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True, help="Directory with fixtures.csv, prior_season.csv and market_asof.csv; no scores")
    parser.add_argument("--output-dir", type=Path, required=True, help="Separate directory for sealed forecasts")
    args = parser.parse_args()
    INPUT, OUTPUT = args.input_dir.resolve(), args.output_dir.resolve()
    if INPUT == OUTPUT:
        raise ValueError("input and output directories must differ")
    fixtures = read_csv("fixtures.csv")
    prior_rows = read_csv("prior_season.csv")
    markets = read_csv("market_asof.csv")
    if not fixtures or len({f["match_id"] for f in fixtures}) != len(fixtures):
        raise ValueError("fixtures must be nonempty and have unique match IDs")
    if len(prior_rows) != len(fixtures) or len({r["match_id"] for r in prior_rows}) != len(fixtures):
        raise ValueError("prior season table must have one row per fixture")
    fixture_by_id = {f["match_id"]: f for f in fixtures}
    prior_by_id = {r["match_id"]: r for r in prior_rows}
    if set(fixture_by_id) != set(prior_by_id):
        raise ValueError("prior season match IDs do not equal fixture match IDs")
    accepted: dict[tuple[str, str, str, str], dict[str, str]] = {}
    rejected = defaultdict(int)
    for row in markets:
        mid = row["match_id"]
        if mid not in fixture_by_id:
            raise ValueError("unknown market match ID")
        if row["phase"] not in {"T_minus_1h", "T_minus_24h"} or row["market"] not in {"x12", "ah"}:
            continue
        if not valid_snapshot(row, fixture_by_id[mid]):
            rejected[f"{row['phase']}:{row['market']}"] += 1
            continue
        key = (mid, row["phase"], row["market"], row["company_id"])
        if key in accepted:
            raise ValueError(f"duplicate checkpoint for {key}")
        accepted[key] = row

    predictions = []
    notes = []
    for fixture in fixtures:
        mid = fixture["match_id"]
        prior = prior_by_id[mid]
        if (prior["league_code"], prior["home_team"], prior["away_team"]) != (
            fixture["league_code"], fixture["home_team"], fixture["away_team"]
        ):
            raise ValueError("prior-season/fixture identifiers disagree")
        kickoff = dt(fixture["kickoff_beijing"])
        cutoff = kickoff - timedelta(hours=1)
        x12 = {company: accepted.get((mid, "T_minus_1h", "x12", company)) for company in EURO_COMPANIES}
        x12 = {company: row for company, row in x12.items() if recent(row)}
        if len(x12) < 2:
            predictions.append({
                "match_id": mid, "league_code": fixture["league_code"],
                "kickoff_beijing": fixture["kickoff_beijing"],
                "cutoff_beijing": cutoff.strftime("%Y-%m-%d %H:%M"),
                "home_team": fixture["home_team"], "away_team": fixture["away_team"],
                "p0_home": "", "p0_draw": "", "p0_away": "",
                "p_home": "", "p_draw": "", "p_away": "", "pick": "",
                "max_probability": "", "quote_companies": len(x12),
                "oldest_x12_quote_age_minutes": "", "quality": "C",
                "prior_ppg_home": "", "prior_ppg_away": "", "prior_ppg_difference": "",
                "adjustment": "none", "eu_company_spread_max": "",
                "asian_static_mismatch_count": "", "trend_assessable_companies": "",
                "eu_asian_trend_conflict": "", "status": "abstain",
                "reason": "fewer than two valid 1X2 companies within 72 hours",
            })
            notes.append({"match_id": mid, "match": f"{fixture['home_team']} vs {fixture['away_team']}",
                          "note": "Abstain: fewer than two valid 1X2 companies within 72 hours."})
            continue
        company_fair = {company: fair_prices(row) for company, row in x12.items()}
        p0_raw = [statistics.median(v[i] for v in company_fair.values()) for i in range(3)]
        p0_sum = sum(p0_raw)
        p0 = tuple(x / p0_sum for x in p0_raw)
        if not math.isclose(sum(p0), 1.0, abs_tol=1e-12):
            raise ValueError("p0 normalization failed")

        eligible_prior = (prior["home_prior_division"] == "1" and
                          prior["away_prior_division"] == "1" and
                          prior["home_promoted_from_second_tier"].lower() == "false" and
                          prior["away_promoted_from_second_tier"].lower() == "false")
        prior_h = prior_ppg(prior, "home") if eligible_prior else None
        prior_a = prior_ppg(prior, "away") if eligible_prior else None
        eligible_prior = eligible_prior and prior_h is not None and prior_a is not None
        prior_delta = None
        p = list(p0)
        adjustment = "none"
        if eligible_prior:
            prior_delta = prior_h - prior_a
            if (prior_delta >= 0.60 - 1e-10 and p0[0] < p0[2] - 1e-10
                    and p0[2] - 0.01 >= MIN_REMAINING_PROBABILITY):
                p[0] += 0.01
                p[2] -= 0.01
                adjustment = "away_to_home_0.01"
            elif (prior_delta <= -0.60 + 1e-10 and p0[0] > p0[2] + 1e-10
                  and p0[0] - 0.01 >= MIN_REMAINING_PROBABILITY):
                p[0] -= 0.01
                p[2] += 0.01
                adjustment = "home_to_away_0.01"
        if min(p) < 0 or not math.isclose(sum(p), 1.0, abs_tol=1e-12):
            raise ValueError("invalid adjusted forecast")

        spread = max(max(v[i] for v in company_fair.values()) -
                     min(v[i] for v in company_fair.values()) for i in range(3))
        ages = {company: round(age(row).total_seconds() / 60) for company, row in x12.items()}
        fresh24 = sum(a <= 1440 for a in ages.values())
        grade = 2 if fresh24 >= 3 else 1 if len(x12) >= 2 else 0

        # Cross-sectional European/Asian disagreement is descriptive only.
        ah = {company: accepted.get((mid, "T_minus_1h", "ah", company)) for company in ASIAN_COMPANIES}
        ah = {company: row for company, row in ah.items() if recent(row)}
        static_mismatches = []
        for company in sorted(set(x12) & set(ah), key=int):
            line, _, _ = asian_prices(ah[company])
            euro_dir = direction(company_fair[company][0] - company_fair[company][2], 0.05)
            ah_dir = 1 if line <= -0.25 + 1e-10 else -1 if line >= 0.25 - 1e-10 else 0
            if euro_dir and ah_dir == -euro_dir:
                static_mismatches.append(company_label(company, x12[company]))

        # The two independently validated checkpoints permit a 23-hour
        # movement comparison. Old quotes at either checkpoint are excluded.
        trend_companies = []
        discord_home = []
        discord_away = []
        for company in sorted(EURO_COMPANIES, key=int):
            eu24 = accepted.get((mid, "T_minus_24h", "x12", company))
            eu1 = x12.get(company)
            ah24 = accepted.get((mid, "T_minus_24h", "ah", company))
            ah1 = ah.get(company)
            if not all(recent(r) for r in (eu24, eu1, ah24, ah1)):
                continue
            old_fair = fair_prices(eu24)
            new_fair = company_fair[company]
            eu_move = direction((new_fair[0] - new_fair[2]) - (old_fair[0] - old_fair[2]), 0.02)
            ah_move = asian_move(ah24, ah1)
            label = company_label(company, eu1)
            trend_companies.append(f"{label}:EU{eu_move:+d}/AH{ah_move:+d}")
            if eu_move == 1 and ah_move == -1:
                discord_home.append(label)
            elif eu_move == -1 and ah_move == 1:
                discord_away.append(label)
        trend_conflict = len(discord_home) >= 2 or len(discord_away) >= 2
        grade = max(0, grade - int(spread > 0.05) - int(trend_conflict))
        quality = "CBA"[grade]
        p0_export = export_triplet(p0)
        p_export = export_triplet(tuple(p))
        # The saved decision must be derivable from the saved probabilities.
        # Exact ties use the fixed 1, X, 2 order.
        pick_index = max(range(3), key=lambda i: Decimal(p_export[i]))
        pick = ("1", "X", "2")[pick_index]
        euro_summary = "; ".join(
            f"{company_label(c, x12[c])} {md_float(company_fair[c][0])}/{md_float(company_fair[c][1])}/{md_float(company_fair[c][2])},"
            f" age {ages[c]}m, return {md_float(100/sum(1/finite_positive(x12[c][k], min_value=1.0) for k in ('home_odds','draw_odds','away_odds')), 1)}%"
            for c in sorted(x12, key=int)
        )
        asian_summary = "; ".join(
            f"{company_label(c, ah[c])} {asian_prices(ah[c])[0]:+.2f},"
            f" HK {asian_prices(ah[c])[1]:.2f}/{asian_prices(ah[c])[2]:.2f},"
            f" age {round(age(ah[c]).total_seconds()/60)}m"
            for c in sorted(ah, key=int)
        )
        prior_summary = (f"both top tier; PPG {prior_h:.3f} vs {prior_a:.3f}; D={prior_delta:+.3f}"
                         if eligible_prior else "prior top-tier PPG adjustment ineligible")
        assessment = ("T-24h trends " + ", ".join(trend_companies)) if trend_companies else "T-24h trend unassessable"
        note = (f"European: {euro_summary}. Asian: {asian_summary or 'no fresh quote'}. "
                f"{prior_summary}; adjustment {adjustment}. "
                f"EU company spread {spread:.3f}; static EU/AH favorite mismatch "
                f"{', '.join(static_mismatches) or 'none'}. {assessment}; "
                f"qualified movement conflict {'yes' if trend_conflict else 'no'}. "
                f"Quality {quality} describes input consistency/freshness only.")

        predictions.append({
            "match_id": mid, "league_code": fixture["league_code"],
            "kickoff_beijing": fixture["kickoff_beijing"],
            "cutoff_beijing": cutoff.strftime("%Y-%m-%d %H:%M"),
            "home_team": fixture["home_team"], "away_team": fixture["away_team"],
            "p0_home": p0_export[0], "p0_draw": p0_export[1], "p0_away": p0_export[2],
            "p_home": p_export[0], "p_draw": p_export[1], "p_away": p_export[2],
            "pick": pick, "max_probability": p_export[pick_index],
            "quote_companies": len(x12), "oldest_x12_quote_age_minutes": max(ages.values()),
            "quality": quality, "prior_ppg_home": "" if prior_h is None else f"{prior_h:.6f}",
            "prior_ppg_away": "" if prior_a is None else f"{prior_a:.6f}",
            "prior_ppg_difference": "" if prior_delta is None else f"{prior_delta:.6f}",
            "adjustment": adjustment, "eu_company_spread_max": f"{spread:.6f}",
            "asian_static_mismatch_count": len(static_mismatches),
            "trend_assessable_companies": len(trend_companies),
            "eu_asian_trend_conflict": str(trend_conflict).lower(),
            "status": "predicted", "reason": "",
        })
        notes.append({"match_id": mid, "match": f"{fixture['home_team']} vs {fixture['away_team']}", "note": note})

    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise ValueError("output directory must be empty; sealed forecasts are never overwritten")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for filename, data in (("predictions.csv", predictions), ("per_game_notes.csv", notes)):
        with (OUTPUT / filename).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
    with (OUTPUT / "per_game_notes.md").open("w", encoding="utf-8") as f:
        f.write(f"# Prematch notes for {len(fixtures)} fixtures\n\n")
        f.write("All probabilities use the T−1h decision time. Company fair 1/X/2 prices and Asian lines are descriptive evidence.\n\n")
        for i, row in enumerate(notes, 1):
            f.write(f"{i}. **{row['match']}** (`{row['match_id']}`): {row['note']}\n\n")

    manifest = {
        "method": "Componentwise median of fresh company de-vig 1/X/2 prices, renormalized; predeclared 0.01 prior-PPG reversal correction",
        "cutoff": "kickoff Beijing time minus one hour",
        "trend_reference": "kickoff Beijing time minus 24 hours",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": len(predictions),
        "predicted_rows": sum(r["status"] == "predicted" for r in predictions),
        "abstained_rows": sum(r["status"] == "abstain" for r in predictions),
        "rejected_snapshots": dict(rejected),
        "input_sha256": {name: hashlib.sha256((INPUT / name).read_bytes()).hexdigest() for name in FILES},
        "output_sha256": {name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest()
                          for name in ("predictions.csv", "per_game_notes.csv", "per_game_notes.md")},
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with (OUTPUT / "lock_manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps({"rows": len(predictions), "predicted": manifest["predicted_rows"],
                      "abstained": manifest["abstained_rows"],
                      "adjustments": dict((a, sum(r["adjustment"] == a for r in predictions))
                                                               for a in sorted(set(r["adjustment"] for r in predictions))),
                      "quality": {q: sum(r["quality"] == q for r in predictions) for q in "ABC"},
                      "trend_conflicts": sum(r["eu_asian_trend_conflict"] == "true" for r in predictions)}, sort_keys=True))


if __name__ == "__main__":
    main()
