"""Static checks on a config file, so a typo fails loudly instead of quietly.

Every mistake this catches is one that otherwise costs you coverage without
saying anything: a platform name that no adapter answers to is skipped, a
Workday slug missing its site name 404s once a run for ever, and a misspelled
filter key is simply ignored — `max_age_day` silently means "no age limit at
all", and you find out weeks later from the channel.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import yaml

from .sources import ADAPTERS, NO_SLUG

# Filter keys src/filters.py actually reads. Anything else is a typo.
KNOWN_FILTERS = {
    "it_only", "entry_level_only", "max_years_experience", "max_age_days",
    "locations", "countries", "allow_remote", "unknown_location",
    "keywords", "block_companies",
}

# Slug shapes that are cheap to check without a network call.
_URL = re.compile(r"^https?://", re.I)
_HOSTNAME = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def _check_slug(platform: str, slug: str) -> str | None:
    """Return a complaint about this slug, or None if it looks plausible."""
    if platform == "workday":
        parts = slug.split("|")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            return "expected 'tenant|wdN|SiteName'"
        if not re.fullmatch(r"wd\d+", parts[1].strip()):
            return f"middle segment should be wdN, got {parts[1]!r}"
    elif platform == "oraclecloud":
        host = slug.split("|")[0]
        if not _HOSTNAME.match(host):
            return "expected 'host|siteNumber', e.g. 'x.fa.em2.oraclecloud.com|CX_1'"
    elif platform == "phenom":
        if not _HOSTNAME.match(slug.replace("https://", "").strip("/")):
            return "expected a careers hostname, e.g. 'careers.example.com'"
    elif platform in ("rss", "jsonld", "sitemap"):
        if not _URL.match(slug.split("|")[0]):
            return "expected a full http(s) URL"
    elif platform == "jobspy":
        site = slug.split("|")[0].strip().lower()
        from .sources import JOBSPY_WORKING
        if site not in JOBSPY_WORKING:
            return (f"{site!r} returns nothing; working back-ends are "
                    f"{', '.join(sorted(JOBSPY_WORKING))}")
    elif platform in ("instahyre", "themuse"):
        if slug and not slug.strip().isdigit():
            return "expected a number, or nothing"
    elif "|" not in slug and "/" in slug:
        return "looks like a URL or path; most platforms want just the slug"
    return None


def check_config(path: str) -> list[str]:
    """Return a list of problems. Empty means the config is sane."""
    problems: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return [f"{path}: no such file"]
    except yaml.YAMLError as e:
        return [f"{path}: invalid YAML — {e}"]

    if not isinstance(cfg, dict):
        return [f"{path}: top level should be a mapping"]

    sources = cfg.get("sources")
    if sources is None:
        problems.append("no 'sources:' section")
    elif not isinstance(sources, dict):
        problems.append("'sources:' should be a mapping of platform -> list")
    else:
        for platform, slugs in sources.items():
            if platform not in ADAPTERS:
                near = [a for a in ADAPTERS if a.startswith(str(platform)[:4])]
                hint = f" (did you mean {near[0]}?)" if near else ""
                problems.append(f"sources.{platform}: no such adapter{hint}")
                continue
            if not slugs:
                if platform not in NO_SLUG:
                    problems.append(
                        f"sources.{platform}: empty, so it never runs — "
                        "delete it or give it a slug")
                continue
            if not isinstance(slugs, list):
                problems.append(f"sources.{platform}: should be a list")
                continue
            seen: set[str] = set()
            for slug in slugs:
                text = str(slug).strip()
                if not text:
                    problems.append(f"sources.{platform}: blank entry")
                    continue
                if text in seen:
                    problems.append(f"sources.{platform}: {text!r} listed twice")
                seen.add(text)
                complaint = _check_slug(platform, text)
                if complaint:
                    problems.append(f"sources.{platform}: {text!r} — {complaint}")

    filters = cfg.get("filters")
    if filters is not None:
        if not isinstance(filters, dict):
            problems.append("'filters:' should be a mapping")
        else:
            for key in filters:
                if key not in KNOWN_FILTERS:
                    near = [k for k in KNOWN_FILTERS if k.startswith(str(key)[:6])]
                    hint = f" (did you mean {near[0]}?)" if near else ""
                    problems.append(
                        f"filters.{key}: not a filter this project reads — "
                        f"it will be ignored{hint}")
            unknown = str(filters.get("unknown_location", "keep")).lower()
            if unknown not in ("keep", "drop"):
                problems.append(
                    f"filters.unknown_location: expected 'keep' or 'drop', "
                    f"got {unknown!r}")
            for key in ("max_years_experience", "max_age_days"):
                value = filters.get(key)
                if value is not None and not isinstance(value, int):
                    problems.append(f"filters.{key}: expected a whole number")
    return problems


def report(paths: Iterable[str]) -> int:
    """Print problems for each config. Returns a process exit code."""
    total = 0
    for path in paths:
        problems = check_config(path)
        total += len(problems)
        if problems:
            print(f"{path}: {len(problems)} problem(s)")
            for p in problems:
                print(f"  - {p}")
        else:
            print(f"{path}: ok")
    return 1 if total else 0
