# Keeping the feed regular

GitHub Actions runs this for free with nothing on your machine — but its cron
is best-effort, and on a low-activity public repo it is worse than that.
Measured on this repository against an hourly schedule:

```
19:53  →  22:35   gap 2.7h
22:35  →  01:12   gap 2.6h
```

**3 of 6 slots fired. Roughly half were skipped.** Nothing is misconfigured;
this is documented GitHub behaviour and it cannot be fixed from inside the
repo. So the feed is defended two ways.

---

## Defence 1 — each run catches up (already on)

A run does not send a fixed batch. It checks how long it has actually been
since the last delivery and sends `rate x hours`, capped:

| Gap since last post | Sent |
|---|---|
| 20 min | 5 |
| 1 hour | 15 |
| 2.6 hours | 39 |
| 5+ hours | 60 (capped) |

So the daily volume holds near the target whether GitHub fires six times or
twenty. Nothing to configure — it is the default.

A **floor** of 20 minutes stops the opposite problem: once several schedulers
are pointed at the same workflow, runs can arrive minutes apart, and without a
floor the channel would get a trickle of two-message bursts. A run inside the
floor still fetches and stores; it just does not post. `--min-interval 0`
removes it.

---

## Defence 2 — more alarm clocks

The rest of this page. Each one is free and takes a few minutes.

> **Add schedulers, not runners.** Every option below *triggers the GitHub
> workflow*; none of them run the pipeline themselves. That is deliberate. Two
> independent runners would each keep their own database, both would think the
> same posting was new, and your channel would get everything twice. One
> executor, one state, as many alarm clocks as you like.
>
> GitHub's `concurrency` group serialises the runs that result, so triggers
> arriving at the same moment queue up instead of colliding.

### First: make a token for them

All three options need a token to call GitHub's API. Make one that can do
nothing else:

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Repository access:** Only select repositories → `job-radar`
3. **Permissions:** Repository permissions → **Actions: Read and write**
4. Copy it.

That token cannot read your code, your secrets, or any other repository. If it
leaks, the worst anyone can do is run your job feed.

The request every option below makes is the same one:

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/Anikesh0001/job-radar/actions/workflows/post.yml/dispatches \
  -d '{"ref":"main"}'
```

`204 No Content` and an empty body means it worked. Test it in your terminal
before wiring anything up — if it fails there, it will fail everywhere.

---

### Option A — cron-job.org (recommended)

Free, unlimited jobs, down to every minute, and far more punctual than GitHub.
No card, no account limits worth worrying about.

1. Sign up at [cron-job.org](https://cron-job.org).
2. **Create cronjob**:
   - **URL:** `https://api.github.com/repos/Anikesh0001/job-radar/actions/workflows/post.yml/dispatches`
   - **Schedule:** every hour (or every 30 minutes — the floor protects you)
3. **Advanced** tab:
   - **Request method:** `POST`
   - **Headers:**
     ```
     Authorization: Bearer YOUR_TOKEN
     Accept: application/vnd.github+json
     ```
   - **Request body:** `{"ref":"main"}`
4. Save, then **Test run**. Expect `204`.

cron-job.org treats a 204 as success and will email you when it starts failing,
which doubles as monitoring for the trigger itself.

### Option B — GitLab CI

`.gitlab-ci.yml` in this repo already does it. GitLab's scheduler is punctual
and the free tier is generous; one curl a run costs seconds of the monthly
allowance.

1. Import the repo at
   [gitlab.com/projects/new#cicd_for_external_repo](https://gitlab.com/projects/new#cicd_for_external_repo).
2. **Settings → CI/CD → Variables** → add `GH_DISPATCH_TOKEN`, **Masked: yes**,
   **Protected: no** (scheduled pipelines run unprotected).
3. **Build → Pipeline schedules → New schedule**, cron `0 * * * *`.

The pipeline only runs on a schedule or the manual button, never on a push, so
mirroring the repo does not fire it.

### Option C — UptimeRobot

Free for 50 monitors at 5-minute intervals. Built for pinging, so it is the
most reliable of the three, but its free plan sends **POST with custom
headers** only on some plan tiers — check before relying on it.

- **Monitor type:** HTTP(s)
- **URL:** the dispatch endpoint above
- **Method:** POST, with the same two headers and body
- **Interval:** 30 minutes

### Options deliberately not used

| | Why not |
|---|---|
| **Render / Railway / Fly.io** | free cron tiers withdrawn or trial-only |
| **PythonAnywhere** | free tier allows one scheduled task a day, and restricts outbound hosts to a whitelist |
| **Cloudflare Workers** | cron is excellent and free, but Workers run JS/WASM — this is Python |
| **Oracle Cloud Always Free** | genuinely free forever and the most capable option, but it needs a credit card and a VM you now own and patch |

---

## What to expect once it is set up

With cron-job.org hourly **and** GitHub's own schedule, the channel gets a post
roughly every hour. When GitHub skips a slot, cron-job.org covers it; when both
fire close together, the 20-minute floor drops the second one and the next run
catches up. Neither can double-post, because neither holds any state.

Check it is working:

```bash
python -m src.run --stats
```

```
when                 mode      fetched  kept  new  sent  queued  dead  secs
2026-09-09 11:10:04  fetch        4254   752   45    15     604    3      54
2026-09-09 10:09:58  fetch        4198   731   38    15     619    3      51
```

Even `sent` counts an hour apart mean the pacing is working. Long gaps in the
`when` column mean every scheduler missed, which is when it is worth adding
another.
