"""Source adapters.

Every adapter is a callable that takes a slug (or URL) and returns list[Job].
They all use public, unauthenticated endpoints. No API keys anywhere in here.

If you add a new ATS, write one function, register it in ADAPTERS, and add
companies to config/sources.yaml. Nothing else changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import random
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx
from selectolax.parser import HTMLParser

from .models import Job

log = logging.getLogger(__name__)

UA = "job-radar/1.0 (personal job alert bot; +https://github.com/Anikesh0001/job-radar)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# --- polite retry / rate limiting -----------------------------------------
#
# A single transient 503 used to cost us a whole company's board for that run.
# Retry on the status codes that mean "try again", never on a 404 (a dead slug
# is dead; retrying it three times just triples the load for the same answer).
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE = 1.5

# Two adapters can point at the same host (twenty Greenhouse slugs all hit
# boards-api.greenhouse.io). The thread pool would fire them simultaneously,
# so serialise per host with a minimum gap rather than trusting global jitter.
MIN_HOST_INTERVAL = 0.35
_host_lock = threading.Lock()
_host_last: dict[str, float] = {}


def _throttle(url: str) -> None:
    host = httpx.URL(url).host or ""
    with _host_lock:
        wait = MIN_HOST_INTERVAL - (time.monotonic() - _host_last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()


class Skipped(Exception):
    """Raised by an adapter that chose not to run — a missing API key, say.

    Distinct from returning []: "no key configured" and "this board is broken"
    both look like zero postings otherwise, and only one of them is a problem.
    """


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)


def _text(html: str, limit: int = 4000) -> str:
    """Strip HTML down to plain text for keyword filtering."""
    if not html:
        return ""
    clean = _TAGS.sub(" ", html)
    clean = (
        clean.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
    )
    return _WS.sub(" ", clean).strip()[:limit]


def _request(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """HTTP with backoff on transient failures. Raises on a final hard error."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle(url)
        try:
            r = client.request(method, url, **kw)
        except (httpx.TransportError, httpx.InvalidURL) as e:
            last_exc = e
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(BACKOFF_BASE ** attempt + random.uniform(0, 0.4))
            continue

        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
            # Honour Retry-After when the server bothers to send one, but cap
            # it — some hosts answer "3600" and we are not waiting an hour.
            delay = BACKOFF_BASE ** attempt + random.uniform(0, 0.4)
            ra = r.headers.get("Retry-After", "")
            if ra.strip().isdigit():
                delay = min(float(ra), 30.0)
            log.debug("%s -> HTTP %s, retrying in %.1fs", url, r.status_code, delay)
            time.sleep(delay)
            continue

        r.raise_for_status()
        return r

    if last_exc:
        raise last_exc
    raise RuntimeError(f"unreachable retry state for {url}")


def _get(client: httpx.Client, url: str, **kw):
    return _request(client, "GET", url, **kw)


def _post(client: httpx.Client, url: str, **kw):
    return _request(client, "POST", url, **kw)


# --------------------------------------------------------------------------
# ATS adapters
# --------------------------------------------------------------------------


def greenhouse(client: httpx.Client, slug: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    data = _get(client, url, params={"content": "true"}).json()
    out = []
    for j in data.get("jobs", []):
        out.append(
            Job(
                company=slug,
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                location=(j.get("location") or {}).get("name", ""),
                description=_text(j.get("content", "")),
                posted_at=j.get("updated_at"),
                source="greenhouse",
            )
        )
    return out


def lever(client: httpx.Client, slug: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    data = _get(client, url, params={"mode": "json"}).json()
    out = []
    for j in data:
        cat = j.get("categories") or {}
        out.append(
            Job(
                company=slug,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                location=cat.get("location", ""),
                description=_text(j.get("descriptionPlain") or j.get("description", "")),
                posted_at=j.get("createdAt"),
                source="lever",
            )
        )
    return out


def ashby(client: httpx.Client, slug: str) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _get(client, url, params={"includeCompensation": "true"}).json()
    out = []
    for j in data.get("jobs", []):
        out.append(
            Job(
                company=data.get("name") or slug,
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                location=j.get("location", ""),
                description=_text(j.get("descriptionHtml") or j.get("descriptionPlain", "")),
                posted_at=j.get("publishedAt"),
                source="ashby",
            )
        )
    return out


SMARTRECRUITERS_MAX = 1000  # per company, per run


def smartrecruiters(client: httpx.Client, slug: str) -> list[Job]:
    """Per-company board. Pages 100 at a time — the big employers here run to
    thousands of openings, and the un-paged version silently returned the
    first 100 of Bosch's 4,800."""
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    out = []
    for offset in range(0, SMARTRECRUITERS_MAX, 100):
        data = _get(client, url, params={"limit": 100, "offset": offset}).json()
        rows = data.get("content") or []
        if not rows:
            break
        for j in rows:
            loc = j.get("location") or {}
            city = ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            if loc.get("remote"):
                city = f"Remote{' - ' + city if city else ''}"
            out.append(
                Job(
                    company=slug,
                    title=j.get("name", ""),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                    location=city,
                    posted_at=j.get("releasedDate"),
                    source="smartrecruiters",
                )
            )
        if len(rows) < 100:
            break
    return out


def workable(client: httpx.Client, slug: str) -> list[Job]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    data = _get(client, url).json()
    out = []
    for j in data.get("jobs", []):
        city = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(
            Job(
                company=data.get("name") or slug,
                title=j.get("title", ""),
                url=j.get("url") or j.get("application_url", ""),
                location=city,
                description=_text(j.get("description", "")),
                posted_at=j.get("published_on"),
                source="workable",
            )
        )
    return out


def recruitee(client: httpx.Client, slug: str) -> list[Job]:
    url = f"https://{slug}.recruitee.com/api/offers/"
    data = _get(client, url).json()
    out = []
    for j in data.get("offers", []):
        out.append(
            Job(
                company=slug,
                title=j.get("title", ""),
                url=j.get("careers_url") or j.get("careers_apply_url", ""),
                location=j.get("location", ""),
                description=_text(j.get("description", "")),
                posted_at=j.get("published_at"),
                source="recruitee",
            )
        )
    return out


WORKDAY_MAX_JOBS = 500  # 25 pages of 20; Workday ignores a larger page size


def workday(client: httpx.Client, spec: str) -> list[Job]:
    """spec format: 'tenant|wdN|SiteName'  e.g. 'nvidia|wd5|NVIDIAExternalCareerSite'"""
    tenant, wd, site = spec.split("|")
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out = []
    # Workday hard-caps a page at 20. These are large employers, so walk far
    # enough to be useful and stop early when a page comes back empty.
    for offset in range(0, WORKDAY_MAX_JOBS, 20):
        r = _post(
            client,
            url,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        posts = r.json().get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append(
                Job(
                    company=tenant,
                    title=j.get("title", ""),
                    url=f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{path}",
                    location=j.get("locationsText", ""),
                    posted_at=j.get("postedOn"),
                    source="workday",
                )
            )
    return out


def bamboohr(client: httpx.Client, slug: str) -> list[Job]:
    """https://{slug}.bamboohr.com/careers/list

    A dead slug returns the marketing page with a 200, not a 404, so we check
    the content type rather than the status code.
    """
    url = f"https://{slug}.bamboohr.com/careers/list"
    r = _get(client, url)
    if "json" not in r.headers.get("content-type", ""):
        return []  # slug does not exist — BambooHR serves HTML with HTTP 200
    out = []
    for j in r.json().get("result") or []:
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            city = ", ".join(
                str(x) for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x
            )
        else:
            city = str(loc)
        if not city and j.get("isRemote"):
            city = "Remote"
        jid = j.get("id") or j.get("jobOpeningId") or ""
        out.append(
            Job(
                company=slug,
                title=j.get("jobOpeningName") or j.get("name") or j.get("title", ""),
                url=f"https://{slug}.bamboohr.com/careers/{jid}",
                location=city,
                description=_text(j.get("jobOpeningShareUrl") or ""),
                posted_at=j.get("datePosted") or j.get("postedDate"),
                source="bamboohr",
            )
        )
    return out


def breezy(client: httpx.Client, slug: str) -> list[Job]:
    """https://{slug}.breezy.hr/json"""
    data = _get(client, f"https://{slug}.breezy.hr/json").json()
    out = []
    for j in data:
        loc = j.get("location") or {}
        country = (loc.get("country") or {}) if isinstance(loc, dict) else {}
        state = (loc.get("state") or {}) if isinstance(loc, dict) else {}
        city = ", ".join(
            str(x)
            for x in [loc.get("city"), state.get("name"), country.get("name")]
            if x
        )
        if loc.get("is_remote"):
            city = f"Remote{' - ' + city if city else ''}"
        out.append(
            Job(
                company=slug,
                title=j.get("name", ""),
                url=j.get("url", ""),
                location=city,
                description=_text(j.get("description", "")),
                posted_at=j.get("published_date"),
                source="breezy",
            )
        )
    return out


def teamtailor(client: httpx.Client, slug: str) -> list[Job]:
    """https://{slug}.teamtailor.com/jobs.json — JSON Feed, not a bespoke API."""
    data = _get(client, f"https://{slug}.teamtailor.com/jobs.json").json()
    company = (data.get("title") or slug).replace(" jobs", "").strip()
    out = []
    for j in data.get("items", []):
        tags = j.get("tags") or []
        out.append(
            Job(
                company=company,
                title=j.get("title", ""),
                url=j.get("url", ""),
                location="; ".join(str(t) for t in tags[:2]),
                description=_text(j.get("content_html") or j.get("summary", "")),
                posted_at=j.get("date_published"),
                source="teamtailor",
            )
        )
    return out


def personio(client: httpx.Client, slug: str) -> list[Job]:
    """https://{slug}.jobs.personio.de/xml — Personio's <workzag-jobs> feed."""
    root = _parse_xml(_get(client, f"https://{slug}.jobs.personio.de/xml").text)
    out = []
    for pos in root.iter("position"):

        def val(tag: str) -> str:
            el = pos.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        offices = [val("office")] + [
            (o.text or "").strip()
            for o in pos.iterfind("additionalOffices/office")
            if o.text
        ]
        jid = val("id")
        desc = " ".join(t or "" for t in pos.itertext() if t)
        out.append(
            Job(
                company=val("subcompany") or slug,
                title=val("name"),
                url=f"https://{slug}.jobs.personio.de/job/{jid}",
                location="; ".join(o for o in offices if o),
                description=_text(desc),
                posted_at=val("createdAt") or None,
                source="personio",
            )
        )
    return out


def rippling(client: httpx.Client, slug: str) -> list[Job]:
    """https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"""
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    data = _get(client, url).json()
    out = []
    for j in data:
        loc = j.get("workLocation") or {}
        dept = (j.get("department") or {}).get("label", "")
        out.append(
            Job(
                company=slug,
                title=j.get("name", ""),
                url=j.get("url", ""),
                location=loc.get("label", "") if isinstance(loc, dict) else str(loc),
                description=_text(dept),
                source="rippling",
            )
        )
    return out


def oraclecloud(client: httpx.Client, spec: str) -> list[Job]:
    """Oracle Cloud Recruiting (ORC). spec: 'host|siteNumber', site defaults CX_1.

    Widely used by large Indian employers. Read the host straight out of the
    careers URL: https://ekjk.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/
    en/sites/CX_1/  ->  'ekjk.fa.em2.oraclecloud.com|CX_1'
    """
    host, _, site = spec.partition("|")
    site = site or "CX_1"
    out = []
    for offset in range(0, 600, 100):
        url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList"
            f"&finder=findReqs;siteNumber={site},limit=100,offset={offset}"
        )
        items = _get(client, url).json().get("items") or []
        reqs = items[0].get("requisitionList", []) if items else []
        if not reqs:
            break
        for j in reqs:
            jid = j.get("Id", "")
            out.append(
                Job(
                    company=host.split(".")[0],
                    title=j.get("Title", ""),
                    url=(
                        f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                        f"{site}/job/{jid}"
                    ),
                    location=j.get("PrimaryLocation", ""),
                    description=_text(j.get("ShortDescriptionStr", "")),
                    posted_at=j.get("PostedDate"),
                    source="oraclecloud",
                )
            )
    return out


# --------------------------------------------------------------------------
# Generic: schema.org JobPosting embedded in any careers page
# --------------------------------------------------------------------------


def _walk_jsonld(node, found: list):
    """JobPosting objects hide inside @graph arrays and nested lists."""
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types:
            found.append(node)
        for v in node.values():
            _walk_jsonld(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, found)


def _jobs_from_html(html: str, url: str) -> list[Job]:
    """Extract every schema.org JobPosting embedded in one HTML page."""
    tree = HTMLParser(html)
    blobs: list = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            _walk_jsonld(json.loads(raw), blobs)
        except json.JSONDecodeError:
            continue

    out = []
    for j in blobs:
        org = j.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else str(org)
        locs = j.get("jobLocation") or []
        locs = locs if isinstance(locs, list) else [locs]
        parts = []
        for loc in locs:
            addr = (loc or {}).get("address") or {}
            if isinstance(addr, dict):
                parts.append(
                    ", ".join(
                        x
                        for x in [addr.get("addressLocality"), addr.get("addressRegion")]
                        if x
                    )
                )
        out.append(
            Job(
                company=company or url,
                title=j.get("title", ""),
                url=j.get("url") or url,
                location="; ".join(p for p in parts if p),
                description=_text(j.get("description", "")),
                posted_at=j.get("datePosted"),
                source="jsonld",
            )
        )
    return out


def jsonld(client: httpx.Client, url: str) -> list[Job]:
    """Parse schema.org JobPosting markup from an arbitrary careers page.

    Google requires this markup for a posting to appear in Google Jobs, so a
    large share of career sites carry it — including ones on ATS platforms
    with no public API (Oracle Cloud, Darwinbox, Keka, Zoho Recruit).

    Caveat learned the hard way: most careers *listing* pages are JavaScript
    shells with no markup at all — the JobPosting sits on each individual job
    page. Point this at a page that server-renders its postings, and use the
    `sitemap` adapter when it doesn't.
    """
    return _jobs_from_html(_get(client, url).text, url)


# --------------------------------------------------------------------------
# Aggregator feeds (no slug needed)
# --------------------------------------------------------------------------


def remoteok(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://remoteok.com/api").json()
    out = []
    for j in data[1:]:  # first element is a legal notice, not a job
        out.append(
            Job(
                company=j.get("company", ""),
                title=j.get("position", ""),
                url=j.get("url", ""),
                location=j.get("location") or "Remote",
                description=_text(j.get("description", "")),
                posted_at=j.get("date"),
                source="remoteok",
            )
        )
    return out


def _epoch_to_iso(value) -> str | None:
    """Arbeitnow sends created_at as a Unix timestamp, not a date string."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return None


def arbeitnow(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://www.arbeitnow.com/api/job-board-api").json()
    out = []
    for j in data.get("data", []):
        loc = j.get("location", "")
        if j.get("remote") and "remote" not in loc.lower():
            loc = f"Remote{' - ' + loc if loc else ''}"
        out.append(
            Job(
                company=j.get("company_name", ""),
                title=j.get("title", ""),
                url=j.get("url", ""),
                location=loc,
                description=_text(j.get("description", "")),
                posted_at=_epoch_to_iso(j.get("created_at")),
                source="arbeitnow",
            )
        )
    return out


def hackernews(client: httpx.Client, _: str = "") -> list[Job]:
    """Latest 'Ask HN: Who is hiring?' comments via the public Algolia index."""
    search = _get(
        client,
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 1},
    ).json()
    hits = search.get("hits") or []
    if not hits:
        return []
    story_id = hits[0]["objectID"]
    thread = _get(client, f"https://hn.algolia.com/api/v1/items/{story_id}").json()
    out = []
    for c in (thread.get("children") or [])[:200]:
        body = _text(c.get("text") or "")
        if len(body) < 40:
            continue
        headline = body.split("|")[0][:120]
        out.append(
            Job(
                company=headline.strip(),
                title=headline.strip(),
                url=f"https://news.ycombinator.com/item?id={c['id']}",
                description=body,
                posted_at=c.get("created_at"),
                source="hackernews",
            )
        )
    return out


def remotive(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://remotive.com/api/remote-jobs", params={"limit": 200}).json()
    return [
        Job(
            company=(j.get("company_name") or "").strip(),
            title=j.get("title", ""),
            url=j.get("url", ""),
            location=j.get("candidate_required_location") or "Remote",
            description=_text(j.get("description", "")),
            posted_at=j.get("publication_date"),
            source="remotive",
        )
        for j in data.get("jobs", [])
    ]


def jobicy(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://jobicy.com/api/v2/remote-jobs", params={"count": 100}).json()
    return [
        Job(
            company=j.get("companyName", ""),
            title=j.get("jobTitle", ""),
            url=j.get("url", ""),
            location=j.get("jobGeo") or "Remote",
            description=_text(j.get("jobExcerpt") or j.get("jobDescription", "")),
            posted_at=j.get("pubDate"),
            source="jobicy",
        )
        for j in data.get("jobs", [])
    ]


def himalayas(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://himalayas.app/jobs/api", params={"limit": 200}).json()
    out = []
    for j in data.get("jobs", []):
        restrictions = j.get("locationRestrictions") or []
        out.append(
            Job(
                company=j.get("companyName", ""),
                title=j.get("title", ""),
                url=j.get("applicationLink") or j.get("guid", ""),
                location="; ".join(str(r) for r in restrictions) or "Remote",
                description=_text(j.get("description") or j.get("excerpt", "")),
                posted_at=str(j.get("pubDate") or ""),
                source="himalayas",
            )
        )
    return out


def workingnomads(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://www.workingnomads.com/api/exposed_jobs/").json()
    return [
        Job(
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            url=j.get("url", ""),
            location=j.get("location") or "Remote",
            description=_text(j.get("description", "")),
            posted_at=j.get("pub_date"),
            source="workingnomads",
        )
    for j in data
    ]


# The Muse tags every posting with a category; these are the IT-shaped ones.
MUSE_CATEGORIES = [
    "Software Engineering",
    "Data Science",
    "IT",
    "Data and Analytics",
    "Computer and IT",
]


def themuse(client: httpx.Client, spec: str = "") -> list[Job]:
    """The Muse public API. spec = number of pages to walk (default 3).

    Paginated and category-filtered, so it is the one aggregator here that
    returns a useful volume of non-remote India-based IT roles.
    """
    pages = int(spec) if spec.strip().isdigit() else 3
    out = []
    for page in range(1, pages + 1):
        params = [("page", page)] + [("category", c) for c in MUSE_CATEGORIES]
        r = client.get("https://www.themuse.com/api/public/jobs", params=params)
        if r.status_code != 200:
            break
        payload = r.json()
        for j in payload.get("results", []):
            locs = [(x or {}).get("name", "") for x in (j.get("locations") or [])]
            levels = [(x or {}).get("name", "") for x in (j.get("levels") or [])]
            out.append(
                Job(
                    company=(j.get("company") or {}).get("name", ""),
                    title=j.get("name", ""),
                    url=(j.get("refs") or {}).get("landing_page", ""),
                    location="; ".join(x for x in locs if x),
                    # Level is the useful signal here; the entry-level filter
                    # reads the description, so put it there.
                    description=_text(" ".join(levels) + " " + (j.get("contents") or "")),
                    posted_at=j.get("publication_date"),
                    source="themuse",
                )
            )
        if page >= payload.get("page_count", 1):
            break
    return out


def landingjobs(client: httpx.Client, _: str = "") -> list[Job]:
    data = _get(client, "https://landing.jobs/api/v1/jobs").json()
    out = []
    for j in data:
        # locations is a list of {"city": ..., "country_code": ...} dicts.
        parts = []
        for x in j.get("locations") or []:
            if isinstance(x, dict):
                parts.append(", ".join(str(v) for v in [x.get("city"), x.get("country_code")] if v))
            elif x:
                parts.append(str(x))
        loc = "; ".join(p for p in parts if p)
        if j.get("remote"):
            loc = f"Remote{' - ' + loc if loc else ''}"
        # The payload carries no company field at all; the URL holds the slug:
        # https://landing.jobs/at/<company>/<job-slug>
        url = j.get("url", "")
        seg = url.split("/at/", 1)[1].split("/")[0] if "/at/" in url else ""
        out.append(
            Job(
                company=seg.replace("-", " ").title() or "landing.jobs",
                title=j.get("title", ""),
                url=j.get("url", ""),
                location=loc,
                description=_text(
                    (j.get("role_description") or "") + " " + (j.get("main_requirements") or "")
                ),
                posted_at=j.get("published_at"),
                source="landingjobs",
            )
        )
    return out


# --------------------------------------------------------------------------
# Broad search endpoints — companies we hold no slug for
# --------------------------------------------------------------------------

# SmartRecruiters' public search only honours `keyword`, ignores paging, and
# hard-caps at 100 hits. So one call is one keyword; breadth comes from running
# many keywords. These are chosen to tile IT hiring with minimal overlap.
SR_KEYWORDS = [
    "software engineer", "backend developer", "frontend developer",
    "full stack developer", "data engineer", "data scientist",
    "machine learning", "devops", "cloud engineer", "site reliability",
    "qa engineer", "test automation", "mobile developer", "android developer",
    "ios developer", "python developer", "java developer", "golang",
    "react developer", "node developer", "security engineer", "platform engineer",
    "business analyst", "product manager", "ui ux designer", "database administrator",
    "network engineer", "system administrator", "technical support engineer",
    "solutions architect", "graduate software", "software intern",
]


def smartrecruiters_search(client: httpx.Client, spec: str = "") -> list[Job]:
    """SmartRecruiters' cross-company public search.

    Unlike the per-company `smartrecruiters` adapter this needs no slug, so it
    reaches employers we have never heard of. The catch, established by
    probing it: `offset`/`page`/`limit`/`country` are all silently ignored and
    every response is 100 rows, so the only lever is the keyword. Treat the
    yield as "a sample of what is live", not "everything".

    spec: optional comma-separated keyword override.
    """
    keywords = [k.strip() for k in spec.split(",") if k.strip()] or SR_KEYWORDS
    out: list[Job] = []
    for kw in keywords:
        try:
            data = _get(
                client,
                "https://jobs.smartrecruiters.com/sr-jobs/search",
                params={"keyword": kw},
            ).json()
        except Exception as e:  # noqa: BLE001 - one keyword must not kill the rest
            log.debug("sr-search %r: %s", kw, e)
            continue
        for j in data.get("content", []):
            comp = j.get("company") or {}
            loc = j.get("location") or {}
            city = j.get("shortLocation") or ", ".join(
                str(x) for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            if loc.get("remote"):
                city = f"Remote{' - ' + city if city else ''}"
            out.append(
                Job(
                    company=comp.get("name") or comp.get("identifier") or "",
                    title=j.get("name", ""),
                    url=j.get("applyUrl", ""),
                    location=city,
                    posted_at=j.get("releasedDate"),
                    source="smartrecruiters_search",
                )
            )
    return out


INSTAHYRE_PAGE = 35  # server-side hard cap; asking for more still returns 35
INSTAHYRE_MAX = 1400


def instahyre(client: httpx.Client, spec: str = "") -> list[Job]:
    """Instahyre — India-only, and almost entirely IT.

    The single highest-yield India source found: ~13k live postings, all
    software/data/infra roles, with real company names. Paging is by offset
    and deep offsets work, so the cap here is politeness, not the API.

    spec: max postings to pull (default 1400).
    """
    cap = int(spec) if spec.strip().isdigit() else INSTAHYRE_MAX
    out: list[Job] = []
    for offset in range(0, cap, INSTAHYRE_PAGE):
        data = _get(
            client,
            "https://www.instahyre.com/api/v1/job_search",
            params={"limit": INSTAHYRE_PAGE, "offset": offset},
        ).json()
        rows = data.get("objects") or []
        if not rows:
            break
        for j in rows:
            emp = j.get("employer") or {}
            out.append(
                Job(
                    company=emp.get("company_name", ""),
                    title=j.get("title", ""),
                    url=j.get("public_url", ""),
                    location=j.get("locations", ""),
                    description=_text(" ".join(j.get("keywords") or [])),
                    source="instahyre",
                )
            )
    return out


def phenom(client: httpx.Client, host: str) -> list[Job]:
    """Phenom People career sites: https://{host}/api/jobs

    Big-enterprise platform, but only some tenants leave the JSON endpoint
    open — most answer 200 with the HTML shell instead, so check the content
    type rather than the status code. spec is the careers hostname, e.g.
    'careers.pepsico.com'.
    """
    host = host.replace("https://", "").replace("http://", "").strip("/")
    out: list[Job] = []
    for offset in (0, 100, 200):
        r = _get(
            client,
            f"https://{host}/api/jobs",
            params={"limit": 100, "offset": offset, "recordsPerPage": 100},
        )
        if "json" not in r.headers.get("content-type", ""):
            log.debug("phenom %s served HTML, not the job API", host)
            break
        rows = r.json().get("jobs") or []
        if not rows:
            break
        for entry in rows:
            j = entry.get("data") or entry
            city = j.get("city") or ""
            loc = ", ".join(
                str(x) for x in [city, j.get("state"), j.get("country")] if x
            ) or j.get("location", "")
            out.append(
                Job(
                    company=j.get("company") or host.split(".")[-2],
                    title=j.get("title", ""),
                    url=j.get("applyUrl") or j.get("apply_url") or j.get("url", ""),
                    location=loc,
                    description=_text(j.get("description", "")),
                    posted_at=j.get("postedDate") or j.get("create_date"),
                    source="phenom",
                )
            )
    return out


def adzuna(client: httpx.Client, spec: str = "in") -> list[Job]:
    """Adzuna aggregator. Needs a free key: ADZUNA_APP_ID / ADZUNA_APP_KEY.

    Skipped silently when the keys are absent, so it costs nothing to leave
    enabled in the config. spec: country code, optionally '<cc>|<pages>'
    (e.g. 'in|5'). Adzuna indexes Naukri/Indeed-class inventory legitimately,
    which is how we reach that market without scraping either site.
    """
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise Skipped("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")

    country, _, pages = spec.partition("|")
    country = (country or "in").strip().lower()
    n_pages = int(pages) if pages.strip().isdigit() else 5

    out: list[Job] = []
    for page in range(1, n_pages + 1):
        data = _get(
            client,
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": 50,
                "category": "it-jobs",
                "content-type": "application/json",
            },
        ).json()
        rows = data.get("results") or []
        if not rows:
            break
        for j in rows:
            out.append(
                Job(
                    company=(j.get("company") or {}).get("display_name", ""),
                    title=j.get("title", ""),
                    url=j.get("redirect_url", ""),
                    location=(j.get("location") or {}).get("display_name", ""),
                    description=_text(j.get("description", "")),
                    posted_at=j.get("created"),
                    source="adzuna",
                )
            )
    return out


# --------------------------------------------------------------------------
# Scraped boards, via python-jobspy (optional dependency)
# --------------------------------------------------------------------------

# Which jobspy back-ends are actually worth calling, measured rather than
# assumed. Probed from a clean machine on 2026-09-08:
#
#   indeed         100 jobs in   3s   works, excellent India coverage
#   linkedin       100 jobs in  56s   works, but slow and rate-limits hard
#   naukri           0            HTTP 406 "recaptcha required"
#   glassdoor        0            HTTP 400, location not parsed
#   zip_recruiter    0            HTTP 403
#   bayt             0            HTTP 403
#   google           0            returns nothing
#
# So despite the name, this does NOT get you Naukri. Adzuna remains the only
# working route to that inventory.
JOBSPY_WORKING = {"indeed", "linkedin"}


def jobspy(client: httpx.Client, spec: str) -> list[Job]:
    """Scrape a job board through python-jobspy.

    spec: 'site|search term|location|count', e.g.
          'indeed|software engineer|India|200'
    Count and location are optional: 'indeed|data engineer' works.

    Unlike every other adapter here this is a SCRAPER, not a public API, with
    the consequences that implies: it breaks when a site changes its markup,
    the sites rate-limit it, and LinkedIn's terms prohibit it. It is opt-in for
    that reason — keep it as a supplement, never the backbone.

    Needs `pip install python-jobspy`, which pulls in pandas and numpy. It is
    deliberately not in requirements.txt so the core install stays small.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise Skipped("python-jobspy not installed (pip install python-jobspy)")

    parts = [p.strip() for p in spec.split("|")]
    site = (parts[0] if parts else "").lower()
    term = parts[1] if len(parts) > 1 and parts[1] else "software engineer"
    location = parts[2] if len(parts) > 2 and parts[2] else "India"
    count = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 100

    if site not in JOBSPY_WORKING:
        log.warning(
            "jobspy site %r returns nothing from a plain host — "
            "known working: %s", site, ", ".join(sorted(JOBSPY_WORKING))
        )
        return []

    def clean(value) -> str:
        # pandas hands back NaN for a missing cell, and str(NaN) is "nan".
        if value is None:
            return ""
        text = str(value).strip()
        return "" if text.lower() in ("nan", "nat", "none") else text

    df = scrape_jobs(
        site_name=[site],
        search_term=term,
        location=location,
        results_wanted=count,
        country_indeed="india" if "india" in location.lower() else None,
        description_format="markdown",
        verbose=0,
    )
    if df is None or not len(df):
        return []

    out: list[Job] = []
    for _, row in df.iterrows():
        loc = clean(row.get("location"))
        if clean(row.get("is_remote")).lower() == "true" and "remote" not in loc.lower():
            loc = f"Remote{' - ' + loc if loc else ''}"
        out.append(
            Job(
                company=clean(row.get("company")),
                title=clean(row.get("title")),
                url=clean(row.get("job_url")),
                location=loc,
                description=_text(clean(row.get("description"))),
                posted_at=clean(row.get("date_posted")) or None,
                source=f"jobspy-{site}",
            )
        )
    return out


# --------------------------------------------------------------------------
# Generic: any RSS/Atom job feed
# --------------------------------------------------------------------------

# RSS job titles are overwhelmingly "Job Title at Company Name".
_TITLE_AT = re.compile(r"^(?P<title>.+?)\s+(?:at|@|-)\s+(?P<company>[^-|]+)$")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# HTML entities like &rsquo; are undefined in XML and make a strict parser
# throw. Feeds in the wild are full of them.
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _parse_xml(text: str) -> ET.Element:
    text = text.strip()
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # Escape the bare ampersands and try once more before giving up.
        return ET.fromstring(_BARE_AMP.sub("&amp;", text))


def rss(client: httpx.Client, feed_url: str) -> list[Job]:
    """Parse any RSS 2.0 or Atom job feed.

    One adapter, unlimited sources: every job board that publishes a feed
    becomes a config line rather than new code. That is what makes the source
    list cheap to grow.
    """
    root = _parse_xml(_get(client, feed_url).text)
    host = httpx.URL(feed_url).host or feed_url

    entries = [e for e in root.iter() if _strip_ns(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        f: dict[str, str] = {}
        for child in e:
            name = _strip_ns(child.tag)
            if name == "link":
                # Atom puts the URL in an attribute, RSS in the element text.
                f["link"] = child.get("href") or (child.text or "").strip() or f.get("link", "")
            else:
                f.setdefault(name, (child.text or "").strip())

        raw_title = f.get("title", "")
        # Best case the feed has a real company element (Jobspresso does);
        # creator/author is the usual fallback and often carries HTML.
        company = _text(f.get("company") or f.get("creator") or f.get("author") or "", 80)
        title = raw_title

        if not company:
            m = _TITLE_AT.match(raw_title)
            if m:
                title, company = m.group("title").strip(), m.group("company").strip()
            elif ":" in raw_title:
                # We Work Remotely encodes it as "Company: Job Title". Only
                # trust the prefix when it is short enough to be a name, so
                # "Engineer: Backend" isn't torn in half.
                head, _, tail = raw_title.partition(":")
                if tail.strip() and len(head.split()) <= 5:
                    company, title = head.strip(), tail.strip()

        # Location: an explicit element first, else the geo fields WWR uses.
        location = f.get("location") or "; ".join(
            v for v in [f.get("region"), f.get("country"), f.get("state")] if v
        )

        out.append(
            Job(
                company=company or host,
                title=title,
                url=f.get("link", ""),
                location=location,
                description=_text(
                    f.get("description") or f.get("summary") or f.get("encoded")
                    or f.get("content", "")
                ),
                posted_at=f.get("pubDate") or f.get("published") or f.get("updated"),
                source="rss",
            )
        )
    return out


# --------------------------------------------------------------------------
# Generic: sitemap-driven JSON-LD crawl
# --------------------------------------------------------------------------

# Path fragments that mark a page as job-related. Deliberately loose: it must
# match "/remote-jobs/foo" and "/en/careers/bar" as well as "/job/123", and a
# false positive only costs one fetch — a page with no JobPosting yields
# nothing rather than junk.
_JOB_URL_HINT = re.compile(r"(job|career|vacanc|position|opening|opportunit|hiring)", re.I)
# ...and ones that look like a listing/index rather than one posting.
_JOB_URL_SKIP = re.compile(
    r"(/(page|category|tag|search|filter|department|location|team)s?/|\.(pdf|jpe?g|png|svg|css|js)$)",
    re.I,
)
SITEMAP_MAX_PAGES = 40  # job pages fetched per site, per run
SITEMAP_MAX_SITEMAPS = 8


def _collect_sitemap_urls(client: httpx.Client, url: str, depth: int = 0) -> list[str]:
    """Walk a sitemap, following <sitemapindex> one level down."""
    try:
        root = _parse_xml(_get(client, url).text)
    except Exception as e:  # noqa: BLE001
        log.debug("sitemap %s unreadable: %s", url, e)
        return []

    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if _strip_ns(el.tag) == "loc" and el.text
    ]
    if _strip_ns(root.tag) == "sitemapindex" and depth < 2:
        nested: list[str] = []
        # Prefer child sitemaps whose own name mentions jobs.
        ranked = sorted(locs, key=lambda u: 0 if _JOB_URL_HINT.search(u) else 1)
        for child in ranked[:SITEMAP_MAX_SITEMAPS]:
            nested.extend(_collect_sitemap_urls(client, child, depth + 1))
            if len(nested) > 2000:
                break
        return nested
    return locs


def sitemap(client: httpx.Client, spec: str) -> list[Job]:
    """Discover job pages via a site's sitemap, then read their JSON-LD.

    spec: 'https://example.com' or 'https://example.com/sitemap.xml',
    optionally '<url>|<max_pages>'.

    This is the honest version of "crawl the whole web": it needs no per-site
    code, but it is bounded, stays on one host, and only reads pages the site
    itself advertises in its sitemap. It is slower and lower-yield than an ATS
    API, so reach for it only when a company has no public board.
    """
    target, _, cap = spec.partition("|")
    max_pages = int(cap) if cap.strip().isdigit() else SITEMAP_MAX_PAGES

    base = target.rstrip("/")
    candidates = [base] if base.endswith(".xml") else [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/job-sitemap.xml",
    ]

    urls: list[str] = []
    for cand in candidates:
        urls = _collect_sitemap_urls(client, cand)
        if urls:
            break
    if not urls:
        log.warning("sitemap %s -> no sitemap found", target)
        return []

    host = httpx.URL(base if base.startswith("http") else f"https://{base}").host
    job_urls = [
        u
        for u in dict.fromkeys(urls)
        if _JOB_URL_HINT.search(httpx.URL(u).path or "")
        and not _JOB_URL_SKIP.search(u)
        and (httpx.URL(u).host or "").endswith(host or "")
    ]
    # Detail pages sit deeper than index pages and end in a slug, so ordering
    # by path depth puts the pages that actually carry a JobPosting first.
    job_urls.sort(key=lambda u: -(httpx.URL(u).path or "").count("/"))
    log.info(
        "sitemap %s -> %d job-ish URLs, fetching %d",
        host,
        len(job_urls),
        min(len(job_urls), max_pages),
    )

    out: list[Job] = []
    for u in job_urls[:max_pages]:
        try:
            out.extend(_jobs_from_html(_get(client, u).text, u))
        except Exception as e:  # noqa: BLE001 - one bad page must not stop the crawl
            log.debug("sitemap page %s: %s", u, e)
        time.sleep(random.uniform(0.3, 0.8))  # be a good guest on someone's site
    for j in out:
        j.source = "sitemap"
    return out


ADAPTERS: dict[str, Callable[[httpx.Client, str], list[Job]]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "recruitee": recruitee,
    "workday": workday,
    "bamboohr": bamboohr,
    "breezy": breezy,
    "teamtailor": teamtailor,
    "personio": personio,
    "rippling": rippling,
    "oraclecloud": oraclecloud,
    # generic — reach any site without new code
    "jsonld": jsonld,
    "rss": rss,
    "sitemap": sitemap,
    # broad search — no slug required, reaches unknown employers
    "smartrecruiters_search": smartrecruiters_search,
    "instahyre": instahyre,
    "phenom": phenom,
    "adzuna": adzuna,
    # scraper, opt-in, needs `pip install python-jobspy`
    "jobspy": jobspy,
    # aggregator feeds
    "remoteok": remoteok,
    "arbeitnow": arbeitnow,
    "hackernews": hackernews,
    "remotive": remotive,
    "jobicy": jobicy,
    "himalayas": himalayas,
    "workingnomads": workingnomads,
    "themuse": themuse,
    "landingjobs": landingjobs,
}


def fetch_one(platform: str, slug: str) -> list[Job]:
    """Fetch a single (platform, slug) pair. Never raises — logs and returns []."""
    adapter = ADAPTERS.get(platform)
    if adapter is None:
        log.warning("no adapter for platform %r", platform)
        return []
    try:
        with _client() as client:
            jobs = adapter(client, slug)
        log.info("%-16s %-28s %3d jobs", platform, slug[:28], len(jobs))
        return [j for j in jobs if j.title and j.url]
    except Skipped as e:
        log.info("%-16s %-28s skipped (%s)", platform, slug[:28], e)
        raise
    except httpx.HTTPStatusError as e:
        log.warning("%s/%s -> HTTP %s", platform, slug, e.response.status_code)
    except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
        log.warning("%s/%s -> %s: %s", platform, slug, type(e).__name__, e)
    return []


# Adapters that take no slug — one call fetches the whole feed. For everything
# else an empty list means "nothing configured yet", NOT "call it with an empty
# slug": doing that fired a doomed request at
# apply.workable.com/api/v1/widget/accounts/ on every run.
NO_SLUG = {
    "remoteok", "arbeitnow", "hackernews", "remotive", "jobicy", "himalayas",
    "workingnomads", "themuse", "landingjobs", "smartrecruiters_search",
    "instahyre", "adzuna",
}


def iter_targets(config: dict) -> Iterable[tuple[str, str]]:
    """Flatten config/sources.yaml into (platform, slug) pairs.

    Slugs are coerced to str because YAML parses a bare `- 1400` as an int,
    and every adapter treats its argument as text.
    """
    for platform, slugs in (config.get("sources") or {}).items():
        if platform not in ADAPTERS:
            log.warning("no adapter for platform %r — skipping", platform)
            continue
        if not slugs:
            if platform in NO_SLUG:
                yield platform, ""
            continue
        for slug in slugs:
            yield platform, str(slug)
