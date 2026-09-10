"""What each posting's application form actually asks for.

Greenhouse publishes the question list on its public board API, which is
unusually helpful: it means we can tell you exactly what a job wants — and
pre-fill it — before you ever open the page.

What we cannot do is submit for you over the API. `POST` to the board and the
application endpoints both answer 401: submission is authenticated as the
*employer*, not the candidate. There is no candidate-side apply API on any of
the boards this project reads. Actually filling the form means driving a
browser, which is what src/autofill.py does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "job-radar/1.0 (+https://github.com/Anikesh0001/job-radar)"}


@dataclass
class Question:
    label: str
    required: bool
    kind: str                       # input_text | textarea | input_file | select | ...
    name: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class FormSpec:
    ats: str
    apply_url: str
    questions: list[Question] = field(default_factory=list)
    note: str = ""

    @property
    def needs_resume_file(self) -> bool:
        return any(q.kind == "input_file" for q in self.questions)

    @property
    def extra_questions(self) -> list[Question]:
        """Anything beyond the standard name/email/phone/resume block — these
        are the ones that actually cost you time, so they are worth seeing
        before you commit to opening the page."""
        standard = re.compile(
            r"^(first|last|full)?\s*name$|^email|^phone|^resume|^cv$|^cover letter",
            re.I)
        return [q for q in self.questions if not standard.match(q.label.strip())]


# job-boards.greenhouse.io/acme/jobs/12345  ->  ("acme", "12345")
_GH_URL = re.compile(
    r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)"
    r"(?:/jobs/|.*?gh_jid=)(\d+)", re.I)
_LEVER_URL = re.compile(r"jobs\.lever\.co/([\w-]+)/([\w-]+)", re.I)
_ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([\w-]+)/([\w-]+)", re.I)


def _greenhouse(client: httpx.Client, board: str, job_id: str) -> FormSpec:
    r = client.get(
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
        params={"questions": "true"},
    )
    r.raise_for_status()
    data = r.json()
    questions = []
    for q in data.get("questions") or []:
        fields = q.get("fields") or [{}]
        first = fields[0]
        questions.append(Question(
            label=q.get("label", "").strip(),
            required=bool(q.get("required")),
            kind=first.get("type", "input_text"),
            name=first.get("name", ""),
            options=[str(v.get("label", v)) for v in (first.get("values") or [])],
        ))
    return FormSpec(
        ats="greenhouse",
        apply_url=data.get("absolute_url") or
                  f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
        questions=questions,
    )


def describe(url: str, timeout: float = 15.0) -> FormSpec:
    """Best-effort description of the application form behind a posting URL.

    Only Greenhouse exposes its questions publicly. For everything else we
    return the standard set as an educated guess and say so, rather than
    pretending to knowledge we do not have.
    """
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        m = _GH_URL.search(url)
        if m:
            try:
                return _greenhouse(client, m.group(1), m.group(2))
            except Exception as e:
                log.debug("greenhouse questions for %s: %s", url, e)

    for pattern, ats in ((_LEVER_URL, "lever"), (_ASHBY_URL, "ashby")):
        if pattern.search(url):
            return FormSpec(
                ats=ats, apply_url=url, questions=_STANDARD,
                note=f"{ats} does not publish its questions; showing the usual set",
            )
    return FormSpec(ats="unknown", apply_url=url, questions=_STANDARD,
                    note="form not inspectable; showing the usual set")


_STANDARD = [
    Question("First Name", True, "input_text"),
    Question("Last Name", True, "input_text"),
    Question("Email", True, "input_text"),
    Question("Phone", True, "input_text"),
    Question("Resume/CV", True, "input_file"),
    Question("Cover Letter", False, "textarea"),
]
