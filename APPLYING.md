# Applying, not just watching

The scheduled workflow finds jobs and posts them. This part reads your CV,
ranks what it found against your actual skills, drafts a letter, and fills the
form. It runs **on your machine only** — applying needs your resume, your
judgement and a browser, none of which belong in a public CI job.

```bash
pip install -r requirements-optional.txt      # pypdf, and playwright for autofill
playwright install chromium                   # only for `apply.py fill`
```

---

## What can and cannot be automated

Worth being precise, because "auto-apply" is sold a lot and delivered rarely.

**There is no candidate-side apply API.** Not on Greenhouse, Lever, Ashby or
Workday. I checked rather than assumed: `POST` to Greenhouse's board endpoint
and to `api.greenhouse.io/v1/applications` both return **401**. Submission
authenticates as the *employer*. So nothing can submit an application for you
over HTTP, and any tool claiming otherwise is either driving a browser or
lying.

**What Greenhouse does publish is the question list**, which turns out to be
the useful half. Before you open a page, this can tell you it will ask for a
LinkedIn URL, a work-authorisation answer and four custom questions — so you
can decide whether it is worth twenty minutes.

**So `fill` drives a real browser**, and holds to three rules:

1. **It never clicks submit.** It fills the fields, attaches your resume, and
   leaves the form open. You read it and submit. An application cannot be
   recalled.
2. **It runs headed.** You watch it work. A headless robot filling forms you
   never see is how people apply to the same job nine times.
3. **One at a time.** No batch mode, deliberately. Mass-applying is worse than
   useless — recruiters filter it, and a boilerplate application is how your
   name gets remembered for the wrong reason.

---

## Setup

Put your CV in the project as `resume.pdf`, then:

```bash
python apply.py profile
```

```
wrote profile.yaml
  name    ANIKESH KUMAR
  title   Full Stack Developer
  years   0.5
  skills  31: celery, ci/cd, computer vision, css, flask, git, html, java...

Fill these in profile.yaml before applying — forms ask for them constantly:
  notice_period, current_ctc, expected_ctc, earliest_start_date
```

**Open `profile.yaml` and fill in the blanks.** Parsing gets the facts off your
CV; only you can say what your notice period is. Everything under `answers:`
is reused on every application, and re-running `profile` never overwrites what
you typed.

`resume.pdf`, `profile.yaml`, `applications.db` and `cover-letters/` are all
gitignored. Your repo is public and a CV carries your phone number.

---

## Using it

```bash
python apply.py list                 # ranked matches you have not applied to
python apply.py show 3               # the posting, its form, a draft letter
python apply.py open 3               # open the apply page
python apply.py fill 3               # browser autofill, you press submit
python apply.py log 3                # record that you applied
python apply.py status               # history
```

```
  #    fit  company               title                              location
  1    87%  Accenture             AI / ML Associate Manager          Bangalore,Chennai,Pune
  2    86%  Infygain Technologie  Full Stack Developer               TN, IN
  3    83%  Nykaa                 Full Stack Engineer                Bangalore
  4    83%  Nineleaps             Python Engineer                    Bangalore
```

Logged applications drop out of the list, so it is always the next thing to do
rather than a leaderboard.

`--min-score N` moves the threshold (default 55), `--anywhere` includes roles
outside India, `--limit N` shows more.

---

## How the score works

It is an argument, not a verdict — a 40 you are excited about beats an 82 you
are not. Three parts:

| | Weight | What it measures |
|---|---|---|
| Skills | 55 | share of the posting's named technologies you have |
| Seniority | 25 | distance between the title's level and yours |
| Experience | 20 | the years it asks for against the years you have |

`show` always prints its reasoning:

```
  match 83% (strong)
    - matches java, javascript, python, react
    - 4/4 listed skills match
    - a stretch on seniority
    - not on your CV: kubernetes, kafka
```

Two deliberate wrinkles, both from watching it get things wrong:

- **A vague posting cannot reach the top.** Coverage alone gave "1 of 1 skills
  matched" a 94% and floated the thinnest ads above jobs matched six ways.
  Confidence now scales with how much the posting actually committed to.
- **Education years are not experience.** A 2023–2027 degree counted as four
  years and pushed every experience filter out of reach.

After editing your CV, the skill list or the filters:

```bash
python -m src.run --rescore
```

---

## The cover letter

`show` writes a draft to `cover-letters/<company>-<role>.txt`, naming the
skills that actually overlap. It is a template, not a model: free, offline,
instant, and identical every time so you can read it once and trust the shape.

**Edit it.** A short specific letter beats a fluent generic one, and the draft
is a starting point that saves you the blank page, not a finished thing.

---

## What `fill` actually does

```
  Nykaa — Full Stack Engineer   (83% match)
  greenhouse, 14 questions

  A browser will open and the form will be filled in front of you.
  Nothing is submitted: you check it and press submit yourself.
  Continue? [y/N]
```

It fills name, email, phone, location, LinkedIn, GitHub, notice period,
expected CTC and the standard authorisation answers, attaches `resume.pdf`,
and pastes the cover letter. Then it lists what it could not answer — the
dropdowns and the "why this company" boxes, which are the ones worth writing
yourself anyway.

It matches fields on the name, id, placeholder and aria-label, because boards
disagree about which of the four they populate. Custom or unusual forms will
leave more for you; nothing breaks, it just fills less.

---

## Honest limits

- **Workday** is the weakest case. It wants an account per employer, and
  multi-step forms with server-side state defeat a single-pass filler.
- **Scores from `--rescore` are blunter** than fresh ones. Descriptions are
  truncated to 200 characters in storage, so a rescore sees less than the
  fetch-time score did.
- **This will not get you a job.** It removes the twenty minutes of retyping
  the same details, and tells you which fifteen of four thousand postings are
  worth reading. The application still has to be yours.
