# Setting up the Telegram feed

End result: a Telegram group or channel where every new IT opening arrives as
its own message, with an Apply button, automatically, forever.

Follow the steps in order. Total time about ten minutes, most of it waiting for
BotFather to reply.

---

## Step 1 — Create the bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Give it a display name when asked — anything, e.g. `Job Radar`.
4. Give it a username. It **must** end in `bot`, e.g. `my_job_radar_bot`.
5. BotFather replies with a token that looks like:

   ```
   8123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

Copy it. That token is a password — anyone holding it controls the bot. Never
commit it.

---

## Step 2 — Decide: channel or group?

Both work. Pick one.

| | **Channel** (recommended) | **Group** |
|---|---|---|
| Who can post | only admins, so the feed stays clean | anyone, unless you restrict it |
| Members can chat | no | yes |
| Getting the chat id | easy — it's `@yourname` | needs one extra step |
| Best for | a job feed you read | a job feed you discuss |

A **channel** is the right default for this. Use a group only if you want
people talking under the postings.

---

## Step 3a — If you chose a channel

1. Telegram → **New Channel**. Name it, e.g. `IT Jobs Radar`.
2. Make it **Public** and pick a link, e.g. `t.me/my_it_jobs`.
3. Open the channel → **Administrators** → **Add Admin**.
4. Search your bot's username, add it, and make sure **Post Messages** is on.
5. Your chat id is the public link with an `@`:

   ```
   TELEGRAM_CHAT_ID=@my_it_jobs
   ```

If you want the channel **private**, you can't use `@name` — follow Step 3b to
get the numeric id instead.

---

## Step 3b — If you chose a group (or a private channel)

1. Create the group and add your bot to it as a **member**.
2. Promote the bot to **admin** (groups: not strictly required, but it stops
   Telegram's privacy mode from hiding things; private channels: required).
3. Send any message in the group, e.g. `hello`.
4. Open this URL in a browser, with your token pasted in:

   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

5. Find `"chat":{"id":-1001234567890,...}` in the JSON. That number, **minus
   sign and all**, is your chat id:

   ```
   TELEGRAM_CHAT_ID=-1001234567890
   ```

If `getUpdates` returns `{"ok":true,"result":[]}`, the bot has not seen a
message yet. Send another one in the group and reload. Group ids are negative
and supergroup ids start with `-100` — that is normal, keep the whole thing.

---

## Step 4 — Put the credentials in `.env`

```bash
cd job-radar
cp .env.example .env
```

Edit `.env`:

```ini
TELEGRAM_BOT_TOKEN=8123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=@my_it_jobs
```

`.env` is already in `.gitignore`. Keep it that way.

The project reads plain environment variables, so either export them or use a
loader:

```bash
set -a; source .env; set +a        # load .env into this shell
```

---

## Step 5 — Prove the bot can post

Before running the whole pipeline, confirm the two values work:

```bash
set -a; source .env; set +a
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
     -d "chat_id=$TELEGRAM_CHAT_ID" \
     -d "text=job-radar test"
```

`{"ok":true,...}` and a message appears → you're done here.

If not, read the `description` field — it is specific:

| Error | Meaning | Fix |
|---|---|---|
| `chat not found` | wrong id, or bot not in the chat | redo Step 3 |
| `bot is not a member of the channel chat` | bot added but not admin | make it an admin |
| `not enough rights to send text messages` | admin without post rights | enable **Post Messages** |
| `Unauthorized` | token is wrong | re-copy from BotFather |

---

## Step 6 — Baseline the queue (do not skip this)

The database already holds every posting collected so far — **over 12,000**.
One message per posting, at Telegram's rate limit, is about ten hours of
solid notifications.

Almost certainly you want alerts for what turns up *from now on*:

```bash
python -m src.run --mark-all-notified
```

```
marked 12347 stored posting(s) as already delivered
alerts will now cover only postings found from here on
```

Nothing is deleted — all 12,347 stay in `jobs.xlsx` and the database. You are
only saying "don't announce these".

Skip this step **only** if you genuinely want the whole back catalogue pushed
to the channel over the next several days.

---

## Step 7 — First real run

```bash
set -a; source .env; set +a
python -m src.run
```

You'll see the fetch, then:

```
telegram: posting 12 of 12 queued (one message each, ~3.5s apart)
  sent 1/12   Swiggy — SDE-1, Backend
  sent 2/12   Razorpay — Software Engineer II
  ...
telegram: 12/12 delivered
```

Each posting arrives as its own message:

```
Software Development Engineer I
🏢 Swiggy
📍 Bangalore, India
🗓 2026-09-08
🔗 Apply
via smartrecruiters
              [ Apply ↗ ]
```

To watch it work without sending anything, add `--dry-run`.

---

## Step 8 — Put it on a schedule

### Option A — GitHub Actions (free, nothing running at home) — recommended

**See [GITHUB.md](GITHUB.md) for the full walkthrough.** Short version: push to
a public repo, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Actions
secrets, and `.github/workflows/fetch.yml` runs every four hours by itself.

Two traps GITHUB.md covers that will otherwise kill the feed quietly: GitHub
auto-disables scheduled workflows after 60 days of repo inactivity, and
committing the database instead of storing it as a release asset grows the repo
by ~5.6GB a year.

Deploying this way you do **not** need Step 6 below: the workflow posts its
normal quota on the first run and then clears the ~12,000-job back catalogue
for you automatically.

### Option B — cron on this machine

```bash
crontab -e
```

```cron
0 */3 * * * cd /home/t-201/Downloads/files/job-radar && set -a && . ./.env && set +a && .venv/bin/python -m src.run >> /tmp/job-radar.log 2>&1
```

Cron gets almost no environment, so the absolute paths and the `.env` sourcing
are both load-bearing. Check it with `tail -f /tmp/job-radar.log`.

---

## How the pacing works

Telegram allows roughly **20 messages per minute** to one chat. One posting per
message means that limit — not how fast the sources are fetched — sets the pace.

- Messages go out **3.5 seconds apart**.
- Each run posts at most **40** (`--notify-limit N` to change it).
- Anything over the limit stays queued and goes out next run. The queue lives
  in the database, so it survives restarts, crashes and reboots.
- Each posting is marked delivered **the moment it sends**, so an interrupted
  run never re-posts what already went out.
- A `429` is retried after exactly the delay Telegram asks for. A `400` (bad
  URL, malformed text) is dropped so one bad posting can't block the queue.

A typical run finds 10-50 new postings, so the queue normally empties every
time. After a long gap it drains over a few runs.

```bash
python -m src.run --notify-limit 100     # push harder after a backlog
python -m src.run --notify-limit 0       # fetch and store, post nothing
```

---

## Everyday commands

```bash
python -m src.run                      # fetch, store, post, rewrite the sheet
python -m src.run --dry-run            # everything except sending
python -m src.run --health             # sources that have gone quiet
python -m src.run --mark-all-notified  # clear the alert backlog
```

---

## Troubleshooting

**Nothing is posted, log says `TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set`**
The variables aren't in the environment. `.env` is a file, not magic —
`set -a; source .env; set +a` first, or set them in the systemd/cron/Actions
environment.

**`chat not found`**
The bot isn't in that chat, or the id is wrong. For a public channel use
`@name`; for a group use the full negative number including `-100`.

**Messages stop partway through a run**
You hit the rate limit. The log will say `telegram rate limited, sleeping Ns`.
It recovers on its own; lower `--notify-limit` if it happens every run.

**Thousands of messages started arriving**
You skipped Step 6. Stop the run, then:
```bash
python -m src.run --mark-all-notified
```

**Duplicate postings in the channel**
Shouldn't happen — postings are marked delivered as they send. If it does,
check you aren't running two copies at once (cron *and* GitHub Actions both
firing means two independent databases, each with its own queue).

**Some postings never appear**
They were filtered out, not lost. `it_only` keeps IT roles only; check with:
```bash
python -m src.run --dry-run --no-filter --export /tmp/everything.xlsx
```

---

## Want a second channel with different filters?

Run the same code against a separate config and database, so the two keep
independent alert queues:

```bash
python -m src.run --config config/interns.yaml --db interns.db --export interns.xlsx
```

Put `entry_level_only: true` and an India-only `locations:` list in that config,
and export `TELEGRAM_CHAT_ID` to the other channel before running it.
