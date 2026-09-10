"""Entry point.  python -m src.run [--dry-run] [--no-filter] [--config PATH]"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .db import PRUNE_AFTER_DAYS, QUEUE_MAX_AGE_DAYS, Store
from .export import export
from .filters import Filter
from .models import Job
from .notify import (
    DEFAULT_RATE_PER_HOUR,
    MAX_CATCHUP,
    catchup_quota,
    send_discord,
    send_telegram,
    write_markdown,
)
from .sources import Skipped, fetch_one, iter_targets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("job-radar")

# httpx logs every request at INFO. With 200+ targets, several of them paged,
# that buries the per-source summary under thousands of lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_profile_quietly(path: str):
    """The profile is optional: without one the feed simply has no scores."""
    if not Path(path).exists():
        return None
    try:
        from .resume import load_profile
        return load_profile(path)
    except Exception as e:
        log.warning("could not read %s (%s) — running without match scores",
                    path, e)
        return None


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def fetch_all(config: dict, workers: int = 6) -> tuple[list[Job], list[tuple]]:
    """Fetch every configured target.

    Returns (jobs, health) where health is one (platform, slug, count, error)
    row per target, so the caller can tell "this source returned nothing" from
    "this source was never asked".
    """
    targets = list(iter_targets(config))
    log.info("fetching %d targets with %d workers", len(targets), workers)
    jobs: list[Job] = []
    health: list[tuple] = []

    def task(pair):
        platform, slug = pair
        # Jitter so we never look like a burst to any single host.
        time.sleep(random.uniform(0.2, 1.0))
        return fetch_one(platform, slug)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, t): t for t in targets}
        for fut in as_completed(futures):
            platform, slug = futures[fut]
            try:
                got = fut.result()
                health.append((platform, slug, len(got), None))
                jobs.extend(got)
            except Skipped as e:
                # Deliberate, not a failure — record the reason so --health can
                # say "needs a key" instead of implying the source is broken.
                health.append((platform, slug, 0, f"skipped: {e}"))
            except Exception as e:
                log.warning("%s/%s crashed: %s", platform, slug, e)
                health.append((platform, slug, 0, f"{type(e).__name__}: {e}"))
            done += 1
            if done % 25 == 0:
                log.info("  ...%d/%d targets, %d postings so far",
                         done, len(targets), len(jobs))

    log.info("fetched %d raw postings from %d targets", len(jobs), len(targets))
    return jobs, health


def summarise(raw: list[Job], relevant: list[Job], health: list[tuple]) -> None:
    """Per-source table. The number that matters is 'kept', not 'fetched' —
    a source pulling 4,000 postings of which none are IT roles is dead weight."""
    from collections import Counter

    fetched = Counter(j.source for j in raw)
    kept = Counter(j.source for j in relevant)
    dead = [(p, s) for p, s, n, _ in health if n == 0]

    log.info("")
    log.info("%-26s %9s %9s", "source", "fetched", "kept")
    log.info("%s", "-" * 46)
    for src in sorted(fetched, key=lambda k: -fetched[k]):
        log.info("%-26s %9d %9d", src, fetched[src], kept.get(src, 0))
    log.info("%s", "-" * 46)
    log.info("%-26s %9d %9d", "TOTAL", len(raw), len(relevant))
    if dead:
        log.info("%d target(s) returned nothing this run "
                 "(run --health for the persistent ones)", len(dead))

    # GitHub renders this on the run's summary page, so the state of the feed
    # is legible without opening a log and scrolling.
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [
            f"## {len(relevant)} postings kept of {len(raw)} fetched", "",
            "| source | fetched | kept |", "|---|---:|---:|",
        ]
        lines += [f"| {src} | {fetched[src]} | {kept.get(src, 0)} |"
                  for src in sorted(fetched, key=lambda k: -fetched[k])]
        lines += ["", f"{len(health) - len(dead)} of {len(health)} targets "
                      f"returned something."]
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            log.debug("could not write step summary: %s", e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="job-radar")
    ap.add_argument("--config", default="config/sources.yaml")
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument(
        "--profile", default="profile.yaml", metavar="PATH",
        help="resume profile used to score postings; optional, and the feed "
             "runs unscored without it (build one with: python apply.py profile)",
    )
    ap.add_argument("--dry-run", action="store_true", help="fetch and filter, send nothing")
    ap.add_argument("--no-filter", action="store_true", help="keep every posting")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--export",
        default="jobs.xlsx",
        metavar="PATH",
        help="write results to a file; format from the extension "
        "(.xlsx/.csv/.txt). Default: jobs.xlsx. Pass '' to skip.",
    )
    # The export defaults to the WHOLE database, not this run's new postings.
    # The other way round was a trap: the sheet you actually work from got
    # silently replaced with a 25-row digest the moment you ran without
    # --export-all, and nothing in the output made that look like data loss.
    ap.add_argument(
        "--export-new-only",
        action="store_true",
        help="export only this run's new postings instead of the whole database",
    )
    ap.add_argument(
        "--export-all",
        action="store_true",
        help=argparse.SUPPRESS,  # now the default; kept so old commands still work
    )
    ap.add_argument(
        "--post-only", action="store_true",
        help="skip fetching and just post what is already queued; use this on a "
             "short schedule to keep the channel ticking between fetches",
    )
    ap.add_argument(
        "--queue-max-age", type=int, default=QUEUE_MAX_AGE_DAYS, metavar="N",
        help=f"drop queued postings older than N days instead of announcing "
             f"them late (default {QUEUE_MAX_AGE_DAYS}); 0 keeps everything",
    )
    ap.add_argument(
        "--prune-days", type=int, default=PRUNE_AFTER_DAYS, metavar="N",
        help=f"delete postings unseen for N days (default {PRUNE_AFTER_DAYS}); "
             "0 disables pruning",
    )
    ap.add_argument(
        "--notify-limit", type=int, default=None, metavar="N",
        help="send exactly N messages this run, overriding the rate-based "
             "catch-up",
    )
    ap.add_argument(
        "--min-interval", type=int, default=20, metavar="MINUTES",
        help="never post twice within this many minutes, however often the run "
             "is triggered (default 20); 0 disables the floor",
    )
    ap.add_argument(
        "--notify-rate", type=int, default=DEFAULT_RATE_PER_HOUR, metavar="N",
        help=f"target postings per hour (default {DEFAULT_RATE_PER_HOUR}); each "
             f"run sends this times the hours since the last one, capped at "
             f"{MAX_CATCHUP}",
    )
    ap.add_argument(
        "--mark-all-notified", action="store_true",
        help="mark everything already stored as delivered and exit — run this "
             "once when switching alerts on, so you get new postings from now "
             "on instead of the entire backlog",
    )
    ap.add_argument(
        "--reset-notified", action="store_true",
        help="put every stored posting back in the alert queue, so the backlog "
             "drips out at --notify-limit per run alongside new finds",
    )
    ap.add_argument(
        "--rescore", action="store_true",
        help="re-apply the filters and re-score every stored posting, then "
             "exit; run this after editing filters, the skill list or your CV",
    )
    ap.add_argument(
        "--stats", action="store_true",
        help="show recent run history and exit",
    )
    ap.add_argument(
        "--check-config", action="store_true",
        help="validate the config files and exit; use this in CI",
    )
    ap.add_argument(
        "--health",
        action="store_true",
        help="print sources that have returned nothing for several runs, then exit",
    )
    ap.add_argument(
        "--min-streak", type=int, default=3,
        help="how many consecutive empty runs counts as broken (--health)",
    )
    args = ap.parse_args(argv)

    if args.rescore:
        from .match import score as match_score
        config = load_config(args.config)
        f = Filter(config)
        profile = _load_profile_quietly(args.profile)
        with Store(args.db) as store:
            rows = store.conn.execute("SELECT * FROM jobs").fetchall()
            dropped = rescored = 0
            for r in rows:
                job = Job(
                    company=r["company"], title=r["title"], url=r["url"],
                    location=r["location"] or "", source=r["source"] or "",
                    description=r["description"] or "", posted_at=r["posted_at"],
                )
                reason = f.reason(job)
                if reason:
                    store.conn.execute("DELETE FROM jobs WHERE fingerprint = ?",
                                       (r["fingerprint"],))
                    dropped += 1
                    continue
                if profile:
                    # Note the stored description is truncated, so a rescore is
                    # a little blunter than the score taken at fetch time with
                    # the full text in hand.
                    new = match_score(job, profile).score
                    if new != r["match_score"]:
                        store.conn.execute(
                            "UPDATE jobs SET match_score = ? WHERE fingerprint = ?",
                            (new, r["fingerprint"]))
                        rescored += 1
            store.conn.commit()
            print(f"dropped {dropped} posting(s) the filters now reject")
            print(f"rescored {rescored} of {len(rows) - dropped} remaining")
        return 0

    if args.stats:
        with Store(args.db) as store:
            rows = store.recent_runs(20)
            if not rows:
                print("no runs recorded yet")
                return 0
            print(f"  {'when':<20}{'mode':<11}{'fetched':>8}{'kept':>7}"
                  f"{'new':>6}{'sent':>6}{'queued':>8}{'dead':>6}{'secs':>7}")
            for r in rows:
                print(f"  {r['started_at'][:19].replace('T', ' '):<20}"
                      f"{r['mode']:<11}{r['fetched']:>8}{r['kept']:>7}"
                      f"{r['added']:>6}{r['posted']:>6}{r['queued']:>8}"
                      f"{r['dead']:>6}{r['seconds']:>7.0f}")
            total = sum(r["posted"] for r in rows)
            print(f"\n  {len(rows)} run(s), {total} posting(s) delivered")
        return 0

    if args.check_config:
        from .validate import report
        extra = [p for p in ("config/sources.yaml", "config/fast.yaml")
                 if p != args.config and Path(p).exists()]
        return report([args.config, *extra])

    if args.mark_all_notified:
        with Store(args.db) as store:
            n = store.mark_all_notified()
            print(f"marked {n} stored posting(s) as already delivered")
            print("alerts will now cover only postings found from here on")
        return 0

    if args.reset_notified:
        with Store(args.db) as store:
            n = store.reset_notified()
            print(f"queued {n} posting(s) for delivery")
            print(f"at the default limit that is ~{n // 40 + 1} run(s) to drain")
        return 0

    if args.health:
        # Only report what is still configured. Removing a dead slug from the
        # config leaves its row in source_health for ever, and a report that
        # keeps naming slugs you already deleted trains you to ignore it.
        configured = None
        if Path(args.config).exists():
            configured = {(p, s) for p, s in iter_targets(load_config(args.config))}
        with Store(args.db) as store:
            rows = store.unhealthy(args.min_streak)
            if configured is not None:
                rows = [r for r in rows if (r["platform"], r["slug"]) in configured]
            if not rows:
                print(f"no source has been empty for {args.min_streak}+ runs")
                return 0
            print(f"{len(rows)} target(s) empty for {args.min_streak}+ consecutive runs:")
            print(f"  {'platform':<18}{'slug':<34}{'runs':>5}  last ok / error")
            for r in rows:
                note = r["last_error"] or (f"last ok {r['last_ok'][:10]}"
                                           if r["last_ok"] else "never returned anything")
                print(f"  {r['platform']:<18}{(r['slug'] or '(feed)')[:33]:<34}"
                      f"{r['zero_streak']:>5}  {note[:60]}")
            print("\nFix the slug in config/sources.yaml, "
                  "or drop it with: python verify_slugs.py --prune")
        return 0

    if not Path(args.config).exists():
        log.error("config not found: %s", args.config)
        return 1

    started = datetime.now(UTC)
    stats = {"fetched": 0, "kept": 0, "dead": 0}
    sent_jobs: list[Job] = []

    store = Store(args.db)
    try:
        # --post-only skips the fetch entirely and just drains the queue. That
        # is what makes a steady feed possible: fetching 209 sources every hour
        # would hammer them for no benefit, but posting every hour from what is
        # already stored costs nothing and keeps the channel alive between
        # fetches instead of going quiet for four hours at a time.
        new: list[Job] = []
        if not args.post_only:
            config = load_config(args.config)
            raw, health = fetch_all(config, workers=args.workers)

            if args.no_filter:
                relevant = raw
            else:
                relevant = Filter(config).apply(raw)

            # Score against the resume while the full description is still in
            # hand — the stored copy is truncated, so this cannot be done
            # later without refetching.
            profile = _load_profile_quietly(args.profile)
            if profile:
                from .match import job_skills
                from .match import score as match_score
                for job in relevant:
                    # Capture the skills BEFORE the description is truncated
                    # on the way into storage.
                    job.skills = ",".join(sorted(job_skills(job)))
                    job.match_score = match_score(job, profile).score
            log.info("%d postings passed filters", len(relevant))
            summarise(raw, relevant, health)
            stats.update(fetched=len(raw), kept=len(relevant),
                         dead=sum(1 for _, _, n, _ in health if n == 0))

            store.record_health(health)
            new = store.insert_new(relevant)
            if args.prune_days:
                store.prune(args.prune_days)
            log.info("%d new after dedupe (%d total in db)", len(new), store.count())
            for j in new:
                print(f"  NEW  {j}")

            if args.export:
                rows = new if args.export_new_only else store.all_jobs()
                if rows:
                    export(rows, args.export)
                elif args.export_new_only:
                    log.info("nothing new to export (the full sheet is the "
                             "default; drop --export-new-only to rewrite it)")
                else:
                    log.info("nothing to export")

        # Postings arrive faster than Telegram can announce them, so anything
        # that ages out while queued is dropped rather than posted late. Without
        # this the queue only grows, and the oldest entries can never surface.
        if args.queue_max_age:
            expired = store.expire_queue(args.queue_max_age)
            if expired:
                log.info("dropped %d queued posting(s) older than %d days",
                         expired, args.queue_max_age)

        if args.dry_run:
            log.info("dry run — not sending (%d queued)", store.pending_count())
            return 0

        # Deliberately NOT gated on `new`: a run that finds nothing still has a
        # backlog to work through, and the earlier version returned before this
        # point, so the channel went silent whenever a fetch added nothing.
        # Size the batch by how long it has actually been, not by how long the
        # cron says it should have been. GitHub skips about half of all
        # scheduled slots on a repo like this, so a fixed batch makes the
        # channel's output depend on GitHub's mood rather than on a rate we
        # chose. --notify-limit overrides this with a flat number.
        if args.notify_limit is not None:
            quota = args.notify_limit
            gap = None
        else:
            gap = store.hours_since_last_post()
            # Once external schedulers are triggering this as well as GitHub's
            # own cron, runs can arrive minutes apart — a queued trigger, a
            # manual click, two services firing at once. Without a floor the
            # channel gets a trickle of two-message bursts instead of a feed.
            if gap * 60 < args.min_interval:
                log.info("only %.0f minutes since the last post (floor is %d); "
                         "fetched and stored, posting nothing", gap * 60,
                         args.min_interval)
                quota = 0
            else:
                quota = catchup_quota(gap, args.notify_rate)

        queue = store.pending_jobs(quota)
        outstanding = store.pending_count()
        if queue:
            if gap is None:
                log.info("telegram: posting %d of %d queued", len(queue), outstanding)
            else:
                log.info("telegram: %.1fh since the last post, so sending %d of "
                         "%d queued (target %d/hour)",
                         gap, len(queue), outstanding, args.notify_rate)
            sent = send_telegram(queue, on_sent=lambda j: store.mark_notified([j]))
            sent_jobs = sent
            if sent:
                store.set_meta("last_post_at",
                               datetime.now(UTC).isoformat(timespec="seconds"))
            left = store.pending_count()
            if left:
                log.info("telegram: %d still queued, will go out next run", left)
        else:
            log.info("nothing queued to post")

        if new:
            write_markdown(new)
            send_discord(new)

        store.record_run(
            started_at=started.isoformat(timespec="seconds"),
            mode="post-only" if args.post_only else "fetch",
            fetched=stats["fetched"], kept=stats["kept"], added=len(new),
            posted=len(sent_jobs), queued=store.pending_count(),
            dead=stats["dead"],
            seconds=round((datetime.now(UTC) - started).total_seconds(), 1),
        )
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
