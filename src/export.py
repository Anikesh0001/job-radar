"""File export: write the run's jobs to .xlsx, .csv or .txt, apply link included.

The format is chosen from the file extension, so `--export jobs.xlsx` and
`--export jobs.txt` both do the obvious thing. .xlsx needs openpyxl; if it is
missing we say so and fall back to a .csv beside it rather than dying after a
five-minute fetch.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

from .models import Job

log = logging.getLogger(__name__)

# Column order used by every format. Apply Link is the point of the exercise.
COLUMNS = [
    ("Company", lambda j: j.company),
    ("Title", lambda j: j.title),
    ("Location", lambda j: j.location or ""),
    ("Match", lambda j: "" if j.match_score is None else j.match_score),
    ("Salary", lambda j: j.salary or ""),
    ("Apply Link", lambda j: j.url),
    ("Source", lambda j: j.source),
    ("Posted", lambda j: (j.posted_at or "")[:10]),
    ("First Seen", lambda j: (j.first_seen or "")[:19].replace("T", " ")),
]

_MAX_CELL = 32000  # Excel's hard cap is 32767 characters per cell


def _row(job: Job) -> list[str]:
    return [str(fn(job) or "")[:_MAX_CELL] for _, fn in COLUMNS]


def _headers() -> list[str]:
    return [name for name, _ in COLUMNS]


def write_csv(jobs: list[Job], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel on Windows doesn't mangle non-ASCII city names.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_headers())
        for j in jobs:
            w.writerow(_row(j))
    return path


def write_xlsx(jobs: list[Job], path: str | Path) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        fallback = Path(path).with_suffix(".csv")
        log.warning(
            "openpyxl not installed (pip install openpyxl) — writing %s instead", fallback
        )
        return write_csv(jobs, fallback)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"

    headers = _headers()
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F4E79")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")

    link_col = headers.index("Apply Link") + 1
    for job in jobs:
        ws.append(_row(job))
        cell = ws.cell(row=ws.max_row, column=link_col)
        url = job.url or ""
        # Excel refuses hyperlinks longer than 255 chars; leave those as text.
        if url.startswith(("http://", "https://")) and len(url) <= 255:
            cell.hyperlink = url
            cell.value = "Apply"
            cell.font = Font(color="0563C1", underline="single")

    widths = {"Company": 22, "Title": 60, "Location": 34, "Match": 8, "Salary": 20,
              "Apply Link": 46, "Source": 16, "Posted": 12, "First Seen": 20}
    for i, name in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 18)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    wb.save(path)
    return path


def write_txt(jobs: list[Job], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"job-radar — {len(jobs)} opening(s) — {stamp}", "=" * 72, ""]
    for n, j in enumerate(jobs, 1):
        lines.append(f"{n}. {j.title}")
        lines.append(f"   Company : {j.company}")
        if j.location:
            lines.append(f"   Location: {j.location}")
        lines.append(f"   Apply   : {j.url}")
        lines.append(f"   Source  : {j.source}")
        if j.posted_at:
            lines.append(f"   Posted  : {j.posted_at[:10]}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


WRITERS = {".xlsx": write_xlsx, ".csv": write_csv, ".txt": write_txt}


def export(jobs: list[Job], path: str | Path) -> Path:
    """Write jobs to path, picking the format from its extension."""
    path = Path(path)
    writer = WRITERS.get(path.suffix.lower())
    if writer is None:
        raise ValueError(
            f"unsupported export format '{path.suffix}' — use one of {sorted(WRITERS)}"
        )
    out = writer(jobs, path)
    log.info("exported %d job(s) -> %s", len(jobs), out)
    return out
