#!/usr/bin/env python3
"""Check which slugs in config/sources.yaml actually return jobs.

Companies migrate between ATS vendors, so roughly a fifth of any hand-built
slug list is dead at any moment. Run this after editing the config:

    python verify_slugs.py
    python verify_slugs.py --prune      # rewrite the config without dead slugs
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

from src.sources import Skipped, fetch_one, iter_targets

logging.basicConfig(level=logging.ERROR)

CONFIG = "config/sources.yaml"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true", help="rewrite config, dropping dead slugs")
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    targets = list(iter_targets(cfg))

    def probe(pair):
        platform, slug = pair
        try:
            return platform, slug, len(fetch_one(platform, slug))
        except Skipped:
            # A key-gated source with no key is not a dead slug. Report it as
            # such, and never let --prune delete it.
            return platform, slug, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, targets))

    live, dead, skipped = [], [], []
    for platform, slug, n in sorted(results, key=lambda r: (r[0], r[1])):
        label = slug or "(feed)"
        if n is None:
            skipped.append((platform, slug))
            print(f"  skip  {platform:<16} {label:<34}  needs credentials")
        elif n:
            live.append((platform, slug))
            print(f"  ok    {platform:<16} {label:<34} {n:>4} jobs")
        else:
            dead.append((platform, slug))
            print(f"  DEAD  {platform:<16} {label:<34}    0 jobs")

    print(f"\n{len(live)} live, {len(dead)} dead, {len(skipped)} skipped")

    if args.prune and dead:
        keep: dict[str, list] = {}
        for platform, slug in live + skipped:
            keep.setdefault(platform, [])
            if slug:
                keep[platform].append(slug)
        for platform in keep:
            if not keep[platform]:
                keep[platform] = None
        cfg["sources"] = keep
        with open(args.config, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
        print(f"pruned {len(dead)} dead entries from {args.config}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
