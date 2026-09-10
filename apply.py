#!/usr/bin/env python3
"""Apply to the jobs the radar found.

    python apply.py profile                 # build/refresh profile.yaml from your CV
    python apply.py list                    # best matches, ranked
    python apply.py show 3                  # the posting, its form, a draft letter
    python apply.py open 3                  # open the apply page in your browser
    python apply.py fill 3                  # browser autofill — you press submit
    python apply.py log 3                   # record that you applied
    python apply.py status                  # what you have applied to

Runs entirely on your machine. Nothing here touches GitHub Actions: the
scheduled workflow is for finding and announcing jobs, and applying needs your
resume, your judgement and a browser.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from src.db import Store
from src.letters import draft
from src.match import score
from src.models import Job
from src.resume import build_profile_yaml, load_answers, load_profile

logging.basicConfig(level=logging.WARNING, format="%(message)s")

APPLICATIONS_DB = "applications.db"
LETTERS_DIR = Path("cover-letters")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    fingerprint TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    match_score INTEGER,
    applied_at  TEXT NOT NULL,
    method      TEXT,
    status      TEXT NOT NULL DEFAULT 'applied',
    notes       TEXT
);
"""


def _apps() -> sqlite3.Connection:
    conn = sqlite3.connect(APPLICATIONS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _already_applied() -> set[str]:
    with _apps() as conn:
        return {r[0] for r in conn.execute("SELECT fingerprint FROM applications")}


def shortlist(db: str, profile, limit: int, min_score: int,
              india_only: bool) -> list[Job]:
    """Best unapplied matches, best first."""
    applied = _already_applied()
    with Store(db) as store:
        rows = store.conn.execute(
            """SELECT * FROM jobs
               WHERE match_score IS NOT NULL AND match_score >= ?
               ORDER BY match_score DESC, COALESCE(posted_at, first_seen) DESC
               LIMIT 600""",
            (min_score,),
        ).fetchall()

    out: list[Job] = []
    for r in rows:
        if r["fingerprint"] in applied:
            continue
        loc = (r["location"] or "").lower()
        if india_only and not any(
            k in loc for k in ("india", " in", "bangalore", "bengaluru", "hyderabad",
                               "pune", "chennai", "mumbai", "delhi", "noida",
                               "gurgaon", "gurugram", "remote")
        ):
            continue
        out.append(Job(
            company=r["company"], title=r["title"], url=r["url"],
            location=r["location"] or "", source=r["source"] or "",
            description=r["description"] or "", posted_at=r["posted_at"],
            salary=r["salary"] or "", match_score=r["match_score"],
            first_seen=r["first_seen"],
        ))
        if len(out) >= limit:
            break
    return out


def cmd_profile(args) -> int:
    resume = Path(args.resume)
    if not resume.exists():
        print(f"no resume at {resume}. Put your CV there, or pass --resume PATH.")
        return 1
    out = build_profile_yaml(resume, args.profile)
    p = load_profile(args.profile)
    blank = [k for k, v in load_answers(args.profile).items() if not v]
    print(f"wrote {out}")
    print(f"  name    {p.name}")
    print(f"  title   {p.headline}")
    print(f"  years   {p.years_experience:g}")
    print(f"  skills  {len(p.skills)}: {', '.join(p.skills[:14])}...")
    if blank:
        print(f"\nFill these in {out} before applying — forms ask for them "
              f"constantly:\n  {', '.join(blank)}")
    return 0


def cmd_list(args) -> int:
    profile = load_profile(args.profile)
    jobs = shortlist(args.db, profile, args.limit, args.min_score, not args.anywhere)
    if not jobs:
        print("nothing above the score threshold that you have not already applied to.")
        return 0
    print(f"  {'#':<4}{'fit':>4}  {'company':<22}{'title':<44}{'location':<24}salary")
    for i, j in enumerate(jobs, 1):
        print(f"  {i:<4}{j.match_score:>3}%  {j.company[:20]:<22}{j.title[:42]:<44}"
              f"{(j.location or '')[:22]:<24}{(j.salary or '')[:18]}")
    print("\n  python apply.py show N   to see one in full")
    return 0


def _pick(args) -> tuple[Job, object]:
    profile = load_profile(args.profile)
    jobs = shortlist(args.db, profile, max(args.n, args.limit), args.min_score,
                     not args.anywhere)
    if args.n < 1 or args.n > len(jobs):
        print(f"pick a number between 1 and {len(jobs)}")
        sys.exit(1)
    return jobs[args.n - 1], profile


def cmd_show(args) -> int:
    from src.apply_forms import describe

    job, profile = _pick(args)
    m = score(job, profile)
    print(f"\n  {job.title}")
    print(f"  {job.company}  ·  {job.location or 'location not stated'}"
          f"{'  ·  ' + job.salary if job.salary else ''}")
    print(f"  {job.url}\n")
    print(f"  match {m.score}% ({m.label})")
    for r in m.reasons:
        print(f"    - {r}")
    if m.missing:
        print(f"    - not on your CV: {', '.join(m.missing)}")

    print("\n  application form:")
    spec = describe(job.url)
    print(f"    {spec.ats}, {len(spec.questions)} question(s)"
          f"{', resume upload' if spec.needs_resume_file else ''}")
    if spec.note:
        print(f"    ({spec.note})")
    for q in spec.extra_questions[:12]:
        print(f"      {'*' if q.required else ' '} {q.label[:70]}")

    letter = draft(job, profile, m)
    LETTERS_DIR.mkdir(exist_ok=True)
    slug = f"{job.company}-{job.title}".lower()
    slug = "".join(c if c.isalnum() else "-" for c in slug)[:60].strip("-")
    path = LETTERS_DIR / f"{slug}.txt"
    path.write_text(letter, encoding="utf-8")
    print(f"\n  draft cover letter written to {path}")
    print("  " + "-" * 68)
    for line in letter.splitlines()[:8]:
        print(f"  {line}")
    print("  ...")
    return 0


def cmd_open(args) -> int:
    job, _ = _pick(args)
    print(f"opening {job.company} — {job.title}")
    print(f"  {job.url}")
    webbrowser.open(job.url)
    print("\nwhen you have applied:  python apply.py log", args.n)
    return 0


def cmd_fill(args) -> int:
    from src.apply_forms import describe
    from src.autofill import fill

    job, profile = _pick(args)
    answers = load_answers(args.profile)
    resume = Path(args.resume)
    if not resume.exists():
        print(f"no resume at {resume}")
        return 1

    m = score(job, profile)
    letter = draft(job, profile, m)
    spec = describe(job.url)

    print(f"\n  {job.company} — {job.title}   ({m.score}% match)")
    print(f"  {spec.ats}, {len(spec.questions)} questions")
    print("\n  A browser will open and the form will be filled in front of you.")
    print("  Nothing is submitted: you check it and press submit yourself.")
    if input("  Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("  cancelled")
        return 0

    report = fill(job.url, profile, answers, str(resume), letter)
    print("\n  filled:")
    for f in report["filled"]:
        print(f"    {f}")
    if report["left_for_you"]:
        print("  left for you:")
        for f in report["left_for_you"][:12]:
            print(f"    {f}")
    if input("\n  Did you submit it? [y/N] ").strip().lower() in ("y", "yes"):
        _record(job, method="autofill")
        print("  logged")
    return 0


def _record(job: Job, method: str = "manual", notes: str = "") -> None:
    with _apps() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO applications
               (fingerprint, company, title, url, location, match_score,
                applied_at, method, status, notes)
               VALUES (?,?,?,?,?,?,?,?,'applied',?)""",
            (job.fingerprint, job.company, job.title, job.url, job.location,
             job.match_score, datetime.now(UTC).isoformat(timespec="seconds"),
             method, notes),
        )
        conn.commit()


def cmd_log(args) -> int:
    job, _ = _pick(args)
    _record(job, method="manual", notes=args.note or "")
    print(f"logged: {job.company} — {job.title}")
    return 0


def cmd_status(args) -> int:
    with _apps() as conn:
        rows = conn.execute(
            "SELECT * FROM applications ORDER BY applied_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    if not rows:
        print("no applications logged yet.")
        return 0
    print(f"  {'when':<12}{'fit':>4}  {'company':<22}{'title':<40}{'status':<12}how")
    for r in rows:
        print(f"  {r['applied_at'][:10]:<12}{(r['match_score'] or 0):>3}%  "
              f"{r['company'][:20]:<22}{r['title'][:38]:<40}"
              f"{r['status']:<12}{r['method'] or ''}")
    with _apps() as conn:
        total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    print(f"\n  {total} application(s) logged")
    return 0


def main(argv=None) -> int:
    # Shared flags live on a parent parser so they work on either side of the
    # subcommand. argparse otherwise rejects `apply.py list --limit 8`, which
    # is the order everyone types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="jobs.db")
    common.add_argument("--profile", default="profile.yaml")
    common.add_argument("--resume", default="resume.pdf")
    common.add_argument("--limit", type=int, default=25)
    common.add_argument("--min-score", type=int, default=55,
                        help="ignore matches below this (default 55)")
    common.add_argument("--anywhere", action="store_true",
                        help="include roles outside India and non-remote")

    ap = argparse.ArgumentParser(prog="apply.py", description=__doc__,
                                 parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", parents=[common]).set_defaults(fn=cmd_profile)
    sub.add_parser("list", parents=[common]).set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("open", cmd_open), ("fill", cmd_fill)):
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument("n", type=int)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("log", parents=[common])
    sp.add_argument("n", type=int)
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_log)
    sub.add_parser("status", parents=[common]).set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
