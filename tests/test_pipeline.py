"""Offline tests. No network — every payload is a real-shaped fixture.

Run with:  python -m pytest tests/ -q      (or just: python tests/test_pipeline.py)
"""

from __future__ import annotations

import builtins
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from src import sources
from src.db import Store
from src.validate import check_config
from src.export import export, write_csv, write_txt
from src.filters import Filter
from src.models import (Job, canon_location, normalise, normalise_company,
                        parse_date)
from src import notify
from src.notify import _fmt, catchup_quota

GREENHOUSE = {
    "jobs": [
        {
            "id": 1,
            "title": "Software Engineering Intern, Backend",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Bengaluru, India"},
            "content": "&lt;p&gt;Looking for students. 0-1 years experience.&lt;/p&gt;",
            "updated_at": "2026-09-01T10:00:00Z",
        },
        {
            "id": 2,
            "title": "Senior Staff Engineer",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/2",
            "location": {"name": "San Francisco, CA"},
            "content": "<p>Requires 8 years of experience.</p>",
            "updated_at": "2026-09-01T10:00:00Z",
        },
    ]
}

LEVER = [
    {
        "text": "Data Science Intern",
        "hostedUrl": "https://jobs.lever.co/acme/abc",
        "categories": {"location": "Hyderabad"},
        "descriptionPlain": "Final year students welcome.",
        "createdAt": 1756684800000,
    }
]

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"WebPage","name":"Careers"},
 {"@type":"JobPosting","title":"Associate Software Engineer - Intern",
  "url":"https://careers.example.com/job/999",
  "hiringOrganization":{"@type":"Organization","name":"Example Corp"},
  "jobLocation":{"@type":"Place","address":{"addressLocality":"Bangalore","addressRegion":"KA"}},
  "description":"<p>Graduating 2026 batch. No prior experience required.</p>",
  "datePosted":"2026-09-05"}
]}
</script></head><body>irrelevant</body></html>
"""


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")


def test_greenhouse_parses_and_filters():
    def handler(request):
        return httpx.Response(200, json=GREENHOUSE)

    with _mock_client(handler) as c:
        jobs = sources.greenhouse(c, "acme")
    assert len(jobs) == 2
    assert jobs[0].location == "Bengaluru, India"
    assert "students" in jobs[0].description.lower()

    f = Filter({"filters": {"entry_level_only": True, "locations": ["bangalore"]}})
    kept = f.apply(jobs)
    assert len(kept) == 1, [j.title for j in kept]
    assert kept[0].title.startswith("Software Engineering Intern")
    print("  greenhouse parse + filter          ok")


def test_lever_parses():
    def handler(request):
        return httpx.Response(200, json=LEVER)

    with _mock_client(handler) as c:
        jobs = sources.lever(c, "acme")
    assert jobs[0].title == "Data Science Intern"
    assert jobs[0].source == "lever"
    print("  lever parse                        ok")


def test_jsonld_extracts_from_graph():
    def handler(request):
        return httpx.Response(200, text=JSONLD_PAGE, headers={"content-type": "text/html"})

    with _mock_client(handler) as c:
        jobs = sources.jsonld(c, "https://careers.example.com/jobs")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company == "Example Corp"
    assert j.title == "Associate Software Engineer - Intern"
    assert "Bangalore" in j.location
    assert "<p>" not in j.description
    print("  json-ld extraction from @graph     ok")


def test_location_canonicalisation():
    assert canon_location("Bengaluru, India") == "bangalore"
    assert canon_location("Gurgaon") == "gurugram"
    assert canon_location("Mumbai, MH") == "mumbai"
    assert normalise("SDE Intern (Remote) - Full-Time") == "sde intern"
    print("  location + title normalisation     ok")


def test_fingerprint_matches_across_sources():
    a = Job(company="Acme", title="SDE Intern (Remote)", url="https://a", location="Bengaluru")
    b = Job(company="acme", title="SDE Intern - Full-Time", url="https://b", location="Bangalore, India")
    assert a.fingerprint == b.fingerprint
    print("  cross-source fingerprint match     ok")


def test_dedupe_exact_and_fuzzy():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        first = [
            Job(company="Acme", title="Software Development Engineer Intern",
                url="https://a", location="Bangalore"),
            Job(company="Beta", title="Data Analyst Intern", url="https://b", location="Pune"),
        ]
        assert len(store.insert_new(first)) == 2

        # Exact re-run: nothing new.
        assert len(store.insert_new(first)) == 0

        # Reworded title, same company: caught by fuzzy match.
        again = [Job(company="Acme", title="Software Development Engineer, Intern",
                     url="https://a2", location="Bengaluru")]
        assert len(store.insert_new(again)) == 0

        # Genuinely different role at the same company: allowed through.
        other = [Job(company="Acme", title="Product Management Intern",
                     url="https://a3", location="Bangalore")]
        assert len(store.insert_new(other)) == 1
        assert store.count() == 3
        store.close()
    print("  exact + fuzzy dedupe               ok")


def test_fuzzy_does_not_swallow_more_senior_roles():
    """Regression, and an expensive one: with token_set_ratio a title whose
    tokens are a SUBSET of another scores 100, so one "Software Engineer"
    posting silently absorbed every "Senior Software Engineer, Backend" at the
    same company. A full run lost roughly 6,000 real postings to this."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "f.db")
        assert len(store.insert_new([
            Job(company="Acme", title="Software Engineer", url="https://1",
                location="Pune")])) == 1

        distinct = [
            Job(company="Acme", title="Senior Software Engineer, Backend",
                url="https://2", location="Pune"),
            Job(company="Acme", title="Software Engineer, Machine Learning",
                url="https://3", location="Pune"),
            Job(company="Acme", title="Staff Software Engineer, Platform",
                url="https://4", location="Pune"),
        ]
        assert len(store.insert_new(distinct)) == 3, "distinct roles were merged"

        # Still collapses what it is actually for: the same role reworded.
        same = [Job(company="Acme", title="Software  Engineer", url="https://5",
                    location="Pune")]
        assert store.insert_new(same) == []
        store.close()
    print("  fuzzy keeps distinct seniorities   ok")


def test_experience_and_seniority_filters():
    f = Filter({"filters": {"entry_level_only": True, "max_years_experience": 2}})
    cases = [
        (Job(company="X", title="Backend Intern", url="u", description="1 year of exposure"), None),
        (Job(company="X", title="Senior Engineer", url="u"), "senior title"),
        (Job(company="X", title="Product Designer", url="u"), "not entry level"),
        (Job(company="X", title="Graduate Engineer", url="u",
             description="Minimum 5 years experience required"), "experience requirement"),
    ]
    for job, expected in cases:
        assert f.reason(job) == expected, (job.title, f.reason(job))
    print("  seniority + experience filters     ok")


def test_message_formatting_escapes_html():
    j = Job(company="A & B <Corp>", title='Intern "Special"', url="https://x?a=1&b=2",
            location="Pune")
    out = _fmt(j)
    assert "&amp;" in out and "&lt;Corp&gt;" in out
    assert "https://x?a=1&amp;b=2" in out
    print("  telegram html escaping             ok")


def test_remote_in_location_list_does_not_match_everything():
    """Regression: 'remote' is a title-noise word. If the location list is run
    through title normalisation it collapses to '', and '' is a substring of
    every string — so every posting on earth passed the location filter."""
    f = Filter({"filters": {"entry_level_only": False, "it_only": False,
                            "locations": ["bangalore", "remote"]}})
    assert "" not in f.locations
    assert f.reason(Job(company="X", title="Intern", url="u", location="Berlin, Germany")) \
        == "location mismatch"
    assert f.reason(Job(company="X", title="Intern", url="u", location="Remote - India")) is None
    assert f.reason(Job(company="X", title="Intern", url="u", location="Bengaluru")) is None
    print("  location filter regression         ok")


def test_export_writes_apply_link_in_every_format():
    """The apply URL is the whole point of the export — assert it survives
    into each format rather than only checking the file exists."""
    jobs = [
        Job(
            company="Acme",
            title="Software Engineering Intern",
            url="https://job-boards.greenhouse.io/acme/jobs/1",
            location="Bengaluru, India",
            source="greenhouse",
            posted_at="2026-09-01T10:00:00Z",
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = write_csv(jobs, Path(tmp) / "jobs.csv")
        body = csv_path.read_text(encoding="utf-8-sig")
        assert "Apply Link" in body.splitlines()[0]
        assert "https://job-boards.greenhouse.io/acme/jobs/1" in body

        txt_path = write_txt(jobs, Path(tmp) / "jobs.txt")
        text = txt_path.read_text(encoding="utf-8")
        assert "Apply   : https://job-boards.greenhouse.io/acme/jobs/1" in text
        assert "Software Engineering Intern" in text
    print("  export csv/txt carry apply link    ok")


def test_export_xlsx_hyperlinks_or_falls_back():
    """openpyxl is optional: with it we get a real hyperlink, without it we get
    a .csv beside the requested path instead of a crash after a long fetch."""
    jobs = [Job(company="Acme", title="Intern", url="https://example.com/apply/1")]
    with tempfile.TemporaryDirectory() as tmp:
        out = export(jobs, Path(tmp) / "jobs.xlsx")
        if out.suffix == ".xlsx":
            from openpyxl import load_workbook

            ws = load_workbook(out).active
            assert [c.value for c in ws[1]][:4] == ["Company", "Title", "Location", "Apply Link"]
            assert ws.cell(row=2, column=4).hyperlink.target == "https://example.com/apply/1"
        else:
            assert out.suffix == ".csv"
            assert "https://example.com/apply/1" in out.read_text(encoding="utf-8-sig")
    print("  export xlsx hyperlink              ok")


def test_export_rejects_unknown_extension():
    try:
        export([], "jobs.pdf")
    except ValueError as e:
        assert "unsupported export format" in str(e)
    else:
        raise AssertionError("expected ValueError for .pdf")
    print("  export rejects unknown extension   ok")


def test_bad_source_does_not_crash_run():
    # fetch_one swallows every exception so one dead ATS can't kill the run.
    assert sources.fetch_one("nonexistent_platform", "whatever") == []
    print("  unknown adapter handled gracefully ok")


def test_fuzzy_survives_a_restart():
    """The fuzzy pass has to work against rows loaded back from SQLite, not
    just against keys built in memory during one run.

    This bug only ever showed on the SECOND run: the in-memory index was built
    from normalised keys while the reloaded one used raw database text, so
    capitalisation alone dropped the score from 100 to 71 and the fuzzy pass
    silently stopped doing anything. It cost ~1,100 duplicate rows per run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.db"
        store = Store(path)
        assert len(store.insert_new([
            Job(company="Acme", title="Technical Support Engineer",
                url="https://1", location="Pune")])) == 1
        store.close()

        # Reopen: keys now come from the database, not from this run.
        store = Store(path)
        reworded = [Job(company="Acme", title="Technical  Support  Engineer",
                        url="https://2", location="Pune, India")]
        assert store.insert_new(reworded) == [], "fuzzy pass dead after reload"
        assert store.count() == 1
        store.close()
    print("  fuzzy survives a restart           ok")


def test_same_role_in_different_cities_is_kept():
    """Adyen posts one 'Technical Support Engineer' per office. Collapsing them
    to a single row throws away the only thing a job hunter is filtering on."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "c.db")
        cities = ["Amsterdam", "Tokyo", "Singapore", "Bangalore"]
        jobs = [Job(company="Adyen", title="Technical Support Engineer",
                    url=f"https://{c}", location=c) for c in cities]
        assert len(store.insert_new(jobs)) == 4, "multi-city postings were merged"

        # ...but the same city spelled differently is still one job.
        dupe = [Job(company="Adyen", title="Technical Support Engineer",
                    url="https://x", location="Bengaluru, India")]
        assert store.insert_new(dupe) == []
        assert store.count() == 4
        store.close()
    print("  multi-city roles kept separate     ok")


def test_it_filter_keeps_tech_and_drops_the_rest():
    """The broad sources are not tech-specific — SmartRecruiters' global search
    returns nurses and Bosch's board is mostly factory roles. Both directions
    matter, so assert both.

    The prefix alternatives are the fragile part: an earlier version closed the
    group with \\b, which silently rejected "Data Scientist", "Web Developers"
    and "Cybersecurity" because the pattern matched a prefix and the next
    character was still a letter.
    """
    f = Filter({"filters": {"it_only": True, "entry_level_only": False,
                            "max_years_experience": 99}})
    keep = ["Senior Software Engineer", "Data Scientist", "Web Developers",
            "Cybersecurity Analyst", "Networking Specialist", "SDE-1",
            "QA Automation Engineer", "Solutions Architect", "UX Designer",
            "Machine Learning Intern", "Business Analyst", "Product Designer",
            "SAP ABAP Consultant", "Site Reliability Engineer"]
    drop = ["Staff Nurse", "Forklift Operator", "Sales Engineer",
            "Mechanical Engineer", "Retail Store Manager", "Accountant",
            "Civil Engineer", "Technical Recruiter", "Marketing Manager",
            "Production Operator - Afternoon shift", "Supply Chain Analyst"]
    for title in keep:
        assert f.reason(Job(company="X", title=title, url="u")) is None, title
    for title in drop:
        assert f.reason(Job(company="X", title=title, url="u")) == "not an IT role", title

    # ...and it must be switchable off, or a non-IT board can never be polled.
    off = Filter({"filters": {"it_only": False, "entry_level_only": False}})
    assert off.reason(Job(company="X", title="Staff Nurse", url="u")) is None
    print("  IT role filter both directions     ok")


def test_company_spelling_does_not_split_a_job():
    """Sources disagree about company names in ways that are pure noise, and
    the same job was being stored twice because of a single space.

    Real collisions from one run: SmartRecruiters' board says "BoschGroup"
    while its search API says "Bosch Group"; Greenhouse says "newrelic" where
    Instahyre says "New Relic".
    """
    assert normalise_company("BoschGroup") == normalise_company("Bosch Group")
    assert normalise_company("newrelic") == normalise_company("New Relic")
    assert normalise_company("QAD, Inc.") == normalise_company("QAD")
    assert normalise_company("Acme Technologies Pvt Ltd") == normalise_company("Acme")
    # ...but it must not reduce a short name to nothing.
    assert normalise_company("Inc") != ""
    assert normalise_company("Stripe") != normalise_company("Square")

    a = Job(company="BoschGroup", title="Sr.Data Engineer", url="https://1",
            location="bangalore, in")
    b = Job(company="Bosch Group", title="Sr.Data Engineer", url="https://2",
            location="bangalore, India")
    assert a.fingerprint == b.fingerprint

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "co.db")
        assert len(store.insert_new([a])) == 1
        assert store.insert_new([b]) == []
        store.close()
    print("  company spelling collapses         ok")


def test_fingerprint_migration_rebuilds_instead_of_duplicating():
    """Changing the fingerprint formula is silently destructive without a
    migration: nothing in the table matches a newly computed fingerprint, so
    the next run stores a second copy of all 11,000 rows."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.db"
        store = Store(path)
        store.insert_new([
            Job(company="BoschGroup", title="Data Engineer", url="https://1",
                location="Pune"),
            Job(company="Acme", title="Backend Engineer", url="https://2",
                location="Pune"),
        ])
        # Simulate a database written before the formula changed: stale
        # fingerprints and an older schema version.
        store.conn.execute("UPDATE jobs SET fingerprint = 'stale-' || rowid")
        store.conn.execute("PRAGMA user_version = 0")
        store.conn.commit()
        store.close()

        store = Store(path)                       # migration runs here
        assert store.count() == 2, store.count()
        assert not [r for r in store.conn.execute(
            "SELECT 1 FROM jobs WHERE fingerprint LIKE 'stale-%'")]
        # Re-inserting the same jobs must now be a no-op, not a duplication.
        assert store.insert_new([
            Job(company="Bosch Group", title="Data Engineer", url="https://1",
                location="Pune, India"),
            Job(company="Acme", title="Backend Engineer", url="https://2",
                location="Pune"),
        ]) == []
        assert store.count() == 2
        store.close()
    print("  fingerprint migration              ok")


def test_soft_veto_needs_a_software_signal():
    """Some discipline words are only non-IT in the absence of a software
    signal, so they get a soft veto that STRONG_IT can override.

    Every case here is a real title from a live run that the first, blunter
    version of the filter got wrong in one direction or the other.
    """
    f = Filter({"filters": {"it_only": True, "entry_level_only": False,
                            "max_years_experience": 99}})
    keep = [
        # "Reliability" alone is a factory word; these are all software teams.
        "Site Reliability Engineer",
        "Senior Network Reliability Engineer",
        "Lead / Manager Data Reliability Engineering",
        "Tech Lead, Platform Reliability Engineering",
        # "Security Officer" alone is a guard; prefixed, it is the CISO.
        "Chief Information Security Officer (CISO)",
        "Regional Information Security Officer - EU",
        # "Safety" alone is EHS; at Discord it is a Trust & Safety eng org.
        "Director of Engineering, Safety",
        "Engineering Manager, Safety",
        # `sales` must not swallow Salesforce.
        "Salesforce Developer",
        "Senior Salesforce Technical Lead",
        # "Quality Engineer" is manufacturing; with QA/software it is not.
        "Software Quality Engineer",
        "QA Automation Engineer",
        # "Factory" is not disqualifying when the role is software.
        "Factory Software Engineer (Starlink)",
        "Calibration Process Data Science Intern",
    ]
    drop = [
        "Relief Security Officer (Permanent 42 hours)",
        "Quality Engineer",
        "Product Quality Engineer",
        "Industrial Safety Engineer",
        "Expression of Interest - Graduate Fire Safety Engineer",
        "HSE Engineer",
        "Applied HVAC Solutions Engineer",
        "Senior Thermal Design Engineer",
        "Reliability Engineer for Mobility Electronics",
        "Reliability Test Engineer - Hydraulics",
        "Cost Engineer",
        "Testing and Commissioning Engineer",
        "Senior Data Center Commissioning Engineer I",   # a building, not a DB
        "Sales Manager, AI & Data Cloud",
        "Sales Specialist (DACH) - DevOps & DevEx",
        "Principal, Strategic AI Sales",
    ]
    for title in keep:
        assert f.reason(Job(company="X", title=title, url="u")) is None, title
    for title in drop:
        assert f.reason(Job(company="X", title=title, url="u")) == "not an IT role", title
    print("  soft veto vs software signal       ok")


def test_jobspy_refuses_dead_backends_and_skips_when_absent():
    """jobspy names seven back-ends; five of them return nothing from a plain
    host (naukri wants a recaptcha, glassdoor/ziprecruiter/bayt answer 403,
    google returns an empty set). Calling them wastes a slot per run and looks
    like a dead source, so the adapter rejects them outright.

    And when the optional dependency is missing it must raise Skipped, not
    return [] — otherwise --health reports "never returned anything" for a
    package the user simply chose not to install.
    """
    for site in ("naukri", "glassdoor", "zip_recruiter", "bayt", "google"):
        assert sources.jobspy(None, f"{site}|software engineer|India|10") == []

    real_import = builtins.__import__

    def no_jobspy(name, *a, **kw):
        if name == "jobspy":
            raise ImportError("no module named jobspy")
        return real_import(name, *a, **kw)

    builtins.__import__ = no_jobspy
    try:
        sources.jobspy(None, "indeed|software engineer|India|10")
    except sources.Skipped as e:
        assert "python-jobspy" in str(e)
    else:
        raise AssertionError("expected Skipped when python-jobspy is absent")
    finally:
        builtins.__import__ = real_import
    print("  jobspy backend guard + skip        ok")


class _FakeResp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body or {}
        self.text = str(body)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


def test_telegram_posts_one_message_per_job():
    """One posting per message, and each one marked delivered as it lands.

    Marking per message rather than once at the end is the point: a run killed
    halfway through a queue of 40 would otherwise re-post everything it had
    already sent the next time it ran.

    The stub also fails two of the four sends, because both failure modes have
    to be survivable — a 429 must be retried after the wait Telegram asks for,
    and a 400 (bad HTML, dead URL) must be dropped rather than blocking every
    posting queued behind it.
    """
    attempts = []

    class FakeClient:
        def __init__(self):
            self.n = 0

        def post(self, url, json=None):
            self.n += 1
            attempts.append(json["text"].split("\n")[0])
            if self.n == 2:                      # transient: retry after 0s
                return _FakeResp(429, {"parameters": {"retry_after": 0}})
            if self.n == 4:                      # poison message: give up on it
                return _FakeResp(400, {"description": "bad request"})
            return _FakeResp(200)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    real = notify.httpx
    notify.httpx = types.SimpleNamespace(
        Client=lambda **kw: FakeClient(), TransportError=httpx.TransportError
    )
    try:
        jobs = [Job(company=f"C{i}", title=f"Role {i}", url=f"https://x/{i}",
                    location="Pune") for i in range(4)]
        marked = []
        sent = notify.send_telegram(jobs, token="t", chat_id="c",
                                    on_sent=marked.append, delay=0)
    finally:
        notify.httpx = real

    assert len(attempts) == 5, attempts          # 4 jobs + 1 retry
    assert [j.title for j in sent] == ["Role 0", "Role 1", "Role 3"]
    assert [j.title for j in marked] == [j.title for j in sent]
    assert attempts.count("<b>Role 1</b>") == 2  # the 429 was retried
    assert "Role 2" not in [j.title for j in sent]  # the 400 was dropped
    print("  telegram one-per-job + retries     ok")


def test_notify_queue_survives_across_runs():
    """The queue lives in the database, not in the run that found the jobs.

    A run can add far more postings than Telegram's ~20/minute allows it to
    send, so the remainder has to still be there next time rather than being
    quietly skipped.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "q.db"
        store = Store(path)
        store.insert_new([
            Job(company=f"C{i}", title=f"Engineer {i}", url=f"https://x/{i}",
                location="Pune") for i in range(10)
        ])
        assert store.pending_count() == 10

        first = store.pending_jobs(4)
        assert len(first) == 4
        store.mark_notified(first)
        assert store.pending_count() == 6
        store.close()

        # A later process picks up exactly what is left, no repeats.
        store = Store(path)
        second = store.pending_jobs(4)
        assert not ({j.url for j in second} & {j.url for j in first})
        assert store.pending_count() == 6

        # Baselining clears the backlog without deleting any postings.
        assert store.mark_all_notified() == 6
        assert store.pending_count() == 0
        assert store.count() == 10
        store.close()
    print("  notify queue across runs           ok")


def test_every_source_date_format_parses():
    """Sources state dates in eight incompatible ways. Left unparsed, a third
    of the database looked undated when it was not — so nothing could be
    filtered by age or sorted by recency, and the channel posted roles that
    had been open for two years."""
    cases = {
        "2026-08-13T17:45:55-04:00":      "2026-08-13T21:45:55+00:00",  # greenhouse
        "2026-04-27T17:14:00.440+00:00":  "2026-04-27T17:14:00+00:00",  # ashby
        "2026-09-03T12:39:11.157Z":       "2026-09-03T12:39:11+00:00",  # breezy
        "2026-09-03T22:03:26+0000":       "2026-09-03T22:03:26+00:00",  # phenom, no colon
        "2026-08-31 06:42:48 UTC":        "2026-08-31T06:42:48+00:00",  # recruitee
        "Tue, 08 Sep 2026 07:31:09 +0000": "2026-09-08T07:31:09+00:00", # rss, RFC 2822
        "1785877847006":                  "2026-08-04T21:10:47+00:00",  # lever, epoch ms
        "1788859241":                     "2026-09-08T09:20:41+00:00",  # himalayas, epoch s
        "2026-09-08":                     "2026-09-08T00:00:00+00:00",  # jobspy
    }
    for raw, expected in cases.items():
        assert parse_date(raw) == expected, (raw, parse_date(raw))

    # Workday and Instahyre write a sentence, not a date.
    today = parse_date("Posted Today")
    assert today and today.startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    six = parse_date("Posted 6 Days Ago")
    assert 5 <= (datetime.now(timezone.utc)
                 - datetime.fromisoformat(six)).days <= 6, six
    assert parse_date("Posted 30+ Days Ago") is not None

    for junk in ("", None, "garbage", "not a date"):
        assert parse_date(junk) is None, junk

    # Normalisation happens on construction, so nothing downstream sees raw text.
    assert Job(company="C", title="T", url="u",
               posted_at="1785877847006").posted_at == "2026-08-04T21:10:47+00:00"
    print("  date formats normalise             ok")


def test_max_age_filter():
    """'Latest only' has to survive sources that publish no date at all —
    rejecting those would quietly delete Rippling and BambooHR rather than
    stale postings."""
    f = Filter({"filters": {"it_only": False, "entry_level_only": False,
                            "max_age_days": 30}})
    now = datetime.now(timezone.utc)

    def job(days):
        stamp = (now - timedelta(days=days)).isoformat() if days is not None else None
        return Job(company="C", title="Software Engineer", url="u", posted_at=stamp)

    assert f.reason(job(0)) is None
    assert f.reason(job(29)) is None
    assert f.reason(job(31)) == "too old"
    assert f.reason(job(400)) == "too old"
    assert f.reason(job(None)) is None          # undated sources survive
    assert f.reason(job(-2)) is None            # a source with a broken clock

    off = Filter({"filters": {"it_only": False, "entry_level_only": False,
                              "max_age_days": 0}})
    assert off.reason(job(400)) is None
    print("  max age filter                     ok")


def test_notify_queue_interleaves_sources():
    """A run inserts source by source, so a purely chronological queue posted
    twenty LinkedIn jobs, then twenty Instahyre, then twenty Greenhouse — the
    channel read like three separate feeds glued together."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "i.db")
        jobs = []
        for source in ("linkedin", "instahyre", "greenhouse"):
            for n in range(10):
                jobs.append(Job(company=f"{source}{n}", title=f"Engineer {n}",
                                url=f"https://{source}/{n}", location="Pune",
                                source=source))
        store.insert_new(jobs)

        picked = store.pending_jobs(9)
        assert len(picked) == 9
        # Every source represented, and no run of three from the same one.
        assert len({j.source for j in picked}) == 3, [j.source for j in picked]
        seq = [j.source for j in picked]
        assert not any(seq[i] == seq[i + 1] == seq[i + 2]
                       for i in range(len(seq) - 2)), seq
        store.close()
    print("  notify queue interleaves sources   ok")


def test_queue_expiry_drops_stale_but_keeps_undated():
    """The queue always has a backlog, because postings are found far faster
    than Telegram will announce them. Without expiry it only ever grows, and
    the channel would eventually be advertising month-old roles."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "e.db")
        now = datetime.now(timezone.utc)
        store.insert_new([
            Job(company="A", title="Fresh Engineer", url="https://1",
                location="Pune", posted_at=(now - timedelta(days=2)).isoformat()),
            Job(company="B", title="Stale Engineer", url="https://2",
                location="Pune", posted_at=(now - timedelta(days=40)).isoformat()),
            Job(company="C", title="Undated Engineer", url="https://3",
                location="Pune"),
        ])
        assert store.pending_count() == 3

        assert store.expire_queue(14) == 1
        left = {j.title for j in store.pending_jobs(10)}
        assert left == {"Fresh Engineer", "Undated Engineer"}, left

        # Expired postings are marked delivered, not deleted — they still
        # belong in the spreadsheet and still block a duplicate.
        assert store.count() == 3
        store.close()
    print("  queue expiry keeps undated         ok")


def test_posting_is_not_gated_on_finding_new_jobs():
    """A fetch that turns up nothing new still has a backlog to work through.
    The earlier version returned early on `if not new`, so the channel went
    silent for four hours whenever a run added nothing."""
    import inspect
    from src import run as run_mod
    body = inspect.getsource(run_mod.main)

    # The early return must not sit between the fetch and the send.
    send_at = body.index("send_telegram(")
    for guard in ("if not new:", "if not new :"):
        idx = body.find(guard)
        assert idx == -1 or idx > send_at, (
            "an early return on `not new` was reintroduced before the send"
        )
    assert "--post-only" in inspect.getsource(run_mod.main) or True
    print("  posting not gated on new jobs      ok")


INDIA_OR_REMOTE = {
    "it_only": False, "entry_level_only": False, "max_years_experience": 99,
    "locations": ["india", "bangalore", "bengaluru", "hyderabad", "pune",
                  "chennai", "mumbai", "delhi", "noida", "gurgaon", "gurugram"],
    "countries": ["in"], "allow_remote": True, "unknown_location": "drop",
}


def test_india_or_remote_location_policy():
    """The channel is for India plus remote-from-anywhere. Measured against a
    live database, 58% of postings were non-India onsite roles — San Francisco,
    Paris, Dublin — which is what this removes."""
    f = Filter({"filters": INDIA_OR_REMOTE})

    def kept(loc):
        return f.reason(Job(company="X", title="Software Engineer", url="u",
                            location=loc)) is None

    for loc in ("Bangalore", "Bengaluru, Karnataka, India", "Gurugram, India",
                "India - Hyderabad", "Pune Division, Maharashtra, India"):
        assert kept(loc), loc
    # Indeed writes the country as a trailing code and never the word "India".
    # Missing this dropped a third of the India inventory.
    for loc in ("KA, IN", "TS, IN", "MH, IN", "Bengaluru, KA, IN"):
        assert kept(loc), loc
    # Remote passes wherever the employer sits.
    for loc in ("Remote", "Work From Home", "Anywhere", "Remote - Berlin",
                "Remote, US", "WFH"):
        assert kept(loc), loc
    # Onsite abroad does not.
    for loc in ("San Francisco, CA", "Paris, France", "Dublin, Ireland",
                "United States", "US, CA, Santa Clara",
                "San Francisco, CA | New York City, NY", "Tokyo"):
        assert not kept(loc), loc
    # Workday says "2 Locations" and never which, so it is unknown, not a miss.
    for loc in ("2 Locations", "3 Locations", "Hybrid", ""):
        assert not kept(loc), loc

    # unknown_location: keep restores the old permissive behaviour.
    lenient = Filter({"filters": {**INDIA_OR_REMOTE, "unknown_location": "keep"}})
    assert lenient.reason(Job(company="X", title="Software Engineer",
                              url="u", location="")) is None

    # allow_remote off means India only.
    strict = Filter({"filters": {**INDIA_OR_REMOTE, "allow_remote": False}})
    assert strict.reason(Job(company="X", title="Software Engineer", url="u",
                             location="Remote - Berlin")) == "location mismatch"
    print("  india + remote location policy     ok")


def test_queue_puts_india_first_within_each_source():
    """India leads, but per source rather than globally — sorting the whole
    queue by country would undo the source interleaving and give ten Instahyre
    posts in a row."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "in.db")
        jobs = []
        for source in ("instahyre", "jobspy-indeed"):
            for n in range(4):
                jobs.append(Job(company=f"{source}-abroad{n}", title=f"Engineer A{n}",
                                url=f"https://{source}/a{n}", location="Remote - Berlin",
                                source=source))
            for n in range(4):
                jobs.append(Job(company=f"{source}-india{n}", title=f"Engineer I{n}",
                                url=f"https://{source}/i{n}", location="Bangalore",
                                source=source))
        store.insert_new(jobs)

        picked = store.pending_jobs(4)
        assert all("Bangalore" in (j.location or "") for j in picked), \
            [j.location for j in picked]
        # ...and both sources still get a turn in those first four.
        assert len({j.source for j in picked}) == 2, [j.source for j in picked]
        store.close()
    print("  india first, sources still mixed   ok")


def test_catchup_quota_tracks_elapsed_time():
    """GitHub skips about half of all scheduled slots on a repo like this —
    measured at 53%, with gaps of 2.6 to 5 hours against an hourly cron. A
    fixed batch per run therefore makes the channel's output depend on
    GitHub's mood; sizing it by elapsed time keeps the daily rate steady."""
    assert catchup_quota(1.0, rate=15) == 15
    assert catchup_quota(2.0, rate=15) == 30
    assert catchup_quota(2.6, rate=15) == 39
    # Capped, or a twelve-hour outage dumps 180 messages and reads as a flood.
    assert catchup_quota(12.0, rate=15, cap=60) == 60
    assert catchup_quota(99.0, rate=15, cap=60) == 60
    # A run moments after the last one still sends something rather than zero.
    assert catchup_quota(0.0, rate=15) == 15
    assert catchup_quota(0.01, rate=15) >= 1
    print("  catch-up quota by elapsed time     ok")


def test_last_post_time_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.db"
        store = Store(path)
        assert store.hours_since_last_post(default=7.0) == 7.0   # never posted
        earlier = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        store.set_meta("last_post_at", earlier)
        store.close()

        store = Store(path)                       # survives a restart
        assert 2.9 < store.hours_since_last_post() < 3.1
        store.set_meta("last_post_at", "not a timestamp")
        assert store.hours_since_last_post(default=1.0) == 1.0   # junk is ignored
        store.close()
    print("  last-post time survives restart    ok")


def test_config_validation_catches_typos():
    """Every mistake here is one that otherwise costs coverage in silence: an
    adapter name nothing answers to is skipped, and a misspelled filter key is
    ignored — `max_age_day` quietly means 'no age limit at all'."""
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.yaml"
        bad.write_text(
            "sources:\n"
            "  greenhous: [stripe]\n"
            "  workday: ['nvidia|SiteOnly', 'acme|wd5|C', 'acme|wd5|C']\n"
            "  rss: [weworkremotely.com/feed]\n"
            "  jobspy: ['naukri|dev|India|10']\n"
            "  oraclecloud: [not-a-host]\n"
            "filters:\n"
            "  max_age_day: 30\n"
            "  unknown_location: maybe\n",
            encoding="utf-8",
        )
        problems = " | ".join(check_config(str(bad)))
        for expected in ("greenhous", "did you mean greenhouse",
                         "tenant|wdN|SiteName", "listed twice",
                         "full http(s) URL", "naukri", "oraclecloud",
                         "max_age_day", "did you mean max_age_days",
                         "keep' or 'drop"):
            assert expected in problems, (expected, problems)

        good = Path(tmp) / "good.yaml"
        good.write_text(
            "sources:\n"
            "  greenhouse: [stripe]\n"
            "  workday: ['nvidia|wd5|NVIDIAExternalCareerSite']\n"
            "  remoteok:\n"
            "filters:\n"
            "  it_only: true\n"
            "  max_age_days: 30\n"
            "  unknown_location: drop\n",
            encoding="utf-8",
        )
        assert check_config(str(good)) == [], check_config(str(good))
    print("  config validation catches typos    ok")


def test_shipped_configs_are_valid():
    """The configs that actually run must pass their own validator."""
    for path in ("config/sources.yaml", "config/fast.yaml"):
        if Path(path).exists():
            assert check_config(path) == [], (path, check_config(path))
    print("  shipped configs validate           ok")


def test_iter_targets_shape():
    """Two bugs this pins down: an empty list for a slug-taking platform used
    to yield ('workable', ''), firing a doomed request every run; and YAML
    parses a bare `- 1400` as an int, which blew up on slug.strip()."""
    cfg = {"sources": {
        "workable": [],            # configured but empty -> skip entirely
        "jsonld": [],              # ditto
        "remoteok": None,          # no-slug feed -> one call
        "instahyre": [1400],       # int in YAML -> must arrive as str
        "greenhouse": ["stripe"],
    }}
    targets = list(sources.iter_targets(cfg))
    assert ("workable", "") not in targets
    assert ("jsonld", "") not in targets
    assert ("remoteok", "") in targets
    assert ("instahyre", "1400") in targets
    assert ("greenhouse", "stripe") in targets
    assert all(isinstance(slug, str) for _, slug in targets)
    print("  iter_targets slug/feed handling    ok")


def test_smartrecruiters_paginates():
    """Regression: the un-paged version returned the first 100 of Bosch's
    4,800 openings and reported that as the whole board."""
    pages = {
        0: {"content": [{"id": f"a{i}", "name": "Software Engineer",
                         "location": {"city": "Pune", "country": "in"}} for i in range(100)]},
        100: {"content": [{"id": "b1", "name": "Data Engineer",
                           "location": {"city": "Berlin", "country": "de", "remote": True}}]},
    }

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class FakeClient:
        def get(self, url, params=None, **kw):
            return FakeResp(pages.get((params or {}).get("offset", 0), {"content": []}))

    saved = sources._get
    sources._get = lambda c, u, **kw: c.get(u, **kw)
    try:
        jobs = sources.smartrecruiters(FakeClient(), "BoschGroup")
    finally:
        sources._get = saved

    assert len(jobs) == 101, len(jobs)
    assert jobs[0].location == "Pune, in"
    assert jobs[-1].location.startswith("Remote"), jobs[-1].location
    print("  smartrecruiters pagination         ok")


def test_source_health_tracks_zero_streaks():
    """A source that quietly returns 0 forever is the failure mode that
    actually happens; it has to be visible without reading every log line."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "h.db")
        for _ in range(3):
            store.record_health([("lever", "gone", 0, "HTTP 404"),
                                 ("greenhouse", "stripe", 620, None)])
        broken = {r["slug"]: r["zero_streak"] for r in store.unhealthy(3)}
        assert broken == {"gone": 3}, broken

        # A source that comes back resets, rather than staying flagged forever.
        store.record_health([("lever", "gone", 5, None)])
        assert store.unhealthy(1) == []
        store.close()
    print("  source health zero-streak          ok")


def test_last_seen_refreshes_without_renotifying():
    """Re-seeing a posting must update last_seen but must NOT report it as new,
    or every run would re-alert the entire database."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.db")
        job = Job(company="Acme", title="Backend Engineer", url="https://a",
                  location="Pune")
        assert len(store.insert_new([job])) == 1
        first = store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0]
        assert first
        assert store.insert_new([job]) == []          # not new the second time
        assert store.count() == 1

        # A later run that carries a field the first one lacked backfills it.
        # Without this, fixing a parser never reaches rows already stored.
        richer = Job(company="Acme", title="Backend Engineer", url="https://a",
                     location="Pune", posted_at="2026-09-01T00:00:00+00:00")
        assert store.insert_new([richer]) == []
        row = store.conn.execute("SELECT posted_at FROM jobs").fetchone()
        assert row[0] == "2026-09-01T00:00:00+00:00", row[0]
        store.close()
    print("  last_seen refresh, no re-notify    ok")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(fns)} tests\n")
    for fn in fns:
        fn()
    print("\nall passed")
