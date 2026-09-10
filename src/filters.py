"""Relevance filtering.

Order matters: a title can match an include pattern and still be rejected by
an exclude pattern ("Senior Manager, University Recruiting" is not a grad job).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import Job, canon_location, normalise, normalise_location

# Titles that indicate an entry-level or internship role.
INCLUDE = re.compile(
    r"\b("
    r"intern|internship|trainee|apprentice|"
    r"fresher|graduate|new ?grad|campus|university|"
    r"entry[- ]level|junior|jr|associate|"
    r"sde ?[i1]\b|swe ?[i1]\b|engineer ?[i1]\b|analyst ?[i1]\b|"
    r"early ?career"
    r")\b",
    re.I,
)

# Seniority markers that override an include match.
EXCLUDE = re.compile(
    r"\b("
    r"senior|sr\.?|staff|principal|lead|head of|director|vp|vice president|"
    r"chief|architect|manager|mgr|expert|specialist ?i{2,}|"
    r"sde ?[2-9]|swe ?[2-9]|engineer ?i{2,}|level ?[3-9]"
    r")\b",
    re.I,
)

# "5+ years", "minimum 4 years of experience" etc. in the body text.
YEARS = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?year", re.I)

# Remote, in the many ways a job board spells it. Checked before the location
# list, because a remote role is worth surfacing wherever the company sits.
REMOTE = re.compile(
    r"\b(remote|work ?from ?home|\bwfh\b|anywhere|distributed|virtual|"
    r"home[- ]based|telecommute)\b",
    re.I,
)

# "Bengaluru, KA, IN" — Indeed and several ATSes put a two-letter country code
# last. Matching that is the difference between catching those postings and
# silently dropping a third of the India inventory, since none of them contain
# the word "India" at all.
COUNTRY_TAIL = re.compile(r",\s*([A-Za-z]{2})\s*$")

# Workday reports a multi-site posting as "2 Locations" and never says which,
# so there is nothing to match against — treat it as unknown, not as a miss.
VAGUE_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$|^\s*(hybrid|multiple)\s*$", re.I)


# Roles that count as IT/tech. The broad sources (SmartRecruiters' global
# search, a 4,800-opening Bosch board) are not tech-specific — without this
# the sheet fills up with forklift drivers and staff nurses. Matched against
# the TITLE only: a marketing job whose description mentions "our engineering
# team" is still a marketing job.
#
# Note there is no trailing \b on the group. Job titles are full of plurals
# and suffixes — "Data Scientist", "Web Developers", "Cybersecurity",
# "Networking" — and a closing \b silently rejects every one of them, because
# the alternative matches a prefix and the next character is still a letter.
# Short or ambiguous tokens carry their own \b instead, so "SAP" does not
# match "Sapphire".
IT_TITLE = re.compile(
    r"\b(?:"
    r"software|developer|programmer|engineer|"
    r"sde\b|swe\b|sdet\b|"
    r"backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|"
    r"web ?dev|mobile ?dev|android|ios\b|flutter|react\b|angular|vue\b|node\b|"
    r"python|java|golang|\bgo\b|rust\b|scala\b|kotlin|typescript|javascript|"
    r"\.net\b|c\+\+|c#|php\b|ruby|rails|django|spring\b|"
    r"data ?(?:scien|engineer|analy|architect|warehouse|pipeline|platform)|"
    r"analytics|"
    r"machine ?learning|deep ?learning|\bml\b|\bai\b|artificial intelligence|"
    r"\bnlp\b|computer vision|\bllm\b|generative|"
    r"devops|\bsre\b|site ?reliability|platform ?engineer|infrastructure|"
    r"cloud|\baws\b|azure|\bgcp\b|kubernetes|docker|terraform|"
    r"database|\bdba\b|\bsql\b|\betl\b|"
    r"\bqa\b|quality ?assurance|tester|testing|automation|"
    r"security|cyber|infosec|appsec|penetration test|"
    r"network|system ?admin|sysadmin|\bit\b|technical ?support|helpdesk|"
    r"solutions? ?architect|technical ?architect|tech ?lead|"
    r"product ?manager|product ?owner|business ?analyst|scrum|agile|"
    r"\bui\b|\bux\b|user ?experience|user ?interface|product ?design|"
    r"blockchain|web3|smart ?contract|"
    r"embedded|firmware|\biot\b|robotic|"
    r"salesforce|\bsap\b|oracle|\berp\b|\bcrm\b|"
    r"technolog|technical|computer|informatics"
    r")",
    re.I,
)

# Titles that contain a tech word but are not tech jobs. "Sales Engineer" and
# "Mechanical Engineer" both match IT_TITLE on a naive read. Checked first, so
# it always wins — these are the unconditional rejections.
NOT_IT = re.compile(
    r"\b(?:"
    # `sales\b`, not `sales` — without the boundary this rejected every
    # "Salesforce Developer", which is squarely an IT role.
    r"sales\b|pre[- ]?sales|account ?(?:executive|manager)|business ?development|"
    r"recruit|talent ?acquisition|\bhr\b|human ?resource|"
    # Allow a qualifier between the discipline and "engineer": the adjacent-
    # words version missed "Civil Project Engineer" and "Mechanical Design
    # Engineer", which are exactly as non-IT as the plain forms.
    r"(?:civil|mechanical|electrical|chemical|industrial|structural|process|"
    r"manufacturing|production|petroleum|mining|marine|aerospace|automotive|"
    r"geotechnical|environmental)\b[\w ]{0,14}\bengineer|"
    r"manufactur|production ?(?:operator|associate|supervisor)|"
    r"field ?(?:engineer|technician|service)|service ?technician|maintenance|"
    r"nurse|nursing|physician|clinical|pharmac|medical|healthcare ?assistant|"
    r"driver|warehouse|forklift|logistics|supply ?chain|"
    r"cashier|retail|store ?(?:manager|associate)|barista|waiter|chef|cook\b|"
    r"housekeep|janitor|"
    r"teacher|tutor|professor|lecturer|faculty|trainer|training ?(?:manager|lead)|"
    r"accountant|accounting|bookkeep|payroll|audit|tax\b|"
    r"welder|machinist|electrician|plumber|carpenter|fitter|"
    # Civil and heavy infrastructure. "Principal Tunnel Ventilation Systems
    # Engineer" reached the channel on the strength of the word "engineer".
    r"tunnel|ventilation|\bhvac\b|plumbing|surveying|highway|bridge ?design|"
    r"geotechnic|architectural (?!software)|interior design|"
    # Semiconductor packaging and silicon layout: "IC Package Development
    # Engineer" is a hardware role, whatever "development" suggests.
    r"\bic\b ?(?:package|design|layout|validation)|package development|"
    r"wafer|lithograph|foundry|\bpcb\b|\bvlsi\b|\brtl\b ?design|"
    r"marketing|content ?writer|copywriter|social ?media|"
    r"legal|paralegal|attorney|counsel\b"
    r")",
    re.I,
)

# Discipline words that are non-IT *unless* the title also carries an
# unambiguous software signal. Splitting these out matters: rejecting
# "Security Officer" outright would have thrown away every Chief Information
# Security Officer, and rejecting "Reliability Engineer" would have thrown away
# every Site Reliability Engineer — 30+ real SRE roles in one run.
NOT_IT_SOFT = re.compile(
    r"\b(?:"
    r"security ?(?:officer|guard)|"
    r"quality ?engineer|"
    r"reliability|"
    r"\behs\b|\bhse\b|safety|"
    r"hvac|thermal|hydraulic|solder|piping|metallurg|acoustic|"
    r"powertrain|eaxle|emachine|"
    r"instrumentation|controls ?engineer|commissioning|calibration|"
    r"cost ?engineer|packaging|validation ?engineer"
    r")",
    re.I,
)

# Unambiguous software/data signals. Any one of these overrides NOT_IT_SOFT.
STRONG_IT = re.compile(
    r"\b(?:"
    r"software|developer|devops|\bsre\b|site ?reliability|"
    r"information ?security|cyber|infosec|appsec|"
    r"data ?(?:scien|engineer|analy|platform)|machine ?learning|\bml\b|\bai\b|"
    r"cloud|kubernetes|docker|terraform|\baws\b|azure|\bgcp\b|"
    r"full[- ]?stack|backend|back[- ]end|frontend|front[- ]end|"
    r"\bqa\b|quality ?assurance|\bsdet\b|test ?automation|"
    r"python|java|golang|kotlin|typescript|javascript|react|node|"
    r"platform ?engineer|infrastructure|database|\bsql\b|\bapi\b|"
    r"web|mobile|android|\bios\b|firmware|embedded|"
    r"network|salesforce|tech ?lead|"
    # "Data Reliability Engineering" and "Platform Reliability Engineering"
    # are software teams; "Data Center Commissioning" is a building, so the
    # qualifier has to be explicit rather than a bare \bdata\b.
    r"(?:data|platform|service|software|system) ?reliability|"
    # Engineering-org titles: "Director of Engineering, Safety" and
    # "Engineering Manager, Safety" are Trust & Safety *software* teams.
    r"engineering ?manager|(?:director|head|vp) of engineering|"
    r"product ?manager|product ?owner"
    r")",
    re.I,
)

class Filter:
    def __init__(self, cfg: dict):
        f = cfg.get("filters") or {}
        # normalise_location, not normalise: "remote" must survive. The falsy
        # filter guards against a config entry collapsing to "", which would
        # otherwise substring-match every location on earth.
        self.locations = [
            n for n in (normalise_location(x) for x in (f.get("locations") or [])) if n
        ]
        # Two-letter country codes, matched against a trailing ", IN".
        self.countries = {
            str(c).strip().lower() for c in (f.get("countries") or []) if str(c).strip()
        }
        # A remote posting passes wherever the employer is.
        self.allow_remote = bool(f.get("allow_remote", True))
        # What to do when the source states no usable location. "keep" is the
        # safe default; with a country filter on it is mostly noise, because an
        # unreadable location is far more likely to be abroad than local.
        self.unknown_location = str(f.get("unknown_location", "keep")).lower()
        self.max_years = int(f.get("max_years_experience", 2))
        self.keywords = [normalise(x) for x in (f.get("keywords") or [])]
        self.blocked_companies = {normalise(x) for x in (f.get("block_companies") or [])}
        self.entry_level_only = bool(f.get("entry_level_only", True))
        # Default on: every broad source in the config is general-purpose.
        self.it_only = bool(f.get("it_only", True))
        # 0 disables the check. Postings whose source states no date at all
        # always pass — Rippling and BambooHR publish none, and dropping them
        # would silently delete two working sources rather than stale jobs.
        self.max_age_days = int(f.get("max_age_days", 0) or 0)

    def _location_ok(self, job: Job) -> bool:
        if not self.locations and not self.countries:
            return True

        original = (job.location or "").strip()

        # Remote first: a remote role is worth surfacing wherever it is based.
        if self.allow_remote and REMOTE.search(original):
            return True

        if not original or VAGUE_LOCATION.match(original):
            return self.unknown_location != "drop"

        raw = normalise_location(original)
        if not raw:
            return self.unknown_location != "drop"

        # Check the raw string and the canonical city, so a config entry of
        # "bangalore" still matches a posting that says "Bengaluru, India".
        canon = canon_location(original)
        if any(want in raw or want == canon for want in self.locations):
            return True

        m = COUNTRY_TAIL.search(original)
        return bool(m and m.group(1).lower() in self.countries)

    def _experience_ok(self, job: Job) -> bool:
        matches = YEARS.findall(job.description or "")
        if not matches:
            return True
        # If the posting mentions any range starting at or below the cap, keep it.
        return min(int(m) for m in matches) <= self.max_years

    def _keywords_ok(self, job: Job) -> bool:
        if not self.keywords:
            return True
        blob = normalise(f"{job.title} {job.description}")
        return any(k in blob for k in self.keywords)

    def _fresh_enough(self, job: Job) -> bool:
        if not self.max_age_days or not job.posted_at:
            return True
        try:
            posted = datetime.fromisoformat(job.posted_at)
        except ValueError:
            return True
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - posted).days
        # A posting dated in the future is a source with a broken clock, not a
        # job that has not happened yet. Keep it rather than lose it.
        return age <= self.max_age_days

    def _it_ok(self, job: Job) -> bool:
        title = job.title or ""
        if NOT_IT.search(title):
            return False
        # A soft veto only bites when nothing in the title says "software".
        if NOT_IT_SOFT.search(title) and not STRONG_IT.search(title):
            return False
        return bool(IT_TITLE.search(title))

    def reason(self, job: Job) -> str | None:
        """Return None if the job passes, else a short rejection reason."""
        # Indeed hides the employer on some listings. "Full Stack Engineer at
        # (blank)" is not a posting anyone can act on, so it is not worth a
        # message.
        if not (job.company or "").strip():
            return "no company name"
        if normalise(job.company) in self.blocked_companies:
            return "blocked company"
        if self.it_only and not self._it_ok(job):
            return "not an IT role"
        if self.entry_level_only:
            if EXCLUDE.search(job.title):
                return "senior title"
            if not INCLUDE.search(job.title):
                return "not entry level"
        if not self._location_ok(job):
            return "location mismatch"
        if not self._experience_ok(job):
            return "experience requirement"
        if not self._fresh_enough(job):
            return "too old"
        if not self._keywords_ok(job):
            return "no keyword match"
        return None

    def apply(self, jobs: list[Job]) -> list[Job]:
        return [j for j in jobs if self.reason(j) is None]
