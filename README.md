# Resume Agent — grounded resume tailoring

`ingest` builds reusable, grounded experience narratives from a full resume and
optional background documents. `analyze-jd` independently analyzes a local job
description. `select-experiences` plans which saved experiences to emphasize for
that job. `write-resume` creates a tailored Markdown draft from the saved run.
`review-alignment` evaluates that draft against the role and candidate evidence.
`review-recruiter` assesses human screening clarity and credibility after alignment review.
`finalize-resume` makes one grounded editing pass using both reviews.
`tailor` runs those six application stages sequentially in one new run.
Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`. Choose a model available to your
API account that supports Responses API structured outputs. Shell environment
values take precedence over `.env`. Candidate text is sent to the OpenAI API.

Place a full UTF-8 resume at `inputs/resume.md`. Optional `.md`/`.txt` background
documents may be placed anywhere under `inputs/background/`. Empty or absent
background directories are fine; blank background files are skipped. Other file
formats are ignored in that directory. Then run:

```bash
python main.py ingest
# For a text resume, or custom paths:
python main.py ingest --resume inputs/resume.txt
```

Optional flags: `--resume`, `--background`, `--bank`. Defaults are resolved relative
to the repository; explicit relative paths are resolved from your working directory.
No job description is needed for ingestion.

The output is `data/experience_bank.json`, shaped as
`{"experiences": [{"organization": "...", "title": "...", "content": "..."}]}`.
Fields must be nonblank; organization/title pairs must be unique ignoring case.
An empty bank is rejected. Every invocation explicitly rebuilds the bank. The
existing bank is replaced atomically only after extraction and validation succeed.
Failures exit with status 1 and preserve the existing bank.

Rebuild candidate evidence explicitly with `python main.py ingest` whenever your
resume/background changes should be incorporated into the bank. Then, for each
application, paste the local JD into `inputs/job_description.md` and run:

```bash
python main.py tailor --name company_role
# Optional source overrides, matching analyze-jd:
python main.py tailor --name company_role --resume path/to/resume.md --job-description path/to/job.md --bank path/to/experience_bank.json
```

`tailor` creates one timestamped application run and executes JD analysis →
experience selection → draft → alignment review → recruiter review → final edit.
Every stage reads the saved run artifacts, using the same recorded model. It
prints progress and, on success, the run directory and `final_resume.md` path;
the final status is `finalized`. It never runs ingestion or inspects background
files. A missing or invalid bank requires explicit ingestion before retrying.

Completed run contents:

```text
outputs/company_role-YYYYMMDD-HHMMSS/
├── run.json
├── source_resume.md
├── source_job_description.md
├── experience_bank_snapshot.json
├── jd_analysis.json
├── selected_experiences.json
├── draft_resume.md
├── alignment_review.json
├── recruiter_review.json
└── final_resume.md
```

On failure, orchestration stops, reports the failing stage, run location when
created, and saved status, and preserves completed artifacts. Continue a partial
run with the standalone commands below; `tailor` always starts a new run. For
example, after a recruiter-review failure, run `review-recruiter --run <path>`
then `finalize-resume --run <path>` through `python main.py`. There is no automatic
retry or rollback of earlier successful stages. Failures during initialization
can leave incomplete snapshots; create a new run after correcting that error.

The standalone commands remain useful for inspection, debugging, and targeted
reruns. To start with just JD analysis:

To analyze a job, provide `inputs/resume.md`, paste the full job description into
`inputs/job_description.md` (UTF-8 text), and build `data/experience_bank.json`
with `ingest` first. Then give the application a name:

```bash
python main.py analyze-jd --name twin-health_product-analytics
# Optional local source overrides:
python main.py analyze-jd --name twin-health_product-analytics --job-description path/to/job.txt --resume path/to/resume.md --bank path/to/bank.json
```

Each application creates its own timestamped directory and prints its path:

```text
outputs/twin-health_product-analytics-20260930-171500/
├── run.json
├── source_resume.md
├── source_job_description.md
├── experience_bank_snapshot.json
└── jd_analysis.json
```

Names must use `<company>_<role>`, with the first underscore separating the two
components. Names are lowercased; spaces and punctuation within either component
become hyphens. Additional role underscores are preserved, for example
`notion_product-data-scientist` or `twin-health_product_analytics_engineer`.
Both components must contain ASCII letters or digits.
Timestamps use the machine's local time; manifest timestamps include the UTC
offset. Existing directories are never overwritten: a same-name, same-second
collision fails and asks you to retry later or choose a different name.

The resume and JD snapshots preserve the exact source bytes. The bank snapshot
contains the validated bank, pretty-printed with schema whitespace normalization.
The analyst reads the saved JD and sends only that text to the configured model;
candidate snapshots are retained for future stages, not included in the JD call.
`jd_analysis.json` contains role, responsibilities, required/preferred qualifications,
technical/business skills, important language, and grounded hiring themes. Empty
categories are allowed. There is no global output file or `--output` override.

Missing, unreadable, blank, or invalid required sources fail before a run is
created. A missing or malformed bank requires an explicit `python main.py ingest`;
it is never rebuilt automatically. The small manifest records the name, creation
time, configured model, status, and a revision count starting at zero. Its status
is `initializing` during snapshot writes, `initialized` after snapshotting, and
`jd_analyzed` only after valid analysis is saved. Failed or interrupted runs retain
available diagnostic files with an incomplete status; there is no automatic resume
or retry workflow. If the first manifest write fails, a partial directory may have
no manifest and must also be treated as incomplete.

V1 accepts local `.md`/`.txt` source text only; URL fetching and web scraping are
deferred. Default paths are relative to the repository; explicit relative paths
use the working directory.

After JD analysis, select relevant experiences within that same run:

```bash
python main.py select-experiences --run outputs/twin-health_product-analytics-associate-engineer-20260930-180506
```

This command validates `run.json` and reads only the run's `jd_analysis.json` and
`experience_bank_snapshot.json` as model context. It does not reread global source
files or send the resume to the selector. `OPENAI_MODEL` must match the model
recorded in the run. The result is `selected_experiences.json`, a planning artifact
containing exact experience references, relevance explanations, qualitative
priorities, and an emphasize/deemphasize strategy. No resume content is written.

References must match existing organization/title pairs after trimming surrounding
whitespace and ignoring case; there is no fuzzy matching. Duplicate selected
pairs are rejected instead of guessing how to combine conflicting priorities.
The status becomes `experiences_selected` only after the validated selection is
saved. Runs with `jd_analyzed`, `experiences_selected`, `draft_written`, `alignment_reviewed`, `recruiter_reviewed`, or `finalized` status
are accepted. Reselection atomically replaces the validated selection, then removes
any stale draft, both reviews, and final resume, returning the status to `experiences_selected`.
Generation, validation, or persistence failures preserve prior artifacts and status.
If invalidation or manifest saving fails, the prior selection, draft, reviews, and final resume are restored.

After experience selection, generate a draft in the same run:

```bash
python main.py write-resume --run outputs/twin-health_product-analytics-associate-engineer-20260930-180506
```

The writer uses `source_resume.md`, `jd_analysis.json`, `selected_experiences.json`,
and `experience_bank_snapshot.json`. Only the source resume and bank establish
candidate facts; the JD analysis and selection guide emphasis. The source resume
provides the structure and contact details. Selection references are revalidated
against the saved bank before generation, and `OPENAI_MODEL` must match `run.json`.
The writer makes one plain-text model call and saves `draft_resume.md` atomically
after checking for a completed, non-blank response. It then sets `draft_written`.
Runs with `experiences_selected`, `draft_written`, `alignment_reviewed`, `recruiter_reviewed`, or `finalized` status
are accepted; a successful rerun replaces the draft without creating versions and
removes both stale reviews and the final resume only after the replacement succeeds. API/validation
failures preserve prior artifacts and status. Invalidation or manifest-write failures
restore the prior draft, reviews, and final resume, following the same approach as reselection.

Grounding, contact preservation, and concise one-page intent are prompt requirements,
not automated factual or physical-layout guarantees.

Review a completed draft using the same saved application context:

```bash
python main.py review-alignment --run outputs/twin-health_product-analytics-associate-engineer-20260930-180506
```

This makes one structured call using `draft_resume.md`, `source_resume.md`,
`jd_analysis.json`, `selected_experiences.json`, and `experience_bank_snapshot.json`.
It produces `alignment_review.json` with `needs_revision`, specific strengths, and
issues containing qualitative severity and actionable recommendations. Candidate
facts come only from the source resume and bank; unsupported job requirements are
gaps, not permission to fabricate skills. ATS assessment is qualitative, without
scores or vendor simulation. The reviewer does not edit the draft.

The command accepts `draft_written`, `alignment_reviewed`, `recruiter_reviewed`, and `finalized` runs, enforcing the
run's recorded model. Empty issue lists require `needs_revision: false`; informational
gaps may also be reported without requiring changes to an already truthful draft.
Status becomes `alignment_reviewed` after a valid review is saved. Successful reruns
replace the review without versioning and invalidate any stale recruiter review and final resume
only after the replacement succeeds. Failed review or persistence preserves prior
artifacts and status.

After alignment review, assess the draft from a human-screening perspective:

```bash
python main.py review-recruiter --run outputs/twin-health_product-analytics-associate-engineer-20260930-180506
```

This reads the saved draft, source resume, JD analysis, bank snapshot, and alignment
review. The alignment review is advisory context, not candidate evidence; the
recruiter adds screening-specific concerns rather than merely repeating it. The
optional selection artifact is not used. One structured call saves
`recruiter_review.json` with `needs_revision`, specific strengths, and issues with
qualitative severity and recommendations. An empty issue list requires
`needs_revision: false`. No scores or automatic resume edits are produced.

The command accepts `alignment_reviewed`, `recruiter_reviewed`, and `finalized` runs, enforces
model consistency, and sets `recruiter_reviewed` only after saving a valid result.
Reruns replace the review and then invalidate stale final output; failures preserve
prior artifacts and status.
Grounding and distinct human-screening judgment remain prompt requirements rather
than deterministic factual guarantees.

After both reviews, finalize the draft in the same run:

```bash
python main.py finalize-resume --run outputs/<run-folder>
```

This makes one grounded revision pass using the saved draft, source resume, bank,
JD analysis, selection, and both reviews. Candidate evidence remains authoritative;
reviewer advice guides alignment and readability without establishing new facts.
The result is `final_resume.md`, saved atomically after checking nonblank output
and candidate-name preservation. For this lightweight check, put the candidate's
name alone on the first nonblank source-resume line; Markdown heading/emphasis
formatting is allowed. Name matching ignores case, formatting, and whitespace.
Other identity facts and grounding remain prompt-guided, without a semantic parser
or page-layout validation.

The command accepts `recruiter_reviewed` and `finalized`, enforces the recorded
model, and sets `finalized` after persistence. Reruns edit the saved draft again
and replace the final without versioning. Failures preserve prior final output
and status; manifest-write failures restore the previous final. Successful upstream
reruns invalidate stale final output only after the upstream replacement succeeds.

The helper uses synchronous `responses.parse` with Pydantic validation, following
[OpenAI's structured-output documentation](https://developers.openai.com/api/docs/guides/structured-outputs).
The writer and final editor use `responses.create` and `output_text` for
[plain-text generation](https://developers.openai.com/api/docs/guides/text?lang=python).
No extraction-level repair loops or input truncation are performed. Inputs too
large for the configured model fail rather than being silently shortened.

Local checks (mocked API; no key or network required):

```bash
python -m unittest discover -s tests -v
```

Private inputs, data, outputs, `.env`, and the virtual environment are Git-ignored.
After tailoring, optionally apply a targeted edit:

```bash
python main.py revise --run outputs/twin-health_product-analytics-associate-engineer-<timestamp> --instruction "Make the BRG section slightly less technical and emphasize business impact."
```

`revise` accepts `finalized` or `revised` runs and a nonblank instruction. It uses
only saved candidate evidence and planning context, with the same recorded model.
User editorial intent takes priority over prior reviewer preferences, but cannot
establish unsupported candidate facts. Old reviews are intentionally omitted from
the revision payload. No reviewers or Final Editor are automatically rerun;
`tailor` still stops at `finalized`.

Each request makes one editing pass over the latest `revision_XXX.md`, or
`final_resume.md` for the first request. It saves a new `revision_001.md`, then
`revision_002.md`, and so on. History is linear; prior versions remain intact.
Numbering must be contiguous and match `run.json.revision_count`. Gaps, unexpected
files, noncanonical names, or inconsistent status/count fail rather than guessing.
A nonblank final resume remains required even when revisions exist. The latest
numbered revision is the current user-edited version.

Output must be nonblank and retain the source candidate name, using the same
lightweight check as finalization. Revision creation is atomic and refuses to
replace an existing filename. Only successful persistence updates status to
`revised` and increments the count. If manifest saving fails, the new revision is
removed and the prior manifest restored. Grounding beyond this identity check
remains prompt-guided.

All upstream commands also accept `revised` runs. Successful reselection, writing,
either review, or finalization invalidates stale `revision_*.md` and resets
`revision_count` to zero, only after the upstream replacement succeeds. Failed
reruns preserve revisions with the other prior artifacts. Thus history is preserved
across user revisions, but intentionally cleared by successful upstream reruns.
Run commands sequentially within a run; there is no concurrent workflow manager.

