#!/usr/bin/env python3
"""Build outcome-free T-24h/T-3h/T-1h odds snapshots from parsed quote rows.

Input raw_quotes.csv is produced from captured pages by a separately checked
parser or careful manual transcription. This program does not parse HTML.
Default strict mode links each quote to an archived page in capture_titan.py's
manifest, checks the page hash and fetch time, and requires parser/timezone
provenance. This verifies capture timing and integrity, not the parser's semantic
correctness. --historical-replay permits later archives but marks the limitation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


PHASES = (("T_minus_24h", 24), ("T_minus_3h", 3), ("T_minus_1h", 1))
FIELDS = [
    "match_id", "league_code", "home_team", "away_team", "kickoff_beijing",
    "market", "company_id", "bookmaker_label", "phase", "asof_beijing",
    "quote_time_beijing", "quote_age_minutes", "status", "line_value",
    "home_hk", "away_hk", "over_hk", "under_hk", "home_odds", "draw_odds", "away_odds",
]
FORBIDDEN = {"home_goals", "away_goals", "score", "result", "ftr", "fthg", "ftag"}
STATUS = {"即", "早", "初盘", "(初盘)"}
MARKET_PAGES = {"x12": {"1x2", "3in1"}, "ah": {"ah", "3in1"}, "ou": {"ou", "3in1"}}
PROVENANCE_FIELDS = [
    "match_id", "market", "company_id", "phase", "source_url", "source_line",
    "fetched_at_utc", "quote_time_beijing", "raw_sha256", "parser_version",
    "quote_time_source", "timezone_basis", "capture_file", "capture_page",
    "capture_manifest_sha256", "capture_manifest_path",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        bad = {x.lower() for x in reader.fieldnames or []} & FORBIDDEN
        if bad:
            raise ValueError(f"{path.name}: target-result columns forbidden: {sorted(bad)}")
        return list(reader)


def bt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def ut(s: str) -> datetime:
    x = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if x.tzinfo is None:
        raise ValueError("fetched_at_utc needs explicit UTC offset")
    return x.astimezone(timezone.utc)


def capture_index(paths: list[Path], fixtures: dict[str, dict[str, str]]) -> tuple[dict[tuple[str, str, str, str, datetime], dict], list[str]]:
    """Index successful, immutable captures; verify each linked page on use."""
    indexed = {}
    hashes = []
    for path in paths:
        manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(manifest_hash)
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError(f"capture manifest must be a JSON list: {path}")
        for record in records:
            if not isinstance(record, dict) or str(record.get("http_status")) != "200" or record.get("error"):
                continue
            mid = str(record.get("match_id", ""))
            cid = str(record.get("company_id", ""))
            if mid not in fixtures or not cid:
                continue  # The company-less oddslist page cannot prove a company-specific row.
            if not record.get("url") or not record.get("sha256") or not record.get("file"):
                continue
            try:
                fetched = ut(str(record["fetched_at_utc"]))
                expected_cutoff = bt(fixtures[mid]["kickoff_beijing"]).replace(
                    tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc) - timedelta(hours=1)
                if ut(str(record["cutoff_utc"])) != expected_cutoff:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            sha = str(record["sha256"]).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                continue
            record["_capture_manifest_path"] = str(path.resolve())
            record["_capture_manifest_sha256"] = manifest_hash
            # A redirect means the page bytes came from the final URL, not
            # necessarily the requested template URL.
            source_url = str(record.get("final_url") or record["url"])
            key = (mid, cid, source_url, sha, fetched)
            if key in indexed and indexed[key] != record:
                raise ValueError(f"ambiguous capture manifest entries for match {mid}, company {cid}")
            indexed[key] = record
    return indexed, hashes


def verify_capture(q: dict[str, str], indexed: dict,
                   verified_files: dict[str, str]) -> dict:
    """Fail closed when a quote's claimed source cannot be tied to saved bytes."""
    required = ("source_url", "source_line", "fetched_at_utc", "raw_sha256",
                "parser_version", "quote_time_source", "timezone_basis")
    missing = [field for field in required if not q.get(field, "").strip()]
    if missing:
        raise ValueError(f"match {q.get('match_id')} company {q.get('company_id')}: missing provenance {missing}")
    if not q["source_line"].isdigit() or int(q["source_line"]) < 1:
        raise ValueError("source_line must be a positive integer")
    if q["market"] not in MARKET_PAGES:
        raise ValueError(f"unsupported market {q['market']!r}")
    sha = q["raw_sha256"].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("raw_sha256 must be a SHA-256 hex digest")
    fetched = ut(q["fetched_at_utc"])
    key = (q["match_id"], q["company_id"], q["source_url"], sha, fetched)
    record = indexed.get(key)
    if record is None:
        raise ValueError(f"unlinked capture for match {q['match_id']}, company {q['company_id']}, market {q['market']}")
    if record.get("page") not in MARKET_PAGES[q["market"]]:
        raise ValueError(f"capture page {record.get('page')!r} cannot support market {q['market']!r}")
    filename = str(record["file"])
    manifest_path = Path(record["_capture_manifest_path"])
    candidate = (manifest_path.parent / filename).resolve()
    if not candidate.is_relative_to(manifest_path.parent.resolve()) or not candidate.is_file():
        raise ValueError(f"capture file missing or outside capture directory: {filename}")
    file_key = str(candidate)
    if file_key not in verified_files:
        verified_files[file_key] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if verified_files[file_key] != sha:
        raise ValueError(f"capture page hash mismatch: {filename}")
    return record


def valid_price(q: dict[str, str]) -> bool:
    try:
        if q["market"] == "x12":
            values = [float(q[k]) for k in ("home_odds", "draw_odds", "away_odds")]
            return all(math.isfinite(x) and x > 1 for x in values)
        if q["market"] == "ah":
            values = [float(q[k]) for k in ("line_value", "home_hk", "away_hk")]
        elif q["market"] == "ou":
            values = [float(q[k]) for k in ("line_value", "over_hk", "under_hk")]
        else:
            return False
        return all(math.isfinite(x) for x in values) and all(x >= 0 for x in values[1:])
    except (KeyError, ValueError, TypeError):
        return False


def source_identity(q: dict[str, str]) -> tuple[str, ...] | None:
    record = q.get("_capture_record")
    if record:
        return (record["_capture_manifest_path"], str(record["file"]), str(record["sha256"]))
    if q.get("source_url") and q.get("raw_sha256"):
        return (q["source_url"], q["raw_sha256"])
    return None


def choose(available: list[dict[str, str]]) -> dict[str, str]:
    latest = max(bt(q["quote_time_beijing"]) for q in available)
    tied = [q for q in available if bt(q["quote_time_beijing"]) == latest]
    if len(tied) == 1:
        return tied[0]
    data_fields = ("line_value", "home_hk", "away_hk", "over_hk", "under_hk",
                   "home_odds", "draw_odds", "away_odds")
    values = {tuple(q.get(k, "") for k in data_fields) for q in tied}
    if len(values) == 1:
        return tied[0]
    identities = {source_identity(q) for q in tied}
    if len(identities) != 1 or None in identities:
        raise ValueError("different same-minute quotes come from different or unidentified pages")
    if all(q.get("source_order") for q in tied):
        ranks = [int(q["source_order"]) for q in tied]
    elif all(q.get("source_line") for q in tied):
        # Titan detail pages list newer events nearer the top; smaller line is newer.
        ranks = [-int(q["source_line"]) for q in tied]
    else:
        raise ValueError("different same-minute quotes lack consistent source_order/source_line")
    best_rank = max(ranks)
    best = [q for q, rank in zip(tied, ranks) if rank == best_rank]
    if len({tuple(q.get(k, "") for k in data_fields) for q in best}) != 1:
        raise ValueError("same-minute source order cannot break price tie")
    return best[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--raw-quotes", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, help="Separate directory for selected raw source URLs and capture times")
    parser.add_argument("--capture-manifest", type=Path, action="append", help="capture_titan.py manifest.json; repeat for each capture run; required unless --historical-replay")
    parser.add_argument("--historical-replay", action="store_true", help="Allow post-cutoff capture; output is not a genuine prospective snapshot")
    args = parser.parse_args()
    if not args.historical_replay and not args.capture_manifest:
        parser.error("strict mode requires --capture-manifest (use --historical-replay for unverified archives)")
    if args.audit_dir and args.audit_dir.resolve() == args.out_dir.resolve():
        parser.error("audit directory must differ from blind input directory")
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        parser.error("output directory must be empty; previous snapshots are never overwritten")
    if args.audit_dir and (args.audit_dir / "selected_quote_sources.csv").exists():
        parser.error("audit source file already exists; previous provenance is never overwritten")
    fixtures = read_csv(args.fixtures)
    raw = read_csv(args.raw_quotes)
    if not fixtures or len({r["match_id"] for r in fixtures}) != len(fixtures):
        raise ValueError("fixtures must be nonempty and unique")
    by_id = {r["match_id"]: r for r in fixtures}
    indexed, capture_manifest_hashes = ({}, []) if args.historical_replay else capture_index(args.capture_manifest, by_id)
    verified_files: dict[str, str] = {}
    groups = defaultdict(list)
    rejection = Counter()
    for q in raw:
        mid = q.get("match_id", "")
        if mid not in by_id:
            rejection["unknown_match_id"] += 1
            continue
        if not args.historical_replay:
            q["_capture_record"] = verify_capture(q, indexed, verified_files)
        if q.get("status") not in STATUS or not valid_price(q):
            rejection["invalid_status_or_price"] += 1
            continue
        if not q.get("company_id") or not q.get("bookmaker_label"):
            rejection["missing_company"] += 1
            continue
        try:
            if bt(q["quote_time_beijing"]) >= bt(by_id[mid]["kickoff_beijing"]):
                rejection["not_prematch_quote"] += 1
                continue
        except (KeyError, ValueError):
            rejection["invalid_quote_time"] += 1
            continue
        groups[(mid, q["market"], q["company_id"])].append(q)
    output = []
    provenance = []
    for (mid, market, cid), quotes in groups.items():
        f = by_id[mid]
        kickoff = bt(f["kickoff_beijing"])
        for phase, before_hours in PHASES:
            asof = kickoff - timedelta(hours=before_hours)
            asof_utc = asof.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
            available = []
            for q in quotes:
                if bt(q["quote_time_beijing"]) > asof:
                    continue
                if not args.historical_replay:
                    try:
                        captured = ut(q["fetched_at_utc"])
                        quote_utc = bt(q["quote_time_beijing"]).replace(
                            tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
                        if not quote_utc <= captured <= asof_utc:
                            continue
                    except (KeyError, ValueError):
                        continue
                available.append(q)
            if not available:
                rejection["no_visible_quote_at_" + phase] += 1
                continue
            q = choose(available)
            row = {k: f[k] for k in ("match_id", "league_code", "home_team", "away_team", "kickoff_beijing")}
            row.update({
                "market": market, "company_id": cid, "bookmaker_label": q["bookmaker_label"],
                "phase": phase, "asof_beijing": asof.strftime("%Y-%m-%d %H:%M"),
                "quote_time_beijing": q["quote_time_beijing"],
                "quote_age_minutes": int((asof - bt(q["quote_time_beijing"])).total_seconds() / 60),
                "status": q["status"],
            })
            for key in ("line_value", "home_hk", "away_hk", "over_hk", "under_hk",
                        "home_odds", "draw_odds", "away_odds"):
                row[key] = q.get(key, "")
            output.append(row)
            provenance.append({
                "match_id": mid, "market": market, "company_id": cid, "phase": phase,
                "source_url": q.get("source_url", ""), "source_line": q.get("source_line", ""),
                "fetched_at_utc": q.get("fetched_at_utc", ""),
                "quote_time_beijing": q["quote_time_beijing"],
                "raw_sha256": q.get("raw_sha256", ""),
                "parser_version": q.get("parser_version", ""),
                "quote_time_source": q.get("quote_time_source", ""),
                "timezone_basis": q.get("timezone_basis", ""),
                "capture_file": q.get("_capture_record", {}).get("file", ""),
                "capture_page": q.get("_capture_record", {}).get("page", ""),
                "capture_manifest_sha256": q.get("_capture_record", {}).get("_capture_manifest_sha256", ""),
                "capture_manifest_path": q.get("_capture_record", {}).get("_capture_manifest_path", ""),
            })
    output.sort(key=lambda r: (r["kickoff_beijing"], r["match_id"], r["market"],
                               r["company_id"], next(i for i, (p, _) in enumerate(PHASES) if p == r["phase"])))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / "market_asof.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output)
    if args.audit_dir:
        args.audit_dir.mkdir(parents=True, exist_ok=True)
        with (args.audit_dir / "selected_quote_sources.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=PROVENANCE_FIELDS)
            writer.writeheader()
            writer.writerows(provenance)
    manifest = {
        "mode": "retrospective_timestamp_unverified" if args.historical_replay else "capture_integrity_and_cutoff_verified_parser_declared",
        "note": "Strict mode verifies captured page bytes, URL, company, market and fetch time. Parser-derived quote values and time interpretation remain declared provenance, not independently proven by this script.",
        "rows": len(output), "rejected_counts": dict(rejection),
        "fixtures_sha256": hashlib.sha256(args.fixtures.read_bytes()).hexdigest(),
        "raw_quotes_sha256": hashlib.sha256(args.raw_quotes.read_bytes()).hexdigest(),
        "market_asof_sha256": hashlib.sha256(out_csv.read_bytes()).hexdigest(),
    }
    if not args.historical_replay:
        manifest["capture_manifests_sha256"] = capture_manifest_hashes
        if len(capture_manifest_hashes) == 1:
            manifest["capture_manifest_sha256"] = capture_manifest_hashes[0]
        manifest["verified_capture_files"] = len(verified_files)
    if args.audit_dir:
        manifest["selected_sources_sha256"] = hashlib.sha256(
            (args.audit_dir / "selected_quote_sources.csv").read_bytes()).hexdigest()
    (args.out_dir / "prep_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(output), "mode": manifest["mode"], "rejected": dict(rejection)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
