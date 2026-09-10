"""Fill an application form in a real browser.

Why a browser and not an HTTP POST: there is no candidate-side apply API on
any board this project reads. Greenhouse and the rest authenticate submission
as the *employer* — `POST` to their endpoints returns 401 — so the only way in
is the page a human would use.

Three rules this module holds to, and they are the reason it is safe to run:

1. **It never clicks submit.** It fills the fields, attaches your resume, and
   leaves the browser open on the completed form. You read it and you submit.
   A wrong application cannot be recalled, and a form filled by a script is
   exactly the kind of thing that should get a human's eyes before it lands.
2. **It runs headed.** You watch it work. A headless robot filling forms you
   never see is how people end up applying to the same job nine times.
3. **One at a time.** No queue, no batch. Mass-applying is worse than useless:
   recruiters filter it, and it is how a boilerplate application gets your
   name remembered for the wrong reason.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Field label -> profile key. Matched case-insensitively against the label,
# the placeholder and the name attribute, because boards disagree about which
# of the three they populate.
FIELD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("first name", "firstname", "given name"), "first_name"),
    (("last name", "lastname", "surname", "family name"), "last_name"),
    (("full name", "your name"), "name"),
    (("email",), "email"),
    (("phone", "mobile", "contact number"), "phone"),
    (("linkedin",), "linkedin"),
    (("github",), "github"),
    (("website", "portfolio", "personal site"), "github"),
    (("location", "city", "where are you based"), "location"),
    (("notice period",), "notice_period"),
    (("current ctc", "current salary", "current compensation"), "current_ctc"),
    (("expected ctc", "expected salary", "salary expectation",
      "desired salary", "compensation expectation"), "expected_ctc"),
    (("how did you hear", "referral source"), "how_did_you_hear"),
    (("start date", "earliest start", "when can you start"), "earliest_start_date"),
]


def _values(profile, answers: dict) -> dict[str, str]:
    parts = (profile.name or "").split()
    return {
        "name": " ".join(w.capitalize() for w in parts),
        "first_name": parts[0].capitalize() if parts else "",
        "last_name": " ".join(w.capitalize() for w in parts[1:]) if len(parts) > 1 else "",
        "email": profile.email or "",
        "phone": profile.phone or "",
        "location": profile.location or "",
        "linkedin": (profile.links or {}).get("linkedin", "")
                    or answers.get("linkedin", ""),
        "github": (profile.links or {}).get("github", "")
                  or answers.get("github", ""),
        **{k: str(v) for k, v in answers.items() if v},
    }


def fill(url: str, profile, answers: dict, resume_path: str,
         cover_letter: str = "", timeout: int = 45_000) -> dict:
    """Open the application page and fill what it can. Never submits.

    Returns a report of what was filled and what was left for you.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "browser autofill needs playwright:\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    values = _values(profile, answers)
    filled: list[str] = []
    skipped: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)   # rule 2: you watch it
        page = browser.new_page()
        page.set_default_timeout(timeout)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)                    # let the SPA settle

        # Text inputs and textareas, matched on whatever identifying text the
        # board happens to provide.
        for element in page.query_selector_all(
            "input[type=text], input[type=email], input[type=tel], "
            "input:not([type]), textarea"
        ):
            try:
                if not element.is_visible() or element.input_value():
                    continue
                ident = " ".join(filter(None, [
                    element.get_attribute("name") or "",
                    element.get_attribute("id") or "",
                    element.get_attribute("placeholder") or "",
                    element.get_attribute("aria-label") or "",
                ])).lower()
                key = next((k for labels, k in FIELD_MAP
                            if any(lbl in ident for lbl in labels)), None)
                if key and values.get(key):
                    element.fill(values[key])
                    filled.append(f"{key} -> {values[key][:40]}")
                elif "cover" in ident and cover_letter:
                    element.fill(cover_letter)
                    filled.append("cover letter")
            except Exception as e:
                log.debug("field skipped: %s", e)

        # Resume upload.
        resume = Path(resume_path)
        for upload in page.query_selector_all("input[type=file]"):
            try:
                if resume.exists():
                    upload.set_input_files(str(resume))
                    filled.append(f"resume -> {resume.name}")
                    break
            except Exception as e:
                skipped.append(f"resume upload: {e}")

        # Anything required and still empty is yours to answer — usually the
        # dropdowns and the "why do you want to work here" boxes.
        for element in page.query_selector_all("[required], [aria-required=true]"):
            try:
                if element.is_visible() and not (element.input_value() or "").strip():
                    label = (element.get_attribute("aria-label")
                             or element.get_attribute("name") or "?")
                    skipped.append(label)
            except Exception:
                pass

        print("\n  Form filled. The browser is open and nothing has been submitted.")
        print("  Check every field, answer what is left, then submit it yourself.")
        input("  Press Enter here once you are done to close the browser... ")
        browser.close()

    return {"filled": filled, "left_for_you": sorted(set(skipped))}
