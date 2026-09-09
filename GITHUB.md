# Putting this on GitHub and walking away

End state: a public Telegram channel that posts IT jobs by itself, forever, on
GitHub's free tier. Nothing running on your laptop. Nothing to maintain.

Do [TELEGRAM.md](TELEGRAM.md) first — you need the bot token and chat id before
any of this is useful.

**Time: about fifteen minutes.** Then you are done.

---

## Step 1 — Check nothing secret is about to be published

The repo is going to be public. Run this from the project folder:

```bash
ls -la .env                    # should say "No such file or directory"
grep -rn "TELEGRAM_BOT_TOKEN=" --include="*.py" --include="*.yml" .
```

The only hits should be in `.env.example`, and it must contain placeholders
rather than your real token. `.env` itself is in `.gitignore` and stays on your
machine.

Your token lives in **GitHub Secrets** (Step 4), never in a file.

---

## Step 2 — Create the repository

On [github.com/new](https://github.com/new):

- **Name:** `job-radar`
- **Visibility: Public** — this matters. Private repos get 2,000 Actions
  minutes a month; a run of this takes ~5 minutes, six times a day, which is
  ~900 minutes a month. You would run out. Public repos get **unlimited**
  minutes.
- Do **not** add a README, .gitignore or licence — the repo already has them.

Nothing sensitive is published: the repo holds code only. The database and
spreadsheet are kept as release assets, and your token is a secret.

---

## Step 3 — Push

```bash
cd path/to/job-radar

git init -b main
git add .
git commit -m "job-radar: IT job feed across 209 sources"
git remote add origin https://github.com/Anikesh0001/job-radar.git
git push -u origin main
```

Check what you actually committed before pushing:

```bash
git status --short
```

You should see source files only — no `.env`, no `jobs.db`, no `jobs.xlsx`.
Those are gitignored on purpose (see Step 7).

---

## Step 4 — Add the secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Name | Value | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather | **yes** |
| `TELEGRAM_CHAT_ID` | `@yourchannel` or `-100...` | **yes** |
| `GH_PAT` | see Step 5 | strongly recommended |
| `TELEGRAM_ALERT_CHAT_ID` | where to report failures — see below | recommended |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | free key from [developer.adzuna.com](https://developer.adzuna.com/) | optional |

**About `TELEGRAM_ALERT_CHAT_ID`:** an unattended feed that breaks looks exactly
like an unattended feed with nothing to say. GitHub emails you on the *first*
failure of a scheduled workflow and then goes quiet, so the workflows also
report failures over Telegram. Point this at a **private** chat — message
[@userinfobot](https://t.me/userinfobot) to get your own numeric id — so
operational noise never lands in the public job channel. Leave it unset and the
alert step skips itself rather than posting errors to your subscribers.

Secrets are write-only. GitHub will never show them again, and they are masked
in logs.

---

## Step 5 — The 60-day problem (do not skip)

**GitHub disables scheduled workflows in a public repository after 60 days
with no repository activity.** Commits made by the built-in `GITHUB_TOKEN` do
not count as activity. So a feed that works perfectly will simply stop after
two months, with no error and no email.

This is the single most likely reason a "set and forget" GitHub cron dies.

The fix is a personal access token, which makes the weekly keepalive commit
count as real activity:

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
   (Fine-grained tokens)
2. **Repository access:** Only select repositories → `job-radar`
3. **Permissions:** Repository permissions → **Contents: Read and write**
4. **Expiration:** GitHub caps fine-grained tokens at 1 year. Set a calendar
   reminder — this is the one piece of genuine maintenance in the whole setup.
5. Save it as the `GH_PAT` secret.

Without `GH_PAT` everything still works, the keepalive job just skips itself
and you must visit and re-enable the workflow roughly every two months.

---

## Step 6 — Start it

**Actions** tab → **fetch jobs** → **Run workflow** → **Run workflow**.

If Actions shows *"Workflows aren't being run on this forked repository"* or a
green **I understand my workflows, go ahead and enable them** button, click it.

The first run takes about 5 minutes:

```
no state release yet — first run, starting from empty
fetched 29258 raw postings from 209 targets
14842 postings passed filters
12468 new after dedupe (12468 total in db)
telegram: posting 40 of 12468 queued (one message each, ~3.5s apart)
  sent 1/40   Swiggy — SDE-1, Backend
  ...
marked 12428 stored posting(s) as already delivered
```

**You get 40 jobs immediately**, then the remaining ~12,000 back catalogue is
dropped from the queue. That is deliberate: dripping it out would take seven
weeks and bury every genuinely new opening behind it. From run two onwards the
channel carries whatever is actually new, typically 20-120 per run.

Want the back catalogue after all? See "Choosing how much to post" below.

---

## Step 7 — Confirm it worked

Three things should now be true:

1. **Actions tab** — a green tick on *fetch jobs*.
2. **Releases** — a release tagged `data` with `jobs.db.gz` and `jobs.xlsx`
   attached. That is where all state lives.
3. **Second run** (or trigger one manually) — postings appear in your channel:

```
telegram: posting 40 of 121 queued (one message each, ~3.5s apart)
  sent 1/40   Swiggy — SDE-1, Backend
  ...
telegram: 81 still queued, will go out next run
```

Then close the tab. That is the whole job.

---

## What happens from here, without you

There are **two** workflows, and the split is the point:

| Workflow | Runs | What it does |
|---|---|---|
| **fetch jobs** | every 4 hours | Polls 209 sources, filters, dedupes, stores, refreshes the spreadsheet, posts its share |
| **post jobs** | every hour | Posts ~20 more from the queue. Fetches nothing. |

Postings are found far faster than Telegram will announce them — roughly 1,500
a day discovered against ~20 a minute deliverable — so there is always a
backlog. Fetching hourly would hammer 209 public APIs for no benefit; posting
hourly from what is already stored costs nothing and is what turns six bursts a
day into a steady feed. A `post jobs` run takes about a minute and makes no
request to any job board.

Both share a `job-radar-state` concurrency group, so they queue behind each
other rather than clobbering the same database.

| When | What |
|---|---|
| Every hour | ~20 postings to Telegram, newest first, sources interleaved |
| Every 4 hours | Full fetch across 209 sources; spreadsheet refreshed |
| Every run | State saved back to the `data` release |
| Every run | Queued postings older than 14 days dropped rather than posted stale |
| Every fetch | Postings unseen for 60 days deleted, so the database stays flat |
| Weekly | One keepalive commit, so the schedule is never auto-disabled |
| Yearly | **You** renew `GH_PAT` |

### Why each run posts a different number

GitHub's scheduler is best-effort, and on a low-activity public repo it is
worse than that: measured over five hours against an hourly cron it ran **3 of
6 slots**, with gaps of 2.6 to 5 hours. Nothing is misconfigured — this is
simply what free scheduled Actions do, and it cannot be fixed from inside the
repo.

So the batch size is not fixed. Each run checks how long it has actually been
since the last delivery and sends `rate × hours`, capped at 60:

```
1.0h since last post  → 15 postings
2.6h                  → 39
5.0h                  → 60 (capped)
```

The daily volume therefore stays near the target of ~15/hour whether GitHub
fires six times or twenty. The cap exists so a twelve-hour outage catches up
over a few runs instead of dumping 180 messages at once.

`--notify-rate N` changes the target; `--notify-limit N` overrides it with a
flat number.

Nothing runs on your machine. Your laptop can be off, asleep, or in another
country — this all executes on GitHub's runners.

The spreadsheet is always downloadable at a stable URL:

```
https://github.com/Anikesh0001/job-radar/releases/download/data/jobs.xlsx
```

### Why state is in a release, not committed

The obvious design — commit `jobs.db` every run — was measured and rejected:

| | Per commit | Per year |
|---|---|---|
| Raw `jobs.db` | 65 MB | *unusable within a week* |
| Descriptions dropped | 7.7 MB | 16 GB |
| Gzipped | 1.7 MB | 5.6 GB |
| **Release asset** | **0** | **0** |

Git keeps every version of a binary forever, and a SQLite file changes
throughout on every write, so nothing deltas. Release assets are replaced in
place and never enter git history. The repo stays a few hundred KB of code no
matter how long it runs.

---

## Choosing how much to post

The channel posts **~20 jobs an hour, around the clock** — roughly 480 a day,
paced 3.5 seconds apart to stay inside Telegram's ~20-messages-per-minute
limit. Anything over the limit stays queued in the database and goes out next
hour, so nothing is lost and nothing floods.

To change the rate, edit the `--notify-limit` in
`.github/workflows/post.yml`, or its `cron` for a different interval:

```yaml
- cron: "30 * * * *"                          # hourly → "30 */2 * * *" for less
run: python -m src.run --post-only --notify-limit 20
```

**Default: only postings found from now on.** A typical run turns up 20-120 new
jobs, so the channel stays busy on its own.

**To also drip out the ~12,000 already collected:** Actions → *fetch jobs* →
*Run workflow* → tick **Re-queue every stored posting**. They then flow out at
240/day alongside new finds, taking about seven weeks. Good if you want a
lively channel immediately; skip it if you would rather only see fresh roles.

To change the pace, edit `.github/workflows/fetch.yml`:

```yaml
- cron: "0 */4 * * *"      # every 4 hours → "0 */6 * * *" for less
```

```yaml
run: python -m src.run --export jobs.xlsx --notify-limit 25
```

---

## Monitoring

You will not need to look at this often, but when you do:

**Is it running?** The badges at the top of the README, or the Actions tab.
Every run also writes a per-source table to its GitHub summary page, so you can
see what each source contributed without opening a log.

**Is it broken?** You get a Telegram message, if `TELEGRAM_ALERT_CHAT_ID` is
set. Every push also runs the test suite and validates both configs, so a
change that would break the feed fails before it ships rather than after.

**Get told when it breaks:** GitHub emails you on the first failure of a
scheduled workflow by default. Confirm at
[github.com/settings/notifications](https://github.com/settings/notifications)
→ Actions → *Notify on: failed workflows only*.

**Are the sources still alive?** Open any run's log — every run prints a table:

```
source                       fetched      kept
greenhouse                      8906      4513
workday                         5964      2600
instahyre                       1400      1214
```

A source that drops to zero and stays there has moved or died. Locally:

```bash
python -m src.run --health          # empty for 3+ consecutive runs
python verify_slugs.py              # probe every slug now
```

---

## Troubleshooting

**Workflow ran green but nothing was posted**
Expected on run one — it baselines. Check run two. If that is also silent,
open the log: `TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set` means the
secrets are missing or misspelled (they are case-sensitive).

**`chat not found` in the log**
The bot is not in the channel, or the id is wrong. Redo Step 3 of
[TELEGRAM.md](TELEGRAM.md). A public channel is `@name`; a group is the full
negative number including `-100`.

**Everything stopped after about two months**
The 60-day disable. Add `GH_PAT` (Step 5), then Actions → *fetch jobs* →
**Enable workflow**.

**`Resource not accessible by integration`**
Settings → Actions → General → Workflow permissions → **Read and write
permissions**.

**Jobs posted twice**
Two runs raced. The workflow has a `concurrency` group to prevent this, so the
usual cause is also running it on your laptop against a different database.
Pick one.

**Actions tab says the workflow was disabled**
Either the 60-day rule, or GitHub disabled it after repeated failures. Fix the
failure, then re-enable it with the button.

**A run failed with a source timeout**
Normal and harmless. One dead source cannot fail a run — the adapter logs a
warning and moves on. If the *whole run* times out at 45 minutes, a site is
hanging; raise `timeout-minutes` or drop the slow source.

---

## Making it yours

Before you tell anyone about the channel:

- **`src/sources.py`** and **`discover.py`** — the `UA` string already points
  at this repo. It is the courtesy that keeps these public endpoints open;
  leave a working URL in it if you fork or rename.
- **`config/sources.yaml`** — the filters at the bottom decide what the channel
  is. Uncomment the `locations:` block for India-only; set
  `entry_level_only: true` for a freshers channel.
- **`config/companies.txt`** — add companies, then
  `python discover.py --file config/companies.txt` to find their boards.

## If you want two channels

Different filters, different channel, one repo. Copy the config, and give the
second one its own database so the alert queues stay independent:

```bash
python -m src.run --config config/interns.yaml --db interns.db --export interns.xlsx
```

In the workflow, add a second job with its own `TELEGRAM_CHAT_ID` secret and a
separate state release tag.
