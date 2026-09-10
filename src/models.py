"""Normalised job posting model shared by every source adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

# Noise that shows up in titles and destroys naive deduplication.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|full[- ]time|part[- ]time|contract|"
    r"urgent|hiring|immediate joiner|wfh|w/?f/?h)\b",
    re.I,
)
_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

# Common Indian metro spellings -> canonical form.
_CITY_ALIASES = {
    "bengaluru": "bangalore",
    "banglore": "bangalore",
    "blr": "bangalore",
    "gurgaon": "gurugram",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    "trivandrum": "thiruvananthapuram",
    "noida uttar pradesh": "noida",
    "hyderabad telangana": "hyderabad",
}


def normalise(text: str, strip_noise: bool = True) -> str:
    """Lowercase, strip bracketed asides and punctuation, collapse whitespace.

    strip_noise removes words like "remote" and "full-time" that pollute job
    TITLES. Never use it on a location — "Remote" is a real location, and
    stripping it leaves an empty string that matches everything.
    """
    if not text:
        return ""
    t = _BRACKETS.sub(" ", text.lower())
    if strip_noise:
        t = _TITLE_NOISE.sub(" ", t)
    t = _NON_ALNUM.sub(" ", t)
    return _WS.sub(" ", t).strip()


# Legal / generic suffixes that one source keeps and another drops.
_COMPANY_SUFFIX = (
    "privatelimited", "corporation", "technologies", "technology", "incorporated",
    "solutions", "holdings", "software", "systems", "company", "limited",
    "group", "labs", "gmbh", "corp", "plc", "pvt", "ltd", "llc", "inc", "sa", "bv",
)


def normalise_company(text: str) -> str:
    """Company key for deduplication: no spaces, no legal suffix.

    Sources disagree about company names in ways that are pure noise.
    SmartRecruiters' board calls it "BoschGroup" and its search API calls the
    same employer "Bosch Group"; Greenhouse says "newrelic" where Instahyre
    says "New Relic". Those collided on nothing but a space, so the same job
    was stored twice.

    Spaces come out FIRST, then trailing suffixes — do it the other way round
    and "Bosch Group" reduces to "bosch" while "BoschGroup" stays whole, which
    makes the mismatch worse rather than better.
    """
    t = normalise(text, strip_noise=False).replace(" ", "")
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIX:
            # Keep a couple of characters, so "Inc" alone doesn't become "".
            if t.endswith(suffix) and len(t) > len(suffix) + 2:
                t = t[: -len(suffix)]
                changed = True
                break
    return t or normalise(text, strip_noise=False).replace(" ", "")


def normalise_location(text: str) -> str:
    """Location-safe normalisation: keeps words like 'remote' intact."""
    return normalise(text, strip_noise=False)


def canon_location(text: str) -> str:
    """Map a free-text location onto a canonical city name where possible."""
    t = normalise_location(text)
    for alias, canon in _CITY_ALIASES.items():
        if alias in t:
            return canon
    # Take the first comma-separated component of the original string.
    head = normalise_location(text.split(",")[0]) if text else ""
    return head or t


# Most ATS adapters only know the company by its URL slug, so the channel was
# announcing roles at "gitlab", "grafanalabs", "hpe" and "BoschGroup". In a
# public feed that reads as broken rather than terse, and it is the one piece
# of every message a reader scans first.
_COMPANY_STYLE = {
    # Acronyms and initialisms, which title-casing mangles.
    "hpe": "HPE", "ibm": "IBM", "sap": "SAP", "hcl": "HCL", "tcs": "TCS",
    "aws": "AWS", "amd": "AMD", "arm": "ARM", "ey": "EY", "kpmg": "KPMG",
    "pwc": "PwC", "nvidia": "NVIDIA", "sas": "SAS", "bmc": "BMC",
    "qad": "QAD", "epam": "EPAM", "hcltech": "HCLTech", "ltimindtree": "LTIMindtree",
    # Names with internal capitals a title() would flatten.
    "gitlab": "GitLab", "github": "GitHub", "openai": "OpenAI",
    "mongodb": "MongoDB", "postgresql": "PostgreSQL", "youtube": "YouTube",
    "linkedin": "LinkedIn", "paypal": "PayPal", "ebay": "eBay",
    "deepmind": "DeepMind", "servicenow": "ServiceNow", "salesforce": "Salesforce",
    "browserstack": "BrowserStack", "freshworks": "Freshworks",
    "razorpay": "Razorpay", "phonepe": "PhonePe", "makemytrip": "MakeMyTrip",
    "nobroker": "NoBroker", "cardekho": "CarDekho", "bigbasket": "BigBasket",
    "upgrad": "upGrad", "byjus": "BYJU'S", "boat": "boAt",
    # Multi-word names that arrive as one lowercase token.
    "grafanalabs": "Grafana Labs", "cockroachlabs": "Cockroach Labs",
    "scaleai": "Scale AI", "togetherai": "Together AI", "newrelic": "New Relic",
    "boschgroup": "Bosch Group", "remotecom": "Remote.com",
    "digitalocean": "DigitalOcean", "planetscale": "PlanetScale",
    "launchdarkly": "LaunchDarkly", "cultureamp": "Culture Amp",
    "customerio": "Customer.io", "helpscout": "Help Scout",
    "observeai": "Observe.AI", "startree": "StarTree", "flyio": "Fly.io",
    "lightmatter": "Lightmatter", "influxdata": "InfluxData",
    "apollographql": "Apollo GraphQL", "clickhouse": "ClickHouse",
    "thoughtworks": "Thoughtworks", "techmahindra": "Tech Mahindra",
    "rebelfoods": "Rebel Foods", "lendingkart": "Lendingkart",
    "surveymonkey": "SurveyMonkey", "workhuman": "Workhuman",
}

# "BoschGroup" -> "Bosch Group", but not "IBM" -> "I B M".
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def display_company(name: str) -> str:
    """A company name fit to put in front of readers.

    Leaves alone anything that already looks like a real name — if a source
    gave us "Thermo Fisher Scientific", that is better than anything we could
    derive. Only slugs get rewritten.
    """
    raw = (name or "").strip()
    if not raw:
        return ""

    override = _COMPANY_STYLE.get(raw.lower().replace(" ", "").replace(".", ""))
    if override:
        return override

    # A name with a space and any capital came from a real API field.
    if " " in raw and raw != raw.lower():
        return raw

    split = _CAMEL.sub(" ", raw)
    if split != raw:
        return split

    if raw.islower() or raw.isupper():
        # Slug punctuation to spaces, then title-case each word.
        words = re.split(r"[-_.]+", raw)
        return " ".join(w[:1].upper() + w[1:] for w in words if w)
    return raw


# Every source states "when was this posted" differently. Left as-is these are
# unsortable and unfilterable, and a third of the database looked undated when
# it was not: Lever sends epoch milliseconds, Himalayas epoch seconds, RSS
# feeds RFC-2822, Workday and Instahyre a human sentence. Normalise once, here,
# so everything downstream can just compare strings.
_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
)
_RELATIVE = re.compile(
    r"(?:posted\s+)?(?:(today|yesterday)|(\d+)\s*\+?\s*(day|week|month|hour|minute)s?)",
    re.I,
)


def parse_date(value) -> str | None:
    """Best-effort convert any source's date into ISO 8601 UTC, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Epoch. 13 digits is milliseconds (Lever), 10 is seconds (Himalayas).
    if text.isdigit() and len(text) in (10, 13):
        stamp = int(text) / (1000 if len(text) == 13 else 1)
        try:
            return datetime.fromtimestamp(stamp, tz=UTC).isoformat(
                timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    cleaned = text.replace("Z", "+00:00").replace(" UTC", "+00:00")
    # "+0000" without a colon predates Python 3.7's parser in some formats.
    tz_fix = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", cleaned)
    for candidate in (cleaned, tz_fix):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            pass
        for fmt in _ISO_FORMATS:
            try:
                dt = datetime.strptime(candidate, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC).isoformat(timespec="seconds")
            except ValueError:
                continue

    # RFC 2822, as used by every RSS feed: "Tue, 08 Sep 2026 07:31:09 +0000".
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass

    # "Posted 6 Days Ago", "Posted Today", "Posted 30+ Days Ago".
    m = _RELATIVE.search(text)
    if m:
        now = datetime.now(UTC)
        word, amount, unit = m.group(1), m.group(2), m.group(3)
        if word:
            delta = timedelta(days=0 if word.lower() == "today" else 1)
        else:
            per = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1),
                   "day": timedelta(days=1), "week": timedelta(weeks=1),
                   "month": timedelta(days=30)}[unit.lower()]
            delta = per * int(amount)
        return (now - delta).isoformat(timespec="seconds")

    return None


@dataclass
class Job:
    company: str
    title: str
    url: str
    source: str = "manual"
    location: str = ""
    description: str = ""
    posted_at: str | None = None  # ISO 8601
    salary: str = ""  # free text as the source stated it, e.g. "₹12L – ₹18L"
    match_score: int | None = None  # 0-100 against profile.yaml, None if unscored
    first_seen: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    @property
    def fingerprint(self) -> str:
        """Stable id for exact dedupe across sources.

        Deliberately excludes the URL: the same role posted on Greenhouse and
        surfaced again via a Google Jobs crawl has two URLs but one fingerprint.
        """
        key = "|".join(
            [
                normalise_company(self.company),
                normalise(self.title),
                canon_location(self.location),
            ]
        )
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    @property
    def dedupe_key(self) -> str:
        """Looser key used for fuzzy near-duplicate comparison.

        Includes the canonical city for the same reason the fingerprint does:
        without it, one "Technical Support Engineer" in Amsterdam fuzzy-matched
        the Tokyo, Singapore and Chicago postings of the same title and threw
        them away. Those are four different jobs, and for a job hunter the city
        is the whole point.
        """
        return (
            f"{normalise_company(self.company)} {normalise(self.title)} "
            f"{canon_location(self.location)}"
        )

    def __post_init__(self) -> None:
        self.posted_at = parse_date(self.posted_at)
        self.company = display_company(self.company)

    def to_row(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d

    def __str__(self) -> str:
        loc = f" — {self.location}" if self.location else ""
        return f"{self.company}: {self.title}{loc}"
