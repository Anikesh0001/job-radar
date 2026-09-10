"""Turn a resume into a structured profile the matcher and applier can use.

Deliberately not an LLM call. A CV is short, highly conventional, and the
fields that matter — skills, titles, years, contact — are all findable with
patterns. That keeps this free, offline, instant, and reproducible, and it
means your CV never leaves the machine.

The output is written to profile.yaml, which is gitignored and which you are
expected to edit: parsing gets the facts, but only you can say what your notice
period is or whether you would relocate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Skills worth matching on, grouped so the matcher can reason about breadth
# rather than just counting keyword hits. Aliases map what a CV says to what a
# job ad says — "js" and "javascript", "ml" and "machine learning".
SKILL_GROUPS: dict[str, dict[str, list[str]]] = {
    "language": {
        "python": ["python"], "java": ["java"], "javascript": ["javascript", "js "],
        "typescript": ["typescript"], "c++": ["c++", "cpp"], "c#": ["c#", ".net"],
        "c": ["c programming", "c language"], "go": ["golang"], "rust": ["rust"],
        "kotlin": ["kotlin"], "swift": ["swift"], "php": ["php"], "ruby": ["ruby"],
        "scala": ["scala"], "sql": ["sql"],
    },
    "frontend": {
        "react": ["react"], "vue": ["vue"], "angular": ["angular"], "svelte": ["svelte"],
        "html": ["html*"], "css": ["css*"], "tailwind": ["tailwind"],
        "nextjs": ["next.js", "nextjs"], "redux": ["redux"],
    },
    "backend": {
        "flask": ["flask"], "django": ["django"], "fastapi": ["fastapi"],
        "node": ["node.js", "nodejs", "node"], "express": ["express"],
        "spring": ["spring"], "rails": ["rails"], "graphql": ["graphql"],
        "rest": ["rest api", "restful", "rest"], "grpc": ["grpc"],
        "jwt": ["jwt"], "oauth": ["oauth"], "microservices": ["microservice*"],
    },
    "data": {
        "postgres": ["postgres*"], "mysql": ["mysql"],
        "sqlite": ["sqlite"], "mongodb": ["mongo*"], "redis": ["redis"],
        "elasticsearch": ["elastic*"], "sqlalchemy": ["sqlalchemy"],
        "firebase": ["firebase"], "kafka": ["kafka"], "rabbitmq": ["rabbitmq"],
        "celery": ["celery"], "airflow": ["airflow"], "spark": ["spark"],
        "pandas": ["pandas"], "numpy": ["numpy"], "etl": ["etl"],
    },
    "ml": {
        "machine learning": ["machine learning"], "deep learning": ["deep learning"],
        "nlp": ["nlp", "natural language"], "computer vision": ["computer vision", "opencv"],
        "llm": ["llm", "large language model", "genai", "generative ai"],
        "scikit-learn": ["scikit*", "sklearn"], "pytorch": ["pytorch"],
        "tensorflow": ["tensorflow"],
    },
    "infra": {
        "docker": ["docker"], "kubernetes": ["kubernet*", "k8s"], "aws": ["aws", "amazon web"],
        "gcp": ["gcp", "google cloud"], "azure": ["azure"], "terraform": ["terraform"],
        "linux": ["linux"], "git": ["git", "github", "bitbucket"],
        "ci/cd": ["ci/cd", "jenkins", "github actions"], "nginx": ["nginx"],
    },
}

# Flattened alias -> canonical skill, longest alias first so "machine learning"
# is tried before "ml".
_ALIASES: list[tuple[str, str, str]] = sorted(
    ((alias, skill, group)
     for group, skills in SKILL_GROUPS.items()
     for skill, aliases in skills.items()
     for alias in aliases),
    key=lambda t: -len(t[0]),
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?\d{5}[\s-]?\d{5}|\+?\d[\d\s().-]{9,}\d")
_YEAR_RANGE = re.compile(r"(20\d{2})\s*[–—\-]\s*(20\d{2}|present|current)", re.I)
_LINK = re.compile(r"(https?://[^\s)]+|(?:github|linkedin)\.com/[\w\-./]+)", re.I)

# Section headings a CV uses, so we can look for skills in the right places.
_SECTION = re.compile(
    r"^\s*(professional summary|summary|objective|experience|work experience|"
    r"education|technical skills|skills|projects|achievements|certifications?"
    r"[\w &]*)\s*$",
    re.I | re.M,
)


@dataclass
class Profile:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    headline: str = ""
    years_experience: float = 0.0
    skills: list[str] = field(default_factory=list)
    skill_groups: dict[str, list[str]] = field(default_factory=dict)
    titles: list[str] = field(default_factory=list)
    education: str = ""
    links: dict[str, str] = field(default_factory=dict)
    resume_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def extract_text(path: str | Path) -> str:
    """Read a .pdf, .docx or plain-text resume into one string."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("reading a PDF needs pypdf: pip install pypdf") from e
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        try:
            import docx
        except ImportError as e:
            raise RuntimeError("reading a .docx needs python-docx") from e
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return path.read_text(encoding="utf-8", errors="replace")


def _alias_pattern(alias: str) -> re.Pattern:
    """Match an alias as a whole token.

    Plain substring matching looked fine and was quietly wrong: "html " ends
    in "ml ", so every CV mentioning HTML claimed machine learning, and "ts "
    matched "projects" and awarded TypeScript to someone who had never used it.
    A profile that overstates your skills is worse than one that misses a few.
    """
    alias = alias.strip()
    # A trailing * means "this alias is a prefix": HTML5 and CSS3 carry a
    # version number, MongoDB extends "mongo". Opting in per alias keeps the
    # looseness where it is needed instead of granting it to "go" and "c".
    prefix = alias.endswith("*")
    core = alias[:-1] if prefix else alias
    escaped = re.escape(core)
    # \b does not work next to +, # or . — anchor on a non-word char instead.
    left = r"\b" if core[:1].isalnum() else r"(?<![\w])"
    right = r"\w*" if prefix else (r"\b" if core[-1:].isalnum() else r"(?![\w])")
    return re.compile(left + escaped + right, re.I)


_ALIAS_RE = [(_alias_pattern(a), skill, group) for a, skill, group in _ALIASES]


def find_skills(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Canonical skills mentioned anywhere in the resume, grouped by kind."""
    blob = re.sub(r"\s+", " ", text)
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    for pattern, skill, group in _ALIAS_RE:
        if skill in seen:
            continue
        if pattern.search(blob):
            seen.add(skill)
            found.setdefault(group, []).append(skill)
    return sorted(seen), {g: sorted(v) for g, v in found.items()}


def estimate_years(text: str) -> float:
    """Years of professional experience, from the date ranges in the CV.

    Education ranges are excluded on purpose: a four-year degree is not four
    years of work, and counting it would push every posting's experience filter
    out of reach.
    """
    lowered = text.lower()
    edu_at = lowered.find("education")
    exp_at = lowered.find("experience")
    total = 0.0
    for m in _YEAR_RANGE.finditer(text):
        # Skip ranges that sit inside the education section.
        if edu_at != -1 and exp_at != -1 and edu_at < m.start() < (
            len(text) if exp_at < edu_at else exp_at
        ) and exp_at < edu_at:
            continue
        if edu_at != -1 and m.start() > edu_at and (exp_at == -1 or exp_at < edu_at):
            continue
        start = int(m.group(1))
        end_raw = m.group(2)
        end = date.today().year if end_raw.lower() in ("present", "current") else int(end_raw)
        if 1990 < start <= end <= date.today().year + 1:
            total = max(total, float(end - start))
    if total:
        return total
    # No explicit range: "Present" against a current role means at least a
    # partial year, which is very different from a fresher for the filters.
    return 0.5 if re.search(r"\b(present|current)\b", lowered) else 0.0


def parse(path: str | Path) -> Profile:
    text = extract_text(path)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    skills, groups = find_skills(text)

    email = (_EMAIL.search(text) or [None]) and (
        _EMAIL.search(text).group(0) if _EMAIL.search(text) else "")
    phones = [p for p in _PHONE.findall(text) if len(re.sub(r"\D", "", p)) >= 10]

    links: dict[str, str] = {}
    for raw in _LINK.findall(text):
        low = raw.lower()
        if "github" in low and "github" not in links:
            links["github"] = raw
        elif "linkedin" in low and "linkedin" not in links:
            links["linkedin"] = raw

    # The first line of a CV is the name; the second is usually the headline.
    name = lines[0] if lines else ""
    headline = lines[1] if len(lines) > 1 and len(lines[1]) < 60 else ""

    location = ""
    for ln in lines[:6]:
        m = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?),\s*(India|USA|UK|Germany|Canada)", ln)
        if m:
            location = m.group(0)
            break

    titles: list[str] = []
    for ln in lines:
        if re.search(r"\b(developer|engineer|analyst|scientist|intern|architect|manager)\b",
                     ln, re.I) and len(ln) < 70 and not ln.endswith("."):
            cleaned = re.sub(r"\s*[–—-]\s*(present|current|\d{4}).*$", "", ln, flags=re.I).strip()
            if 3 < len(cleaned) < 60 and cleaned not in titles:
                titles.append(cleaned)

    education = ""
    # \b matters here: without it "B.E." matched the "Be" of "Bengaluru" and
    # the profile claimed a degree from a city.
    m = re.search(
        r"\b(Bachelor|Master|B\.?E\.?|B\.?Tech|M\.?Tech|B\.?S\.?|M\.?S\.?|PhD)\b"
        r"[^\n]{0,80}",
        text,
    )
    if m:
        education = m.group(0).strip()

    return Profile(
        name=name,
        email=email or "",
        phone=phones[0].strip() if phones else "",
        location=location,
        headline=headline,
        years_experience=estimate_years(text),
        skills=skills,
        skill_groups=groups,
        titles=titles[:6],
        education=education,
        links=links,
        resume_path=str(path),
    )


# Questions nearly every application asks and no resume answers. Written into
# profile.yaml as blanks for you to fill once, then reused for every apply.
DEFAULT_ANSWERS = {
    "work_authorization": "Indian citizen, authorised to work in India",
    "requires_sponsorship": "No",
    "notice_period": "",
    "current_ctc": "",
    "expected_ctc": "",
    "willing_to_relocate": "Yes",
    "preferred_locations": "Bengaluru, Mysore, Remote",
    "earliest_start_date": "",
    "how_did_you_hear": "Company careers page",
    "gender": "",
    "pronouns": "",
    "veteran_status": "",
    "disability_status": "",
}


def build_profile_yaml(resume_path: str | Path, out: str | Path = "profile.yaml",
                       overwrite: bool = False) -> Path:
    """Parse a resume into profile.yaml, preserving anything already answered.

    Re-running after editing the file must not wipe your answers, so existing
    values win over freshly parsed ones for everything under `answers`.
    """
    import yaml

    out = Path(out)
    existing: dict = {}
    if out.exists() and not overwrite:
        existing = yaml.safe_load(out.read_text(encoding="utf-8")) or {}

    parsed = parse(resume_path).to_dict()
    answers = {**DEFAULT_ANSWERS, **(existing.get("answers") or {})}

    # Parsed facts refresh; anything you typed by hand survives.
    merged = {**parsed, **{k: v for k, v in existing.items()
                           if k not in parsed and k != "answers"}}
    for key in ("name", "email", "phone", "location"):
        if existing.get(key):
            merged[key] = existing[key]
    merged["answers"] = answers

    header = (
        "# Generated from your resume by `python apply.py profile`.\n"
        "# Edit freely — re-running preserves everything under `answers:`\n"
        "# and any contact detail you have corrected by hand.\n"
        "#\n"
        "# This file is gitignored. It holds your phone number and email.\n\n"
    )
    out.write_text(header + yaml.safe_dump(merged, sort_keys=False,
                                           allow_unicode=True), encoding="utf-8")
    return out


def load_profile(path: str | Path = "profile.yaml") -> Profile:
    """Read profile.yaml back into a Profile (answers are kept separately)."""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    known = set(Profile.__dataclass_fields__)
    return Profile(**{k: v for k, v in data.items() if k in known})


def load_answers(path: str | Path = "profile.yaml") -> dict:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return data.get("answers") or {}
