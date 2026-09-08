"""Delivery. Telegram is the primary channel; Discord is a drop-in alternative.

Deliberately no WhatsApp: the official Cloud API is paid and needs business
verification, and the unofficial libraries that drive WhatsApp Web get numbers
banned. Post to Telegram and let people forward.
"""

from __future__ import annotations

import html
import logging
import os
import time

import httpx

from .models import Job

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram's documented ceiling is ~20 messages per minute to one group,
# channel or supergroup (the 30/second figure is the global one, across all
# chats). One posting per message means that limit, not our fetch speed, is
# what paces the feed — so leave a real gap rather than discovering the cap
# through 429s.
SECONDS_BETWEEN_MESSAGES = 3.5

# How many postings to send in a single run, unless overridden. At 3.5s each,
# 40 messages is a shade over two minutes.
DEFAULT_PER_RUN = 40


def _fmt(job: Job) -> str:
    """One posting, one message. HTML parse mode."""
    title = html.escape(job.title[:200])
    company = html.escape(job.company[:80])
    lines = [f"<b>{title}</b>", f"🏢 {company}"]
    if job.location:
        lines.append(f"📍 {html.escape(job.location[:100])}")
    if job.posted_at:
        lines.append(f"🗓 {html.escape(str(job.posted_at)[:10])}")
    lines.append(f"🔗 <a href=\"{html.escape(job.url)}\">Apply</a>")
    lines.append(f"<i>via {html.escape(job.source)}</i>")
    return "\n".join(lines)


def _keyboard(job: Job) -> dict | None:
    """A tappable Apply button. Telegram rejects a URL button over 64 bytes of
    callback data, but plain url buttons are fine up to the URL length limit;
    anything malformed just means no button rather than a failed send."""
    if not job.url.startswith(("http://", "https://")) or len(job.url) > 2000:
        return None
    return {"inline_keyboard": [[{"text": "Apply ↗", "url": job.url}]]}


def send_one(client: httpx.Client, token: str, chat_id: str, job: Job) -> bool:
    """Post a single job. Returns True only if Telegram accepted it."""
    payload = {
        "chat_id": chat_id,
        "text": _fmt(job),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    kb = _keyboard(job)
    if kb:
        payload["reply_markup"] = kb

    for attempt in range(3):
        try:
            r = client.post(TG_API.format(token=token, method="sendMessage"), json=payload)
        except httpx.TransportError as e:
            log.warning("telegram transport error: %s", e)
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code == 200:
            return True

        # 429 carries the exact wait in parameters.retry_after. Obey it — the
        # alternative is a widening ban on the chat.
        if r.status_code == 429:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            wait = (body.get("parameters") or {}).get("retry_after", 30)
            log.warning("telegram rate limited, sleeping %ss", wait)
            time.sleep(min(float(wait) + 1, 120))
            continue

        # 400s are this message's fault (bad HTML, dead URL); retrying is
        # pointless and would stall the whole queue behind one poison entry.
        if 400 <= r.status_code < 500:
            log.error("telegram rejected %s: %s", job.title[:50], r.text[:200])
            return False

        time.sleep(2 * (attempt + 1))
    return False


def send_telegram(
    jobs: list[Job],
    token: str | None = None,
    chat_id: str | None = None,
    on_sent=None,
    delay: float = SECONDS_BETWEEN_MESSAGES,
) -> list[Job]:
    """Post each job as its own message. Returns the ones Telegram accepted.

    on_sent is called after every successful send, so the caller can mark that
    posting delivered immediately. Doing it per message rather than in one
    batch at the end matters: a run interrupted halfway through a queue of 40
    would otherwise re-send everything it had already posted.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping Telegram")
        return []
    if not jobs:
        return []

    sent: list[Job] = []
    with httpx.Client(timeout=30) as client:
        for i, job in enumerate(jobs, 1):
            if send_one(client, token, chat_id, job):
                sent.append(job)
                if on_sent:
                    on_sent(job)
                log.info("  sent %d/%d  %s — %s", i, len(jobs), job.company[:24],
                         job.title[:44])
            if i < len(jobs):
                time.sleep(delay)
    log.info("telegram: %d/%d delivered", len(sent), len(jobs))
    return sent


def send_discord(jobs: list[Job], webhook: str | None = None) -> bool:
    webhook = webhook or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook or not jobs:
        return False
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(jobs), BATCH):
            chunk = jobs[i : i + BATCH]
            lines = [f"**{j.company}** — [{j.title}]({j.url})" for j in chunk]
            r = client.post(webhook, json={"content": "\n".join(lines)[:1900]})
            if r.status_code >= 300:
                log.error("discord %s: %s", r.status_code, r.text[:200])
            time.sleep(1)
    return True


def write_markdown(jobs: list[Job], path: str = "latest.md") -> None:
    """Human-readable digest committed alongside the database."""
    lines = ["# Latest openings", ""]
    for j in jobs:
        loc = f" — {j.location}" if j.location else ""
        lines.append(f"- **{j.company}**: [{j.title}]({j.url}){loc}  \n  `{j.source}`")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
