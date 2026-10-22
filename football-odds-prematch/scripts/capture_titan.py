#!/usr/bin/env python3
"""Archive public Titan007 pages with fetch-time provenance; does not parse odds.

Examples:
  python capture_titan.py --fixtures fixtures.csv --out-dir raw_capture --pages 3in1
  python capture_titan.py --fixtures fixtures.csv --out-dir raw_capture --pages 1x2,ah,ou --match-ids 1234567

Use only for authorized public access. HTTP blocks/errors are logged, never bypassed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TEMPLATES = {
    "oddslist": "https://op1.titan007.com/Oddslist/{match_id}.htm",
    "3in1": "https://vip.titan007.com/changeDetail/3in1Odds.aspx?companyid={company_id}&id={match_id}&l=0",
    "1x2": "https://vip.titan007.com/changeDetail/1x2.aspx?companyid={company_id}&id={match_id}&l=0",
    "ah": "https://vip.titan007.com/changeDetail/handicap.aspx?companyID={company_id}&id={match_id}&l=0",
    "ou": "https://vip.titan007.com/changeDetail/overunder.aspx?companyid={company_id}&id={match_id}&l=0",
}


def utc_time(beijing: str) -> datetime:
    local = datetime.strptime(beijing, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
    return local.astimezone(timezone.utc)


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise ValueError(f"invalid match/company ID: {value!r}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True, help="Outcome-free fixtures.csv with verified match IDs")
    parser.add_argument("--out-dir", type=Path, required=True, help="Parent directory for a new timestamped capture")
    parser.add_argument("--company-ids", default="1,8,9,14", help="Titan page IDs; preserve masked company names in your data")
    parser.add_argument("--pages", default="3in1", help="Comma-separated: oddslist,3in1,1x2,ah,ou")
    parser.add_argument("--match-ids", help="Optional comma-separated subset of verified fixture IDs")
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Polite delay between requests")
    parser.add_argument("--dry-run", action="store_true", help="Print planned URL count without requesting pages")
    args = parser.parse_args()
    companies = [safe_id(v.strip()) for v in args.company_ids.split(",") if v.strip()]
    pages = [v.strip() for v in args.pages.split(",") if v.strip()]
    if not pages or set(pages) - set(TEMPLATES):
        parser.error(f"unknown pages: {sorted(set(pages) - set(TEMPLATES))}")
    if len(companies) != len(set(companies)) or len(pages) != len(set(pages)):
        parser.error("duplicate company IDs or page types would overwrite captured files")
    if any(page != "oddslist" for page in pages) and not companies:
        parser.error("at least one company ID is required for detail pages")
    if args.sleep_seconds < 0:
        parser.error("sleep-seconds cannot be negative")
    selected = set(safe_id(v.strip()) for v in args.match_ids.split(",")) if args.match_ids else None
    with args.fixtures.open(encoding="utf-8-sig", newline="") as handle:
        fixtures = list(csv.DictReader(handle))
    if not fixtures:
        parser.error("fixtures.csv has no rows")
    ids = [safe_id(r["match_id"]) for r in fixtures]
    if len(set(ids)) != len(ids):
        parser.error("duplicate fixture match ID")
    if selected is not None:
        if selected - set(ids):
            parser.error("--match-ids includes IDs absent from fixtures.csv")
        fixtures = [r for r in fixtures if r["match_id"] in selected]
    jobs = []
    for fixture in fixtures:
        mid = fixture["match_id"]
        kickoff_utc = utc_time(fixture["kickoff_beijing"])
        for page in pages:
            for cid in ([""] if page == "oddslist" else companies):
                jobs.append({"match_id": mid, "company_id": cid, "page": page,
                             "url": TEMPLATES[page].format(match_id=mid, company_id=cid),
                             "cutoff_utc": (kickoff_utc - timedelta(hours=1)).isoformat()})
    if args.dry_run:
        print(json.dumps({"fixtures": len(fixtures), "requests": len(jobs), "first_urls": [j["url"] for j in jobs[:8]]}, ensure_ascii=False, indent=2))
        return
    run_id = datetime.now(timezone.utc).strftime("capture_%Y%m%dT%H%M%S_%fZ")
    out = args.out_dir / run_id
    out.mkdir(parents=True, exist_ok=False)
    manifest = []
    manifest_path = out / "manifest.json"
    for index, job in enumerate(jobs):
        if index:
            time.sleep(args.sleep_seconds)
        started_at = datetime.now(timezone.utc)
        record = {**job, "request_started_at_utc": started_at.isoformat(),
                  "fetched_at_utc": "", "captured_before_cutoff": False,
                  "http_status": "", "final_url": "", "file": "", "sha256": "", "error": ""}
        try:
            request = Request(job["url"], headers={
                "User-Agent": "Mozilla/5.0 (compatible; prematch-research/1.0)",
                "Referer": "https://www.titan007.com/",
            })
            with urlopen(request, timeout=25) as response:
                data = response.read()
                record["http_status"] = response.status
                record["final_url"] = response.geturl()
            fetched_at = datetime.now(timezone.utc)
            record["fetched_at_utc"] = fetched_at.isoformat()
            record["captured_before_cutoff"] = fetched_at <= datetime.fromisoformat(job["cutoff_utc"])
            filename = f"{job['match_id']}_{job['company_id'] or 'all'}_{job['page']}.html"
            (out / filename).write_bytes(data)
            record["file"] = filename
            record["sha256"] = hashlib.sha256(data).hexdigest()
        except HTTPError as exc:
            record["http_status"] = exc.code
            record["error"] = str(exc)
        except (URLError, TimeoutError, OSError) as exc:
            record["error"] = str(exc)
        if not record["fetched_at_utc"]:
            record["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest.append(record)
        # Keep completed records even if a long run is interrupted later.
        pending = manifest_path.with_suffix(".json.tmp")
        pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(manifest_path)
        print(f"{index + 1}/{len(jobs)} {job['match_id']} {job['company_id']} {job['page']}: {record['http_status'] or record['error']}")
    print(f"Saved {len(manifest)} capture records to {out}")


if __name__ == "__main__":
    main()
