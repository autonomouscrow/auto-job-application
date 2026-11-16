# auto-job-application

A local, human-in-the-loop assistant for internship applications. It reads
job-list repos on GitHub, shows you one posting at a time with a match score,
prepares a resume and cover letter that can only contain facts from your own
profile, and keeps track of everything you have applied to.

> **Status: early development.** Working today: reading sources, filtering,
> the one-job-at-a-time queue, the tracker, reading job postings, the full
> match score, filters based on the job description, and alerts when the
> Claude API key needs attention. Document tailoring is next (see the
> roadmap).

## What it does

- **Reads many sources.** Add any GitHub job-list repo. Parsers cover JSON
  data files, per-company YAML folders, and Markdown or HTML tables in a
  README, with an LLM fallback for unusual layouts. The same role listed in
  several repos shows up once.
- **Skips dead postings.** Closed roles are filtered using the source's own
  markers, by noticing when a link disappears, and by checking that the
  posting itself is still live.
- **Scores every job.** A 0 to 100 match score compares the job description
  with your skills and experience, with a visible breakdown of what matched
  and what is missing. Hard blockers such as citizenship requirements are
  shown as flags or filtered out with the reason, instead of being buried in
  the number. See "How the match score works" below.
- **One job at a time.** New postings go to the front of the queue. For each
  one you choose **Apply**, **Set aside** (a separate queue for later), or
  **Reject** (never shown again).
- **Tailors without inventing.** Resumes and cover letters are built only
  from a hand-written profile of facts. See "No hallucinated resumes" below.
- **Tracks applications.** A built-in tracker with status, timeline, the
  exact documents sent, follow-up reminders, stats, and CSV export.

What it does not do: it never submits an application for you. You review
everything and you click Submit.

## No hallucinated resumes

Letting a model rewrite a resume again and again slowly drifts it away from
the truth. This project is built to make that impossible:

1. Your facts live in plain YAML files, the **truth profile**, where every
   fact has an ID.
2. Documents are always generated from the profile, never from an earlier
   generated document, so there is no chain of edits to drift along.
3. Tailoring means selecting and rephrasing facts. Every line of output must
   cite the fact IDs it came from.
4. A plain-code verifier rejects any line containing a number, date,
   employer, title, or skill that is not in the cited facts.
5. If you want to add something new, you add it to the profile first.

To keep costs and review time down, the tool keeps a few approved
**archetype resumes** (for example one per kind of role) and reuses them. A
job gets a small touch-up or a full retailor only when no archetype fits.

## How it fits together

```
 job-list repos -> ingest -> enrich (fetch posting, extract requirements)
                                |
        truth profile -----> match (role type, score, filters)
                                |
                          review queue  (apply / set aside / reject)
                                |
        truth profile -----> tailor (archetype resume, cover letter, verify)
                                |
                          assist (prepared answers, you submit)
                                |
                             tracker
```

## Tech stack

| Piece | Choice |
|---|---|
| Language | Python 3.12, managed with `uv` |
| Storage | SQLite (SQLModel) |
| Interface | local web app: FastAPI and Jinja; plus a Typer CLI |
| Browser automation | Playwright |
| LLM | Claude API with structured outputs |
| Profile | YAML validated with Pydantic |
| Resume output | LaTeX template compiled with Tectonic |
| Tests | pytest |

## How the match score works

Each job gets a score from 0 to 100. Before its posting has been read, the
score is a provisional one worked out from the title, term, and location,
and the card says so. Once the posting has been read, the full score
replaces it. It has six parts:

| Part | Points | How it is worked out |
|---|---|---|
| Must-have skills | 40 | share of the posting's required skills that are on your skills list |
| Nice-to-have skills | 15 | the same for the skills it lists as preferred |
| Experience relevance | 20 | Claude rates from 0 to 10 how well your roles and projects fit the actual work |
| Role type | 10 | 10 for one of your target roles, 5 for an adjacent role |
| Eligibility | 10 | full marks unless a work authorization or graduation flag below lowers it |
| Work mode | 5 | 5 for your first choice of onsite, hybrid, or remote, one less for each step down your list, 4 when the posting does not say |

A posting that lists no skills of one kind gets 70% of those points.

Some things are never turned into points. They either remove the job or
put a flag on its card.

These are **filters**. The job moves to **Filtered out** with the reason,
and you can bring it back from there:

- the posting is closed (its page is gone or says applications are closed)
- the term or dates do not fit: it starts after your term ends, more than
  45 days before it starts, names another season without giving dates, or
  runs longer than your maximum length
- it requires a language other than your working language, or is written in
  another language
- its location is outside your regions
- it is only for a degree level other than yours
- it requires citizenship of a country you are not a citizen of

Most of these are also checked from what the source lists, before the
posting is read.

These are **flags**, shown on the card:

- citizenship required without naming a country (eligibility drops to 0)
- security clearance required (eligibility drops to 0)
- you must already have the right to work there (eligibility at most 4)
- no visa sponsorship offered
- it wants graduation in a window that does not include yours (eligibility
  at most 4)
- it starts a little before your term or runs past its end
- the application deadline ("apply by")

Postings are read only for the jobs at the front of the queue, so time and
money are not spent on jobs you may never reach. The `read_ahead` setting
under `llm` in `config.yaml` says how many (10 by default). As you go
through the queue, the jobs behind are read in turn. A posting is fetched
again after `refresh_days` days (4 by default) to notice when it closes.

## Setting up the Claude API key

Reading postings needs a Claude API key.

1. Create a key in the [Anthropic Console](https://console.anthropic.com)
   under API keys.
2. Put it in a file named `.env` inside your home folder, never in the
   repository:

   ```
   ANTHROPIC_API_KEY=your-key
   ```

   `.env.example` in the repository shows the format.
3. Run `uv run jobs doctor` to check that the key works.

If the key is missing, expired, revoked, or out of credit, or Claude is rate
limiting it, the app shows a banner that says which and what to do. In the
meantime it keeps working: it still checks that postings are open and shows
provisional scores.

The models are set under `llm` in `config.yaml`. The default reading model
is the small, cheap one, which costs a fraction of a cent per posting.

## Your data stays on your machine

This repository contains code only. Everything personal lives in a separate
folder outside the repository, which the app finds through the
`JOBTOOL_HOME` setting:

```
<JOBTOOL_HOME>/
  profile/       your truth profile (YAML)
  resume/        your resume template
  config.yaml    sources, targets, filters
  .env           your API key
  data/          database, generated documents, backups
```

The `examples/` folder holds a profile and config for a made-up person, and
all tests and screenshots use that. If you fork this project, keep your own
data out of the repository in the same way. A pre-commit hook is provided
that refuses commits containing your name, email, or phone number.

When a posting is read, the Claude API receives the posting's title,
company, and text, plus these parts of your profile: your skills list, your
degree and year of study, the titles and dates of your roles, the names and
summaries of your projects, and the text of the facts under each of them. It
never receives your name, email, phone, address, links, or the names of your
school and employers. When documents are written (Phase 3), the parts of
your profile needed for that document are sent too. Nothing else leaves your
machine.

## Roadmap

- [x] **Phase 0:** project skeleton, profile schema and validation, example
      profile, commit safeguards
- [x] **Phase 1:** sources page and parsers, closed-job filtering, the
      one-job-at-a-time queue, tracker table, CSV export
- [x] **Phase 2:** posting fetch, requirements extraction, filters, full
      match score
- [ ] **Phase 3:** archetype resumes, tailoring, the verifier, cover letters
- [ ] **Phase 4:** application assist panel and reusable answer bank
- [ ] **Phase 5:** board view, stats, scheduled sync, reminders

## Getting started

Requires Python 3.12 or newer, [`uv`](https://docs.astral.sh/uv/), and git.

```
git clone <this repository>
cd auto-job-application
uv sync
git config core.hooksPath .githooks     # turns on the personal-info commit guard
```

Try it with the made-up applicant:

```
uv run jobs profile check --home examples/home
uv run jobs profile facts --home examples/home
```

To use your own data, copy `examples/home` to a folder **outside** the
repository, replace its contents with your own facts, and point the tool at
it. By default the tool looks for a folder named `private` next to the
repository; set `JOBTOOL_HOME` to use a different location.

```
uv run jobs profile check        # validate your profile
uv run jobs sync                 # read your sources and fill the queue
uv run jobs serve                # open the queue in your browser
```

### Using the queue

The app shows one job at a time, newest finds first, then by match score.

| Button | Key | What happens |
|---|---|---|
| Apply | `A` | opens the posting in a new tab and keeps the job on screen as "in progress" |
| I submitted it | `A` | records the application and shows the next job |
| Set aside | `S` | moves the job to a separate queue, with an optional note |
| Reject | `R` | the job is never shown again |
| Undo | `U` | reverses the last button press |
| | `O` | opens the posting without starting an application |

Other pages: **Tracker** (applications, status, follow-up dates, CSV export),
**Set aside**, **Rejected**, **Filtered out** (jobs removed automatically,
each with its reason and a button to bring it back), and **Sources** (add a
repository, preview what was parsed, turn sources on and off).

Once a posting has been read, the card shows what it asks for, which of the
required and preferred skills you have and which you lack, a short reason for
how well your experience fits, and whether the posting has been read yet or
could not be read. Until then the score is provisional and the card says so.

### Commands

| Command | What it does |
|---|---|
| `jobs serve` | starts the local web app (it listens on this computer only) |
| `jobs sync` | fetches every source, updates the queue, then reads the postings at the front of it |
| `jobs sync --no-read` | the same without reading any postings |
| `jobs enrich` | fetches and reads the postings at the front of the queue and scores them fully (`--limit N` sets how many) |
| `jobs doctor` | checks the home folder, profile, database, and Claude API key, and shows what Claude has cost so far |
| `jobs export FILE` | writes every application to a CSV file |
| `jobs backup` | copies the database to `data/backups` (also done daily by `serve`) |
| `jobs profile check` | validates the profile and lists every problem and warning |
| `jobs profile facts` | lists every fact with its ID |
| `jobs guard` | scans tracked files for your name, email, phone, and extra terms |
| `jobs guard --staged` | the same for the pending commit; run by the pre-commit hook |

### Development

```
uv run pytest
uv run ruff check .
```

## Responsible use

This is a tool for applying carefully and faster, not for mass applying. It
keeps you in the loop on every application, and it is your responsibility to
follow the terms of the job sites you use and to make sure everything you
submit is true.

## License

Not chosen yet.
