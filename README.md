# job-radar

[![tests](https://github.com/Anikesh0001/job-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/Anikesh0001/job-radar/actions/workflows/ci.yml)
[![fetch jobs](https://github.com/Anikesh0001/job-radar/actions/workflows/fetch.yml/badge.svg)](https://github.com/Anikesh0001/job-radar/actions/workflows/fetch.yml)
[![hourly jobs](https://github.com/Anikesh0001/job-radar/actions/workflows/post.yml/badge.svg)](https://github.com/Anikesh0001/job-radar/actions/workflows/post.yml)

Automated IT job alerts, built entirely on free infrastructure. Polls ~200
public ATS boards, aggregator feeds and search APIs on a schedule,
deduplicates across all of them, filters to tech roles, and pushes new
openings to Telegram plus a spreadsheet.

Total running cost: zero. No VPS, no paid API, no database bill.

## Why this beats the WhatsApp groups

Those groups are mostly people re-forwarding each other, with a Google Form
link on top. This polls the companies directly, so you see a posting when it
goes live rather than when somebody gets round to sharing it.

## How it works

```
discover.py             probe a company name against every ATS
        │  (writes verified slugs)
        ▼
config/sources.yaml     which companies, on which ATS, with what filters
        │
        ▼
src/sources.py          29 adapters → normalised Job objects
        │
        ▼
src/filters.py          IT role? right level? right city? experience cap?
        │
        ▼
src/db.py               SQLite: exact fingerprint + fuzzy near-dup check,
                        plus per-source health so dead slugs surface
        │
        ▼
src/notify.py           Telegram / Discord / latest.md
        │
        ▼
src/export.py           jobs.xlsx / .csv / .txt, with apply links
```

The whole thing runs inside a GitHub Actions cron job every three hours and
commits `jobs.db` back to the repo, so the repo itself is your database.

## Setup

```bash
git clone https://github.com/Anikesh0001/job-radar && cd job-radar
pip install -r requirements.txt

# See what it finds without sending anything
python -m src.run --dry-run
```

Useful from day one:

```bash
python -m src.run --health                 # which sources have gone quiet
python -m src.run --export-new-only        # just today's new, as a digest
python discover.py --names "Acme,Beta"     # find a company's ATS
```

### Telegram alerts

Every new opening arrives in your channel or group as **its own message**, with
an Apply button:

```
Software Development Engineer I
🏢 Swiggy
📍 Bangalore, India
🗓 2026-09-08
🔗 Apply
via smartrecruiters
              [ Apply ↗ ]
```

**Full walkthrough: [TELEGRAM.md](TELEGRAM.md)** — bot creation, channel vs
group, finding the chat id, scheduling, and the error messages you will
actually hit. The short version:

1. `/newbot` to [@BotFather](https://t.me/BotFather), copy the token.
2. Create a channel, add the bot as an administrator with **Post Messages**.
3. `cp .env.example .env`, fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **`python -m src.run --mark-all-notified`** — baseline the queue first.
5. `set -a; source .env; set +a && python -m src.run`

Step 4 is not optional in practice. The database already holds 12,000+
postings, all of them undelivered; one message each is about ten hours of
notifications. Baselining says "start alerting from here", and deletes nothing.

Posting is paced for Telegram's ~20 messages/minute-per-chat limit: 3.5s apart,
40 per run by default (`--notify-limit N`), with the remainder queued **in the
database** so it survives a restart. Each posting is marked delivered the
moment it sends, so an interrupted run never re-posts.

### Running it unattended

**Full walkthrough: [GITHUB.md](GITHUB.md).** Push to a public repo, add two
secrets, and GitHub Actions runs it every four hours for free — no VPS, nothing
on your laptop.

Two things in there are easy to get wrong and both kill the feed silently:

- **GitHub disables scheduled workflows in a public repo after 60 days with no
  repository activity**, and commits by the built-in `GITHUB_TOKEN` do not
  count. A perfectly working feed stops after two months. The workflow makes a
  weekly keepalive commit with a personal access token to prevent it.
- **Do not commit `jobs.db`.** Git keeps every version of a binary for ever and
  a SQLite file changes throughout on every write, so nothing deltas: 5.6GB of
  history a year. The workflow keeps the database and spreadsheet as assets on
  a `data` release instead, which git history never sees.

## Sources

29 adapters. Everything below was probed live — nothing is listed on the
strength of documentation alone.

**Per-company ATS boards** — one slug each, no auth:

| Adapter | Endpoint |
|---|---|
| `greenhouse` | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` |
| `lever` | `api.lever.co/v0/postings/{slug}` |
| `ashby` | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| `smartrecruiters` | `api.smartrecruiters.com/v1/companies/{slug}/postings` |
| `workable` | `apply.workable.com/api/v1/widget/accounts/{slug}` |
| `recruitee` | `{slug}.recruitee.com/api/offers/` |
| `bamboohr` | `{slug}.bamboohr.com/careers/list` |
| `breezy` | `{slug}.breezy.hr/json` |
| `teamtailor` | `{slug}.teamtailor.com/jobs.json` |
| `personio` | `{slug}.jobs.personio.de/xml` |
| `rippling` | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` |
| `workday` | `{tenant}.wdN.myworkdayjobs.com/...` — slug is `tenant\|wdN\|SiteName` |
| `oraclecloud` | Oracle ORC — slug is `host\|siteNumber` |
| `phenom` | `{careers-host}/api/jobs` |

**Broad search** — no slug, reaches employers you never listed:

| Adapter | What it is | Auth |
|---|---|---|
| `smartrecruiters_search` | SmartRecruiters' cross-company search | none |
| `instahyre` | India-only, almost entirely IT, ~13k live postings | none |
| `adzuna` | aggregator covering Naukri/Indeed-class inventory | free key |

**Aggregator feeds** — `remoteok`, `arbeitnow`, `remotive`, `jobicy`,
`himalayas`, `workingnomads`, `themuse`, `landingjobs`, `hackernews`.

**Generic crawlers** — reach any site without new code:

| Adapter | Use for |
|---|---|
| `rss` | any job board publishing RSS/Atom. One line of config per feed. |
| `jsonld` | careers pages carrying `schema.org/JobPosting` markup |
| `sitemap` | bounded sitemap walk, then JSON-LD on each job page |

The `jsonld` adapter is the important one for India. Oracle Cloud, Trakstar,
Darwinbox, Keka and Zoho Recruit have no clean public API, but Google requires
`schema.org/JobPosting` markup for a job to appear in Google Jobs, so most of
their career pages carry it. One parser reaches hundreds of sites.

### Things that look like sources and are not

Worth writing down, because each of these costs an afternoon to rediscover:

- **SmartRecruiters' global search ignores paging.** `offset`, `page`, `limit`
  and `country` are all accepted and all silently ignored; only `keyword`
  works, and every response is capped at 100. Breadth comes from running many
  keywords, not from walking pages.
- **Instahyre caps a page at 35** however large a `limit` you send, but deep
  offsets do work, so it pages fine.
- **Most Phenom tenants** answer `/api/jobs` with the HTML shell and HTTP 200.
  Check the content type, not the status.
- **BambooHR does the same** for a slug that no longer exists.
- **Workday needs all three** of tenant, `wdN` and site name — 12 of 53
  plausible guesses were live. There is no cheap way to enumerate them.
- **Jobspresso's RSS feed is empty** and **RemoteOK's is HTTP 410**. Both are
  still widely recommended.
- Jooble sits behind Cloudflare; SuccessFactors, Findwork and YC's Work at a
  Startup all need credentials.

### Scraped sources (`jobspy`) — opt-in

Everything above is a public API. `jobspy` is the exception: it scrapes, and
it is off by default because the trade-offs are real. Scraping LinkedIn
breaches their terms, the sites rate-limit, and a scraper breaks whenever a
page changes. Treat it as a supplement, never the backbone.

```bash
pip install -r requirements-optional.txt   # pandas + numpy, ~50MB
```

Until it is installed the source skips itself and says so in `--health`, so
the config lines cost nothing.

**It does not get you Naukri.** That is the reason most people reach for it,
so it is worth being specific. Measured from a clean machine:

| Back-end | Result |
|---|---|
| `indeed` | ✅ 100 jobs in 3s — fast, excellent India coverage |
| `linkedin` | ✅ 100 jobs in 56s — works, slow, rate-limits at volume |
| `naukri` | ❌ HTTP 406, `recaptcha required` |
| `glassdoor` | ❌ HTTP 400, location not parsed |
| `zip_recruiter` | ❌ HTTP 403 |
| `bayt` | ❌ HTTP 403 |
| `google` | ❌ returns nothing |

So `indeed` is the one worth enabling, `linkedin` is worth a small quota, and
the other five are dead weight — the adapter refuses them rather than making
you discover this yourself.

For Naukri-class inventory the working route is still `adzuna`: it indexes
much of the same market under a licence, and a free key takes a minute.

Config format is `site|search term|location|count`:

```yaml
  jobspy:
    - indeed|software engineer|India|200
    - linkedin|data scientist|India|50
```

Delete the `jobspy:` block to turn it off entirely.

The scheduled workflow deliberately does **not** install it. Scrapers run from
GitHub Actions IPs get blocked far faster than from a home connection — the
same reason Glassdoor and ZipRecruiter already answer 403 here — and LinkedIn
at 56 seconds per 100 results would dominate the run. Run `jobspy` locally
when you want the extra coverage; let the cron job stick to the public APIs.

#### A note on `pip install` failing

On Debian/Ubuntu, `pip install python-jobspy` outside a virtualenv fails with
`error: externally-managed-environment` (PEP 668). That is the OS protecting
its own Python, not a problem with the package. Use the project's venv:

```bash
.venv/bin/pip install -r requirements-optional.txt
# or activate it first:  source .venv/bin/activate
```

## Growing the slug list

This is the actual work, and the part nobody else does. The shipped config
carries ~200 targets, every one of them verified live.

Do not hand-add slugs. The previous version of this config shipped
`smartrecruiters: Visa` and `smartrecruiters: Bosch`; both return zero
postings, because the live Bosch slug is `BoschGroup`. Guessing has roughly a
70% miss rate, and a wrong slug is indistinguishable from a company that
simply is not hiring.

Use the discovery tool instead. Give it company *names* and it derives slug
variants, probes every ATS concurrently, and prints only the boards that
actually returned postings:

```bash
python discover.py --names "Razorpay,Zerodha,Postman"
python discover.py --file config/companies.txt --out found.yaml
```

`config/companies.txt` is a seed list of ~315 Indian and global tech
companies; it found 160 live boards across 8 platforms. Expect a 15-30% hit
rate — most large employers are on Workday, Taleo or SuccessFactors, which
need more than a slug.

For a company the tool misses, open its careers page and read the URL:

| Careers URL | Config entry |
|---|---|
| `job-boards.greenhouse.io/stripe` | `greenhouse: [stripe]` |
| `jobs.lever.co/palantir` | `lever: [palantir]` |
| `jobs.ashbyhq.com/openai` | `ashby: [openai]` |
| `acme.recruitee.com` | `recruitee: [acme]` |
| `acme.wd5.myworkdayjobs.com/en-US/Careers` | `workday: [acme\|wd5\|Careers]` |
| any RSS feed | `rss: [<feed url>]` |
| anything else | `jsonld: [<the listing page URL>]` |

### Keeping it alive

Companies migrate between ATS vendors constantly, so a chunk of any list is
dead at any moment. Three tools, in increasing order of laziness:

```bash
python -m src.run --health       # what has been empty for 3+ consecutive runs
python verify_slugs.py           # probe everything now, report live vs dead
python verify_slugs.py --prune   # rewrite the config without the dead ones
```

`--health` is the one to actually use. Every run records a per-target result
in the `source_health` table, so a board that quietly starts returning zero —
the normal failure mode — shows up as a rising `zero_streak` instead of
hiding among the sources that genuinely had nothing new.

It distinguishes two things that otherwise look identical:

```
adzuna      in|8         1  skipped: ADZUNA_APP_ID / ADZUNA_APP_KEY not set
greenhouse  acmecorp     7  never returned anything
```

The first is a key-gated source doing exactly what it should; the second is a
dead slug. `verify_slugs.py --prune` will never delete the former. And the
report only lists targets still present in your config, so deleting a slug
actually makes it go away instead of leaving a permanent entry you learn to
scroll past.

Note that `--prune` rewrites `config/sources.yaml` through a YAML dumper and
will strip the comments out of it. Prefer deleting the reported lines by hand.

## Tuning what you see

Everything is in the `filters` block of `config/sources.yaml`. The shipped
defaults are set for **maximum coverage** — every IT role the radar can reach,
at every level, in every location:

```yaml
filters:
  it_only: true             # keep software/data/infra/QA/security/PM/UX roles
  entry_level_only: false   # every level, not just internships
  max_years_experience: 99  # no cap
  locations: []             # everywhere
```

- `it_only` — the important one. The broad sources are not tech-specific:
  Bosch's board alone is ~1,000 postings that are mostly factory roles, and
  SmartRecruiters' global search happily returns staff nurses. This matches
  against the **title only**, so a marketing job whose description mentions
  "our engineering team" is still a marketing job. Turn it off and the sheet
  fills with forklift drivers.
- `entry_level_only` — set `true` to see only intern / fresher / new-grad /
  SDE-1 titles, and reject senior / staff / lead / manager titles even if they
  also matched.
- `max_years_experience` — drop postings whose body demands more than this.
- `locations` / `countries` / `allow_remote` — the geography policy. As
  shipped it is **India, plus remote from anywhere**, which on a live database
  removed 58% of postings: San Francisco, Paris and Dublin onsite roles that
  nobody reading this channel can take. Empty both lists to accept everywhere.
  - `countries: [in]` matches a trailing `", IN"`. Indeed and several ATSes
    write `Bengaluru, KA, IN` and never the word "India" — without this a third
    of the India inventory is silently dropped.
  - `allow_remote: true` lets a remote role through wherever the employer is.
  - `unknown_location: drop` discards postings with no readable location,
    including Workday's `"2 Locations"` which never says which. Set it to
    `keep` for the old permissive behaviour.
- `keywords`, `block_companies` — optional extra narrowing.

The title patterns live in `src/filters.py` as four regexes: `IT_TITLE` and
`NOT_IT` decide what counts as a tech role, `INCLUDE` and `EXCLUDE` decide
what counts as entry level. Edit them directly if you want, say, data roles
only.

One trap if you edit `IT_TITLE`: it deliberately has **no closing `\b`**. Job
titles are full of plurals and suffixes, and a trailing word boundary silently
rejects "Data Scientist", "Web Developers" and "Cybersecurity" — the pattern
matches a prefix and the next character is still a letter. Short or ambiguous
tokens carry their own `\b` instead, so `sap` does not match "Sapphire".

## Exporting to a spreadsheet

Every run writes a file with the apply link for each posting. The format comes
from the extension, so you pick it by naming the file:

```bash
python -m src.run --dry-run                      # jobs.xlsx (the default)
python -m src.run --dry-run --export jobs.txt    # plain text
python -m src.run --dry-run --export jobs.csv    # csv, opens in Excel/Sheets
python -m src.run --dry-run --export ''          # skip the file entirely
```

Columns: Company, Title, Location, **Apply Link**, Source, Posted, First Seen.
In `.xlsx` the Apply Link cell is a real clickable hyperlink, the header row is
frozen, and autofilter is on so you can sort by company or location.

**The export writes the whole database, not just this run's new postings.**
That is deliberate, and it used to be the other way round. Under the old
default, a run that found 25 new jobs replaced an 11,000-row working sheet
with a 25-row file, logged `exported 25 job(s)` as though that were a success,
and gave you no way to tell that the rest had gone. Dedupe doing its job
correctly should never look like data loss.

For a per-run digest instead:

```bash
python -m src.run --export-new-only
```

`--export-all` is still accepted so existing commands and cron jobs keep
working; it is now a no-op, because it describes the default.

If the row count in your spreadsheet looks stale, it is almost certainly the
viewer rather than the file — LibreOffice and Excel do not reload a file that
changed underneath them. **File → Reload**, or check from the shell without
opening anything:

```bash
python -c "from openpyxl import load_workbook; print(load_workbook('jobs.xlsx').active.max_row-1)"
```

### Columns that are sometimes blank

Not every source publishes every field, and the exporter does not invent data:

- **Posted** is empty for Instahyre, Rippling and BambooHR — those APIs return
  no date at all. (Arbeitnow does, as a Unix timestamp rather than a date
  string, which is why it looked missing until it was parsed properly.)
- **Location** is empty for a few hundred postings whose source left it null.
  Those deliberately pass the location filter rather than being dropped.

`.xlsx` needs `openpyxl` (it's in `requirements.txt`). If it isn't installed the
run doesn't die after a five-minute fetch — it warns and writes `jobs.csv`
beside it instead.

## Deduplication

The hard part. The same job shows up on three sources with three titles.

1. **Fingerprint** — SHA of normalised company + title + canonical city. The
   URL is deliberately excluded, so the same role reached via two different
   sources collapses to one entry. Bengaluru/Bangalore, Gurgaon/Gurugram and
   friends are aliased to one spelling.
2. **Fuzzy pass** — `rapidfuzz.token_sort_ratio` at 95, scoped to the same
   company.

Use `token_sort_ratio`, not `token_set_ratio`. This matters more than it
sounds: `token_set_ratio` scores **100** whenever one title's tokens are a
subset of the other's, so a single "Software Engineer" posting silently
absorbed every "Senior Software Engineer, Backend", "Software Engineer,
Machine Learning" and "Staff Software Engineer, Platform" at the same
company. A full run lost about 6,000 real postings that way — and the case
the docs claimed it existed for, "SDE Intern" vs "Software Development
Engineer Intern", scored 85 and was never caught anyway.

The threshold errs high on purpose. A false merge loses a job permanently; a
false split only shows you a near-duplicate.

Lower `FUZZY_THRESHOLD` in `src/db.py` if you still see duplicates; raise it
if distinct roles are being swallowed.

Each run also stamps `last_seen` on every posting it sees again, so a listing
that stops appearing can be aged out later without being re-notified now.

## Tests

No network required — every payload is a fixture. 19 tests, about a second.

```bash
python tests/test_pipeline.py
```

Most of them are regressions for bugs that actually shipped: the location
filter matching everything, the dedupe swallowing senior roles, an empty
config list firing a request with a blank slug, SmartRecruiters returning
only the first page, and the IT-title regex rejecting "Data Scientist".

## Being a good citizen

Public ATS endpoints are fine to poll; abusing them is how they get closed.
This project already:

- sends a descriptive User-Agent (**edit it in `src/sources.py` to point at
  your own repo**);
- serialises requests **per host**, with a minimum gap between them. Twenty
  Greenhouse slugs all resolve to `boards-api.greenhouse.io`, and a thread
  pool would otherwise fire them simultaneously — global jitter does not help
  when the collision is on one host;
- retries only on 408/425/429 and 5xx, with exponential backoff, and honours
  `Retry-After` up to a 30-second cap. A 404 is never retried: a dead slug is
  dead, and asking three times just triples the load for the same answer;
- caps concurrency at 6 and paginates to a fixed ceiling per source;
- polls every three hours rather than every minute.

Please don't turn that cron down to `* * * * *`. A full run is ~28,000
postings across 206 targets and takes about four minutes.

## Watch out for

**Fake postings.** A large share of what circulates in job groups is Google
Forms and Microsoft Forms links that aren't on any company domain. Some are
recruiter lead-generation, some are straight data harvesting. This project
only trusts company-controlled ATS domains — if you add form links to
`jsonld`, you're reintroducing that problem.

**Slug rot.** Run `verify_slugs.py` monthly.

**Silent zero-result runs.** If a company's board returns 0 jobs, the adapter
logs a warning and moves on rather than crashing — one dead source must never
take the run down with it. You no longer have to read the logs to catch this:
every run writes a per-target row to `source_health`, so

```bash
python -m src.run --health
```

lists exactly the targets that have come back empty three runs in a row.
That is the difference between "nothing new today" and "this slug died in
March and nobody noticed".

**Name collisions in discovered slugs.** `discover.py` confirms that a board
exists and returns postings, not that it belongs to the company you meant.
`recruitee: accenture` is a real, live board; whether it is *the* Accenture is
another question. Harmless — it is still a genuine employer's board — but
don't read the config as a client list.

## Licence

MIT. Do what you like with it.
