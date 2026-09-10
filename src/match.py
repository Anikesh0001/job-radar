"""Score a posting against your profile, and say why.

The score exists to answer one question: of the several hundred postings this
run found, which twenty are worth your afternoon? It is a ranking aid, not a
verdict — a 40 you are excited about beats an 82 you are not.

Everything here is explainable on purpose. A black-box number you cannot argue
with is useless for deciding where to spend an application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job
from .resume import SKILL_GROUPS, Profile, _alias_pattern

# Reuse the resume taxonomy so a skill means the same thing on both sides.
_JOB_ALIASES = sorted(
    ((alias, skill) for skills in SKILL_GROUPS.values()
     for skill, aliases in skills.items() for alias in aliases),
    key=lambda t: -len(t[0]),
)
_JOB_ALIAS_RE = [(_alias_pattern(a), skill) for a, skill in _JOB_ALIASES]

# "3+ years", "minimum 5 years", "2-4 years of experience".
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?year", re.I)

# Seniority the title is asking for, in ascending order.
_SENIORITY = [
    (re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I), 0),
    (re.compile(r"\b(fresher|graduate|new ?grad|campus|entry[- ]level|junior|jr)\b", re.I), 1),
    (re.compile(r"\b(sde ?[i1]|swe ?[i1]|engineer ?[i1]|associate)\b", re.I), 1),
    (re.compile(r"\b(senior|sr\.?|sde ?[23]|swe ?[23]|lead)\b", re.I), 3),
    (re.compile(r"\b(staff|principal|architect|head of|director|vp|chief)\b", re.I), 4),
]


def job_skills(job: Job) -> set[str]:
    """Skills a posting mentions, using the same vocabulary as the resume."""
    blob = re.sub(r"\s+", " ", f"{job.title} {job.description or ''}")
    return {skill for pattern, skill in _JOB_ALIAS_RE if pattern.search(blob)}


def required_years(job: Job) -> int | None:
    matches = _YEARS.findall(job.description or "")
    return min(int(m) for m in matches) if matches else None


def seniority(title: str) -> int:
    """0 intern .. 4 principal. 2 means "no signal", i.e. plain mid-level."""
    for pattern, level in _SENIORITY:
        if pattern.search(title or ""):
            return level
    return 2


@dataclass
class Match:
    score: int              # 0-100
    reasons: list[str]      # why it scored that way, best first
    overlap: list[str]      # skills you have that the posting asks for
    missing: list[str]      # skills it asks for that you did not list

    @property
    def label(self) -> str:
        if self.score >= 75:
            return "strong"
        if self.score >= 55:
            return "good"
        if self.score >= 35:
            return "worth a look"
        return "weak"


def score(job: Job, profile: Profile) -> Match:
    """Rate one posting 0-100 against a profile."""
    mine = set(profile.skills)
    theirs = job_skills(job)
    reasons: list[str] = []

    # --- skills, 55 points -------------------------------------------------
    # Fraction of what they asked for that you actually have. Scored as a
    # proportion rather than a count so a job listing three technologies is not
    # automatically a worse match than one listing twenty.
    overlap = sorted(mine & theirs)
    missing = sorted(theirs - mine)
    if theirs:
        coverage = len(overlap) / len(theirs)
        # Coverage alone rewarded vagueness: a posting naming one technology
        # you happen to know scored 1/1 and beat a detailed one you matched
        # six ways. Confidence scales with how much the posting actually
        # committed to, so a thin ad cannot reach the top on a single word.
        confidence = min(1.0, 0.45 + 0.14 * len(theirs))
        skill_points = 55 * coverage * confidence
        reasons.append(
            f"{len(overlap)}/{len(theirs)} listed skills match"
            + ("" if len(theirs) >= 4 else " (posting names few specifics)")
        )
    else:
        # No recognisable technology at all. Common for vague postings; neither
        # reward nor punish it.
        skill_points = 22.0
        reasons.append("no specific tech named")

    # --- seniority, 25 points ---------------------------------------------
    want = seniority(job.title)
    have = 1 if profile.years_experience < 2 else (2 if profile.years_experience < 5 else 3)
    distance = abs(want - have)
    seniority_points = max(0.0, 25 - distance * 11)
    if want >= 4 and have <= 1:
        reasons.append("far more senior than your experience")
    elif distance == 0:
        reasons.append("seniority fits")
    elif want < have:
        reasons.append("more junior than you")
    else:
        reasons.append("a stretch on seniority")

    # --- stated experience, 20 points -------------------------------------
    need = required_years(job)
    if need is None:
        years_points = 14.0
    elif profile.years_experience >= need:
        years_points = 20.0
        reasons.append(f"meets the {need}y requirement")
    else:
        gap = need - profile.years_experience
        years_points = max(0.0, 20 - gap * 6)
        reasons.append(f"asks for {need}y, you have {profile.years_experience:g}")

    total = round(skill_points + seniority_points + years_points)
    total = max(0, min(100, total))

    if overlap:
        reasons.insert(0, "matches " + ", ".join(overlap[:5]))
    return Match(score=total, reasons=reasons, overlap=overlap, missing=missing[:8])
