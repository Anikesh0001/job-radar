"""SQLite store. One file, committed back to the repo by the GitHub Action."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz

from .models import Job, canon_location, normalise, normalise_company

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    description TEXT,
    source      TEXT,
    posted_at   TEXT,
    salary      TEXT,
    first_seen  TEXT NOT NULL,
    -- Refreshed every time a run sees the posting again, so a listing that
    -- disappears can be aged out later without being re-notified now.
    last_seen   TEXT,
    notified    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS idx_dedupe ON jobs(company, title);

-- Small key/value store for state that is not a posting: when we last
-- delivered to Telegram, mainly. Kept in the database so it travels with the
-- state release and survives a runner being torn down.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per run. Without it the only history is GitHub's Actions log, which
-- ages out and cannot be queried: "is the feed healthy?" becomes a question
-- you answer by squinting at the last run rather than at a trend.
CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    kept       INTEGER NOT NULL DEFAULT 0,
    added      INTEGER NOT NULL DEFAULT 0,
    posted     INTEGER NOT NULL DEFAULT 0,
    queued     INTEGER NOT NULL DEFAULT 0,
    dead       INTEGER NOT NULL DEFAULT 0,
    seconds    REAL    NOT NULL DEFAULT 0
);

-- Per-target outcome of every run. Without this a source that quietly starts
-- returning zero — a migrated ATS, a renamed slug — looks identical to a
-- source with genuinely nothing new, and stays broken for weeks.
CREATE TABLE IF NOT EXISTS source_health (
    platform    TEXT NOT NULL,
    slug        TEXT NOT NULL,
    last_run    TEXT NOT NULL,
    last_ok     TEXT,
    last_count  INTEGER NOT NULL DEFAULT 0,
    zero_streak INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    PRIMARY KEY (platform, slug)
);
"""

# Two postings are "the same" above this rapidfuzz token_sort_ratio.
#
# token_sort_ratio, NOT token_set_ratio. token_set_ratio scores 100 whenever
# one title's tokens are a subset of the other's, so "Software Engineer"
# swallowed "Senior Software Engineer, Backend", "Data Engineer" swallowed
# "Senior Staff Data Engineer, Platform", and a full run lost ~6,000 genuinely
# distinct postings that way. token_sort_ratio is order-insensitive but still
# length-sensitive, which is the property we actually wanted.
#
# Erring high is deliberate: a false merge loses a job permanently, while a
# false split only shows you a near-duplicate.
FUZZY_THRESHOLD = 95

# Bump whenever Job.fingerprint changes, so existing databases get rebuilt
# instead of quietly duplicating every row they already hold.
FINGERPRINT_VERSION = 2

# Job descriptions are used by the filters at fetch time and never read back
# out of the database — not by the export, not by the notifier. Persisting them
# made jobs.db 63MB, 94% of it description text, which the scheduled workflow
# then committed every three hours: 522MB of git history a day. Store a short
# prefix for debugging and drop the rest.
DESCRIPTION_KEEP = 200


# Postings older than this are deleted on each run. A job board listing is
# stale long before this; without a cap the database grows for ever, and every
# byte of it is re-committed by CI on every run.
PRUNE_AFTER_DAYS = 60

# Postings arrive faster than Telegram can announce them, so the queue always
# has a backlog. Anything that ages out while waiting is dropped rather than
# posted stale — otherwise the queue only ever grows and the channel would one
# day be advertising month-old roles.
QUEUE_MAX_AGE_DAYS = 14


# Cities and the trailing country code, matched against a posting's location.
# Kept here rather than imported from filters so the queue ordering does not
# depend on how a particular run happened to be configured.
_INDIA = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|noida|"
    r"gurgaon|gurugram|kolkata|ahmedabad|jaipur|kochi|coimbatore|indore|"
    r"chandigarh|thiruvananthapuram|trivandrum|visakhapatnam|nagpur|mysore|"
    r"mysuru|bhubaneswar|vadodara|surat|lucknow|goa)\b|,\s*IN\s*$",
    re.I,
)


def _looks_indian(location: str) -> bool:
    return bool(_INDIA.search(location or ""))


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "jobs.db"):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring a database written by an older version up to date."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "last_seen" not in have:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
            self.conn.execute("UPDATE jobs SET last_seen = first_seen")
        if "salary" not in have:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary TEXT")

        if self.conn.execute("PRAGMA user_version").fetchone()[0] < FINGERPRINT_VERSION:
            self._rebuild_fingerprints()
            self.conn.execute(f"PRAGMA user_version = {FINGERPRINT_VERSION}")

    def _rebuild_fingerprints(self) -> None:
        """Recompute every fingerprint after the formula changes.

        Without this a formula change is silently destructive: nothing in the
        table matches any newly computed fingerprint, so the next run treats
        all 11,000 stored postings as new and inserts a second copy of each.

        Rows are replayed oldest-first into a fresh table with INSERT OR
        IGNORE, so pairs that the new formula considers identical collapse to
        one and the earliest first_seen is the copy that survives.
        """
        rows = self.conn.execute(
            """SELECT fingerprint, company, title, url, location, description,
                      source, posted_at, salary, first_seen, last_seen, notified
               FROM jobs ORDER BY first_seen ASC"""
        ).fetchall()
        if not rows:
            return

        self.conn.executescript(
            SCHEMA.replace("TABLE IF NOT EXISTS jobs", "TABLE IF NOT EXISTS jobs_rebuild")
                  .replace("INDEX IF NOT EXISTS idx_first_seen ON jobs",
                           "INDEX IF NOT EXISTS idx_rb_first_seen ON jobs_rebuild")
                  .replace("INDEX IF NOT EXISTS idx_dedupe ON jobs",
                           "INDEX IF NOT EXISTS idx_rb_dedupe ON jobs_rebuild")
        )
        for r in rows:
            fp = Job(
                company=r["company"], title=r["title"], url=r["url"] or "",
                location=r["location"] or "",
            ).fingerprint
            self.conn.execute(
                """INSERT OR IGNORE INTO jobs_rebuild
                   (fingerprint, company, title, url, location, description,
                    source, posted_at, salary, first_seen, last_seen, notified)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fp, r["company"], r["title"], r["url"], r["location"],
                 r["description"], r["source"], r["posted_at"], r["salary"],
                 r["first_seen"], r["last_seen"], r["notified"]),
            )
        kept = self.conn.execute("SELECT COUNT(*) FROM jobs_rebuild").fetchone()[0]
        self.conn.execute("DROP TABLE jobs")
        self.conn.execute("ALTER TABLE jobs_rebuild RENAME TO jobs")
        self.conn.executescript(SCHEMA)  # recreate the indexes on the new table
        log.info("fingerprint migration: %d rows -> %d (%d merged)",
                 len(rows), kept, len(rows) - kept)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _all_fingerprints(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT fingerprint FROM jobs")}

    def _all_posting_keys(self) -> set[tuple]:
        """(company, title, url) for everything stored.

        The fingerprint deliberately ignores the URL so one role reached via
        two sources collapses to one row. That leaves the opposite case open:
        Rippling advertises a single remote job against thirteen US states, all
        on the SAME url, and the fingerprint saw thirteen distinct postings.

        Company and title are part of the key because a URL is not always one
        job — a truncated Oracle Cloud link and a reused Indeed link both cover
        two different titles in the live data, and collapsing those would lose
        a real posting.
        """
        return {
            (normalise_company(c), normalise(t), (u or "").strip())
            for c, t, u in self.conn.execute("SELECT company, title, url FROM jobs")
        }

    def _keys_by_company(self) -> dict[str, list[str]]:
        """company -> its stored dedupe keys, loaded once per run.

        The old code ran one SELECT per candidate job. At a few hundred
        postings that was invisible; at the tens of thousands these sources now
        return it was the slowest thing in the pipeline. One scan instead.

        These MUST be built the same way as Job.dedupe_key. Comparing a
        normalised candidate against a raw database row scored 71 instead of
        100 on nothing but capitalisation, which quietly disabled the whole
        fuzzy pass between runs — it worked within one run, where the keys were
        built in memory, and silently failed on every run after.
        """
        by: dict[str, list[str]] = {}
        for company, title, location in self.conn.execute(
            "SELECT company, title, location FROM jobs"
        ):
            key = (
                f"{normalise(company)} {normalise(title)} "
                f"{canon_location(location or '')}"
            )
            by.setdefault(company, []).append(key)
        return by

    def insert_new(self, jobs: list[Job]) -> list[Job]:
        """Insert jobs not already stored. Returns only the genuinely new ones.

        Two passes: exact fingerprint match first (cheap), then fuzzy title
        match scoped to the same company (catches 'SDE Intern' vs 'Software
        Development Engineer Intern').
        """
        new: list[Job] = []
        seen_this_run: set[str] = set()
        known = self._all_fingerprints()
        posting_keys = self._all_posting_keys()
        keys = self._keys_by_company()
        now = _utcnow()

        for job in jobs:
            fp = job.fingerprint
            if fp in seen_this_run:
                continue
            if fp in known:
                # Already had it: not new, but it is still live, so record that
                # — and backfill any field that was empty when we first stored
                # it. Otherwise a parser fix never reaches the rows already in
                # the table: adding the arbeitnow date left 101 postings blank
                # for ever, because a re-seen job only touched last_seen.
                self.conn.execute(
                    """UPDATE jobs SET
                           last_seen   = ?,
                           posted_at   = COALESCE(NULLIF(posted_at, ''), ?),
                           salary      = COALESCE(NULLIF(salary, ''), ?),
                           location    = COALESCE(NULLIF(location, ''), ?),
                           description = COALESCE(NULLIF(description, ''), ?)
                       WHERE fingerprint = ?""",
                    (now, job.posted_at, job.salary, job.location,
                     (job.description or "")[:DESCRIPTION_KEEP], fp),
                )
                seen_this_run.add(fp)
                continue

            # Same company, same title, same URL: one posting, however many
            # locations the source listed it under.
            posting_key = (
                normalise_company(job.company),
                normalise(job.title),
                (job.url or "").strip(),
            )
            if posting_key in posting_keys:
                self.conn.execute(
                    "UPDATE jobs SET last_seen = ? WHERE url = ?", (now, job.url)
                )
                continue
            posting_keys.add(posting_key)

            key = job.dedupe_key
            company_keys = keys.get(job.company, ())
            if any(
                fuzz.token_sort_ratio(key, existing) >= FUZZY_THRESHOLD
                for existing in company_keys
            ):
                log.debug("fuzzy duplicate skipped: %s", job)
                continue

            row = job.to_row()
            row["last_seen"] = now
            row["description"] = (row.get("description") or "")[:DESCRIPTION_KEEP]
            self.conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (fingerprint, company, title, url, location, description,
                    source, posted_at, salary, first_seen, last_seen)
                   VALUES (:fingerprint, :company, :title, :url, :location,
                           :description, :source, :posted_at, :salary,
                           :first_seen, :last_seen)""",
                row,
            )
            seen_this_run.add(fp)
            known.add(fp)
            # Keep the in-memory index current so two near-identical postings
            # inside the SAME run still collapse to one.
            keys.setdefault(job.company, []).append(key)
            new.append(job)

        self.conn.commit()
        return new

    # -- source health ------------------------------------------------------

    def record_health(self, results: list[tuple[str, str, int, str | None]]) -> None:
        """results: (platform, slug, count, error_or_None) for every target."""
        now = _utcnow()
        for platform, slug, count, err in results:
            prev = self.conn.execute(
                "SELECT zero_streak FROM source_health WHERE platform=? AND slug=?",
                (platform, slug),
            ).fetchone()
            streak = (prev["zero_streak"] if prev else 0) + 1 if count == 0 else 0
            self.conn.execute(
                """INSERT INTO source_health
                       (platform, slug, last_run, last_ok, last_count,
                        zero_streak, last_error)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(platform, slug) DO UPDATE SET
                       last_run    = excluded.last_run,
                       last_ok     = COALESCE(excluded.last_ok, source_health.last_ok),
                       last_count  = excluded.last_count,
                       zero_streak = excluded.zero_streak,
                       last_error  = excluded.last_error""",
                (platform, slug, now, now if count else None, count, streak, err),
            )
        self.conn.commit()

    def unhealthy(self, min_streak: int = 3) -> list[sqlite3.Row]:
        """Targets that have returned nothing for several runs running."""
        return self.conn.execute(
            """SELECT * FROM source_health
               WHERE zero_streak >= ? ORDER BY zero_streak DESC, platform, slug""",
            (min_streak,),
        ).fetchall()

    def mark_notified(self, jobs: list[Job]) -> None:
        self.conn.executemany(
            "UPDATE jobs SET notified = 1 WHERE fingerprint = ?",
            [(j.fingerprint,) for j in jobs],
        )
        self.conn.commit()

    def pending(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE notified = 0 ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def pending_jobs(self, limit: int = 50) -> list[Job]:
        """Undelivered postings, newest first, as Job objects.

        The alert queue lives in the database rather than in the run that found
        the postings. A run that fetches 800 new jobs can only post ~40 of them
        inside Telegram's rate limit, and the other 760 have to survive until
        the next run instead of being silently dropped.
        """
        # Newest first, but pull a generous slice so the round-robin below has
        # something from every source to work with.
        rows = self.conn.execute(
            """SELECT * FROM jobs WHERE notified = 0
               ORDER BY COALESCE(posted_at, first_seen) DESC, first_seen DESC
               LIMIT ?""",
            (max(limit * 20, 200),),
        ).fetchall()

        # Interleave the sources. A run inserts source by source, so ordering
        # purely by date posts twenty LinkedIn jobs, then twenty Instahyre, then
        # twenty Greenhouse — the channel reads like three separate feeds glued
        # together. Round-robin gives every source a turn, and keeps each
        # source's own newest first.
        by_source: dict[str, list] = {}
        for r in rows:
            by_source.setdefault(r["source"] or "", []).append(r)

        # India first within each source. The channel is aimed at India, and
        # remote-anywhere roles are the bonus rather than the main event; doing
        # this per source instead of globally keeps the round-robin below
        # varied, so the feed never becomes ten Instahyre posts in a row.
        for entries in by_source.values():
            entries.sort(key=lambda r: 0 if _looks_indian(r["location"] or "") else 1)

        order = sorted(by_source, key=lambda s: -len(by_source[s]))
        picked, i = [], 0
        while len(picked) < limit and any(by_source[s] for s in order):
            source = order[i % len(order)]
            if by_source[source]:
                picked.append(by_source[source].pop(0))
            i += 1

        return [
            Job(
                company=r["company"], title=r["title"], url=r["url"],
                source=r["source"] or "", location=r["location"] or "",
                description=r["description"] or "", posted_at=r["posted_at"],
                salary=r["salary"] or "", first_seen=r["first_seen"],
            )
            for r in picked
        ]

    def pending_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE notified = 0"
        ).fetchone()[0]

    def reset_notified(self) -> int:
        """Put every stored posting back in the alert queue.

        The counterpart to mark_all_notified, and the right choice for a public
        feed: the backlog then drips out at the per-run limit alongside new
        finds, so the channel stays active instead of going quiet whenever a
        run happens to turn up only three new jobs.
        """
        n = self.count()
        self.conn.execute("UPDATE jobs SET notified = 0")
        self.conn.commit()
        return n

    # -- key/value state ----------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def hours_since_last_post(self, default: float = 1.0) -> float:
        """How long since anything was delivered, in hours.

        Drives the catch-up quota. GitHub skips roughly half of all scheduled
        slots on a low-activity public repo — measured at 53%, with gaps of
        2.6 to 5 hours against an hourly cron — so a fixed batch per run makes
        the channel's output depend on GitHub's mood. Sizing each batch by
        elapsed time instead keeps the daily rate steady however erratic the
        runs are.
        """
        stamp = self.get_meta("last_post_at")
        if not stamp:
            return default
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return default
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        hours = (datetime.now(UTC) - last).total_seconds() / 3600
        return max(hours, 0.0)

    def expire_queue(self, days: int = QUEUE_MAX_AGE_DAYS) -> int:
        """Silently drop queued postings older than `days`.

        Marks them delivered rather than deleting them: they stay in the
        spreadsheet and still count for deduplication, they just never get
        announced. Undated postings are left alone — there is no way to tell
        whether they are stale.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(days=days)
        ).isoformat(timespec="seconds")
        cur = self.conn.execute(
            """UPDATE jobs SET notified = 1
               WHERE notified = 0 AND posted_at IS NOT NULL AND posted_at != ''
                 AND posted_at < ?""",
            (cutoff,),
        )
        self.conn.commit()
        return cur.rowcount or 0

    def mark_all_notified(self) -> int:
        """Baseline the queue: treat everything already stored as delivered.

        Without this, switching alerts on for the first time means a backlog of
        every posting ever collected — 12,000 messages at one per posting, or
        about ten hours of Telegram rate limit. Almost everyone wants alerts
        for what turns up *from now on*.
        """
        n = self.pending_count()
        self.conn.execute("UPDATE jobs SET notified = 1 WHERE notified = 0")
        self.conn.commit()
        return n

    def all_jobs(self, limit: int | None = None) -> list[Job]:
        """Every stored posting, newest first — used by --export-all."""
        sql = "SELECT * FROM jobs ORDER BY first_seen DESC, company, title"
        rows = self.conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        return [
            Job(
                company=r["company"],
                title=r["title"],
                url=r["url"],
                source=r["source"] or "",
                location=r["location"] or "",
                description=r["description"] or "",
                posted_at=r["posted_at"], salary=r["salary"] or "",
                first_seen=r["first_seen"],
            )
            for r in rows
        ]

    def record_run(self, **row) -> None:
        cols = ("started_at", "mode", "fetched", "kept", "added", "posted",
                "queued", "dead", "seconds")
        values = {c: row.get(c, 0) for c in cols}
        values["started_at"] = row.get("started_at") or _utcnow()
        values["mode"] = row.get("mode") or "fetch"
        self.conn.execute(
            f"INSERT OR REPLACE INTO runs ({','.join(cols)}) "
            f"VALUES ({','.join(':' + c for c in cols)})",
            values,
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def prune(self, days: int = PRUNE_AFTER_DAYS) -> int:
        """Delete postings not seen for `days`, then reclaim the space.

        Uses last_seen, not first_seen: a role that has been open for six
        months is still open, and deleting it would make the next run
        rediscover it as brand new and re-announce it.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(days=days)
        ).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "DELETE FROM jobs WHERE COALESCE(last_seen, first_seen) < ?", (cutoff,)
        )
        removed = cur.rowcount or 0
        self.conn.commit()
        if removed:
            # VACUUM cannot run inside a transaction, and without it SQLite
            # keeps the freed pages — the file never actually shrinks.
            self.conn.isolation_level = None
            self.conn.execute("VACUUM")
            self.conn.isolation_level = ""
            log.info("pruned %d posting(s) unseen for %d+ days", removed, days)
        return removed

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
