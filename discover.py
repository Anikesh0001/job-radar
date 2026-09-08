#!/usr/bin/env python3
"""Find which ATS a company uses, by probing every board we can read.

Growing the slug list is the real work of this project, and guessing by hand
has a high dead rate — the shipped config had `smartrecruiters: Visa` and
`Bosch`, both of which return zero (the live slug is `BoschGroup`).

This does the guessing mechanically: for each company name it derives a few
plausible slugs and asks every ATS whether that board exists, then prints the
hits as YAML you can paste straight into config/sources.yaml.

    python discover.py --names "Razorpay,Zerodha,Postman"
    python discover.py --file companies.txt --out found.yaml
    python discover.py --file companies.txt --min-jobs 1 --yaml

Probes are cheap existence checks, not full fetches, and every platform is
tried concurrently. Expect roughly a 15-30% hit rate: most companies are on
Workday/Taleo/SuccessFactors, which need more than a slug.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import yaml

logging.basicConfig(level=logging.ERROR, format="%(message)s")
log = logging.getLogger("discover")

UA = "job-radar/1.0 (slug discovery; +https://github.com/Anikesh0001/job-radar)"
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
TIMEOUT = httpx.Timeout(12.0, connect=6.0)

_NON = re.compile(r"[^a-z0-9]+")


def slug_variants(name: str) -> list[str]:
    """'Tata Consultancy Services' -> tataconsultancyservices, tata-consultancy-services, tata"""
    base = _NON.sub(" ", name.lower()).strip()
    words = base.split()
    if not words:
        return []
    out = ["".join(words)]
    if len(words) > 1:
        out.append("-".join(words))
        out.append(words[0])
    # Names already given as a slug ("razorpay-software") pass through as-is.
    raw = name.strip().lower()
    if raw not in out:
        out.insert(0, raw)
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:3]


# Each probe returns the number of live postings, or None when the board does
# not exist. Counting matters: several platforms answer 200 with an empty list
# for a slug that was real last year, and an empty board is not worth polling.
def _n(data, *keys) -> int:
    for k in keys:
        if isinstance(data, dict) and k in data:
            v = data[k]
            return len(v) if isinstance(v, list) else 0
    return len(data) if isinstance(data, list) else 0


def _json_probe(client, url, *keys, params=None):
    r = client.get(url, params=params)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return None
    try:
        return _n(r.json(), *keys)
    except ValueError:
        return None


PROBES = {
    "greenhouse": lambda c, s: _json_probe(
        c, f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "jobs"),
    "lever": lambda c, s: _json_probe(
        c, f"https://api.lever.co/v0/postings/{s}", params={"mode": "json"}),
    "ashby": lambda c, s: _json_probe(
        c, f"https://api.ashbyhq.com/posting-api/job-board/{s}", "jobs"),
    "smartrecruiters": lambda c, s: _json_probe(
        c, f"https://api.smartrecruiters.com/v1/companies/{s}/postings",
        "content", params={"limit": 10}),
    "workable": lambda c, s: _json_probe(
        c, f"https://apply.workable.com/api/v1/widget/accounts/{s}", "jobs"),
    "recruitee": lambda c, s: _json_probe(
        c, f"https://{s}.recruitee.com/api/offers/", "offers"),
    "bamboohr": lambda c, s: _json_probe(
        c, f"https://{s}.bamboohr.com/careers/list", "result"),
    "breezy": lambda c, s: _json_probe(c, f"https://{s}.breezy.hr/json"),
    "teamtailor": lambda c, s: _json_probe(
        c, f"https://{s}.teamtailor.com/jobs.json", "items"),
    "rippling": lambda c, s: _json_probe(
        c, f"https://api.rippling.com/platform/api/ats/v1/board/{s}/jobs"),
}


def probe(company: str, platform: str, slug: str) -> tuple:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            n = PROBES[platform](c, slug)
    except Exception:  # noqa: BLE001 - a probe failing is a "no", not an error
        n = None
    return company, platform, slug, n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="discover")
    ap.add_argument("--names", default="", help="comma-separated company names")
    ap.add_argument("--file", help="file with one company name per line")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--min-jobs", type=int, default=1,
                    help="ignore boards with fewer live postings than this")
    ap.add_argument("--yaml", action="store_true", help="print a config-ready YAML block")
    ap.add_argument("--out", help="write the YAML block to this file")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            names += [
                ln.strip() for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
    if not names:
        ap.error("give --names or --file")

    tasks = [
        (name, platform, slug)
        for name in names
        for slug in slug_variants(name)
        for platform in PROBES
    ]
    print(f"probing {len(names)} companies x {len(PROBES)} platforms "
          f"= {len(tasks)} checks", file=sys.stderr)

    found: dict[str, set] = defaultdict(set)
    hits = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(probe, *t) for t in tasks]
        for i, fut in enumerate(as_completed(futs), 1):
            company, platform, slug, n = fut.result()
            if n is not None and n >= args.min_jobs:
                found[platform].add(slug)
                hits += 1
                print(f"  HIT  {platform:<16} {slug:<28} {n:>4} jobs   ({company})")
            if i % 200 == 0:
                print(f"  ... {i}/{len(tasks)}", file=sys.stderr)

    covered = {c for c in names
               if any(s in found.get(p, ()) for p in found for s in slug_variants(c))}
    print(f"\n{hits} live boards for {len(covered)}/{len(names)} companies", file=sys.stderr)

    if args.yaml or args.out:
        block = yaml.safe_dump(
            {"sources": {p: sorted(v) for p, v in sorted(found.items())}},
            sort_keys=False, allow_unicode=True,
        )
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(block)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print("\n" + block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
