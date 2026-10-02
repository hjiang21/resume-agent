# Resume Agent — Decision Log

This file records noteworthy product, architecture, and implementation decisions for the `resume-agent` project, including the rationale behind them. It is meant to make the project easier to understand, maintain, and explain later.

---

## 2026-09-30 — Project Direction

### Build a resume-tailoring system around a reusable experience bank

**Decision:** The system will not tailor resumes using only the current one-page resume. It will maintain a reusable experience bank built from the resume plus optional richer background material.

**Why:** A one-page resume omits technical depth, context, tradeoffs, secondary experiences, and details that may become relevant for a different job. A reusable experience bank gives downstream components more complete candidate context.

**V1 experience-bank fields:**
- `organization`
- `title`
- `content`

**Explicitly excluded from V1:**
- experience IDs
- skills arrays
- ATS-keyword fields
- job-family labels
- source-document provenance
- prewritten resume bullets
- job-specific framing

The bank should remain general-purpose context. Job-specific interpretation happens downstream.

---

## 2026-09-30 — Experience IDs

### Do not add persistent `experience_id` fields in V1

**Decision:** Selected experiences will be referenced using the `(organization, title)` pair.

**Why:** Persistent IDs add structure without much value at the current scale. The extractor is instead instructed to create descriptive titles that are unique within each organization.

**Validation:** Duplicate `(organization, title)` pairs are rejected case-insensitively.

**Revisit if:** The bank grows large enough that duplicate or unstable titles become a real maintenance problem.

---

## 2026-09-30 — Source Provenance

### Do not store `source_documents` in each experience in V1

**Decision:** The experience bank will not track provenance at the entry level.

**Why:** Provenance could be useful for debugging a much larger or production system, but it adds overhead now. The user intentionally supplies the candidate corpus, and the project can add provenance later if source tracing becomes necessary.

---

## 2026-09-30 — Orchestration

### Use deterministic Python orchestration, not an autonomous manager agent

**Decision:** Ordinary Python controls the call order for V1.

**Why:**
- easier to debug;
- easier to explain;
- easier to inspect failure points;
- no need for dynamic routing in the current workflow;
- avoids unnecessary agent-framework complexity.

The LLM components are specialized model calls, not independent autonomous agents with their own loops/tools.

**Future possibility:** Dynamic routing or handoffs can be added only if the workflow develops a real need for them.

---

## 2026-09-30 — V1 Pipeline

The agreed fixed pipeline is:

1. Experience Extractor
2. JD Analyst
3. Experience Selector
4. Resume Writer
5. Job Alignment / ATS Reviewer
6. Recruiter Reviewer
7. Final Editor
8. User Revision Agent (human-in-the-loop, after the automatic pipeline)

There is **no hiring-manager reviewer in V1**.

---

## 2026-09-30 — Experience Extractor

### Purpose

Build or rebuild a persistent experience bank from:
- full resume;
- optional `.md` / `.txt` background documents.

### Behavior

The extractor should:
- identify distinct experiences;
- consolidate duplicate references to the same underlying experience;
- preserve useful technical/business detail;
- retain metrics, responsibilities, technologies, decisions, and context;
- avoid job-specific framing;
- avoid aggressive compression;
- use descriptive titles unique within an organization.

### Input handling

- `inputs/resume.md` is required by default.
- `inputs/background/` is optional.
- background files are read recursively in stable sorted order;
- `.md` and `.txt` are supported;
- blank files are skipped;
- no chunking/truncation is performed in V1.

---

## 2026-09-30 — Background Corpus Strategy

### Use a curated `career_experiences.md` rather than dumping all interview-prep files into the pipeline

**Decision:** The main background document is:

`inputs/background/career_experiences.md`

**Why:** Existing Coinbase/interview master documents contain excellent underlying career detail but also large amounts of interview-specific material such as behavioral mappings, answer scripts, "do not say" notes, and drilldown guidance. The resume agent needs the underlying work, not the interview scaffolding.

`career_experiences.md` therefore consolidates rich experience context while stripping interview-prep clutter.

It currently covers:
- BRG sporting-goods promotion / margin analysis;
- BRG assortment / demand-reallocation optimization;
- BRG footwear lost-sales model / model assurance;
- BRG Snowflake data-pipeline reliability;
- BRG retail pilot / KNN matching / KPI framework;
- BRG university technology-spend assessment;
- Edwards 2025 ML defect-detection project;
- Edwards 2024 manufacturing QC workflow redesign;
- Ellison cancer-research analytics;
- Disease ChatDB;
- Trojan Tennis leadership.

---

## 2026-09-30 — JD Analyst

### Output should be structured, not freeform

Planned structure:

```json
{
  "role": "",
  "core_responsibilities": [],
  "required_qualifications": [],
  "preferred_qualifications": [],
  "technical_skills": [],
  "business_skills": [],
  "important_language": [],
  "hiring_themes": []
}
```

**Why:** The system should identify broad hiring themes and responsibilities, not merely scrape keywords.

---

## 2026-09-30 — Experience Selector

### Separate selection from writing

**Decision:** The selector decides which candidate experiences best support the target role before the writer drafts.

**Why:** This creates an inspectable reasoning layer between JD analysis and resume generation and makes it easier to diagnose why content was included or omitted.

The selector should return:
- selected experiences;
- relevance rationale;
- priority;
- resume strategy (`emphasize` / `deemphasize`).

Python will resolve selected `(organization, title)` references back to the actual experience-bank narratives before passing them downstream.

---

## 2026-09-30 — Reviewers

### Keep two reviewers in V1

**Reviewer 1: Job Alignment / ATS**
- primary reviewer;
- checks whether important JD requirements/themes are reflected;
- looks for missing terminology, buried relevant skills, irrelevant space use, and machine-readable structure;
- does **not** pretend to simulate a company's ATS;
- does **not** generate a fake ATS score.

**Reviewer 2: Recruiter**
- secondary reviewer;
- evaluates first-screen clarity, obviousness of fit, impact, readability, trajectory, and concerns;
- is not a keyword checker.

### Reviewer priority

Job-alignment feedback is the primary optimization objective. Recruiter feedback is secondary and should improve clarity/persuasion without overriding stronger job-alignment needs.

No arbitrary numeric weighting (e.g. 70/30) is needed.

---

## 2026-09-30 — Final Editor

### One automatic revision cycle only

**Decision:** The Final Editor receives:
- draft;
- JD analysis;
- experience selection / strategy;
- job-alignment review;
- recruiter review;
- relevant candidate context.

It resolves conflicts rather than blindly applying every reviewer suggestion.

**No endless self-reflection loop.**

**Why:** A single explicit review/revision cycle is easier to inspect, cheaper, and sufficient for V1.

---

## 2026-09-30 — Human-in-the-Loop Revision

### Provide a separate `revise` workflow

**Decision:** After the automatic pipeline, the user can give natural-language editing instructions such as:
- shorten one bullet;
- move a section;
- emphasize a particular metric;
- reduce modeling emphasis;
- preserve a specific line exactly;
- change only one bullet.

The revision agent should:
- modify only what is necessary;
- preserve unaffected content;
- honor "do not change anything else" instructions as strongly as possible;
- use the saved application context;
- not automatically rerun both reviewers after every user edit.

### Version history

Do not overwrite prior revisions.

Example:
- `final_resume.md`
- `revised_resume_01.md`
- `revised_resume_02.md`

---

## 2026-09-30 — Input / Output Formats

### V1 uses `.md` and `.txt`

**Decision:** No PDF or DOCX parsing/generation in V1.

Markdown can represent:
- name/contact header;
- section headings;
- employers;
- titles;
- dates;
- bullets;
- education;
- skills;
- section ordering.

### Limitation

Markdown cannot reliably determine:
- exact font/margins;
- precise page geometry;
- whether a bullet physically wraps to two vs. three lines.

Users can still provide layout constraints manually during revision.

---

## 2026-09-30 — No Frontend in V1

**Decision:** CLI only.

No:
- Streamlit;
- React;
- hosted web application;
- authentication.

**Why:** The intelligence pipeline should work reliably before UI work begins.

---

## 2026-09-30 — No Vector Database / Embeddings in V1

**Decision:** Pass the experience bank directly to the relevant model calls.

**Why:** The candidate corpus is small enough that retrieval infrastructure would add complexity without a current benefit.

**Revisit if:** The corpus grows large enough that context limits, latency, or relevance selection become real issues.

---

## 2026-09-30 — Persistence and Run Reproducibility

### Store each application as a self-contained run

Planned run structure:

```text
outputs/<run-name>/
├── run.json
├── source_resume.md
├── source_job_description.md
├── experience_bank_snapshot.json
├── jd_analysis.json
├── selected_experiences.json
├── draft_resume.md
├── alignment_review.json
├── recruiter_review.json
├── final_resume.md
└── revised_resume_01.md
```

**Why:** Later revisions should use the candidate context and JD associated with that application, even if global inputs or the experience bank change later.

### `run.json`

Keep the manifest small. Likely fields include:
- run name;
- creation time;
- model;
- revision count;
- relevant configuration/filenames.

---

## 2026-09-30 — Experience Bank Rebuild Behavior

### Explicit rebuild via `ingest`

`python main.py ingest`
- builds/rebuilds `data/experience_bank.json`.

Normal tailoring:
- loads existing bank;
- builds it only if missing.

### Malformed existing bank

If an existing bank is malformed, fail clearly and instruct the user to run `ingest`.

Do **not** silently rebuild it.

**Why:** Silent rebuilding could unexpectedly change candidate context and make debugging harder.

---

## 2026-09-30 — Safe Bank Replacement

**Decision:** A newly generated experience bank must be fully generated and validated before it replaces the existing bank.

Implementation uses a temporary file + atomic replacement.

**Why:** Failed extraction or validation should never destroy a previously valid bank.

---

## 2026-09-30 — Grounding Rule

### Candidate-provided context defines the factual source set

Resume generation and revisions should draw only from:
- candidate-provided resume;
- experience bank;
- other supplied candidate context.

Reviewer suggestions may recommend better presentation of existing material but should not introduce unsupported qualifications or technologies.

**Why:** A reviewer noticing a JD mentions a technology should not be enough to cause that technology to appear in the resume if it is absent from the source corpus.

---

## 2026-09-30 — Structured Outputs

### Use Pydantic validation

**Decision:** Structured LLM outputs should use Pydantic models with strict validation.

Initial experience-bank schema:
- extra fields forbidden;
- blank strings rejected;
- at least one experience required;
- duplicate `(organization, title)` pairs rejected.

**Why:** Downstream code should not have to guess whether model-generated JSON is valid.

---

## 2026-09-30 — Failure Behavior

**Decision:** Save each completed intermediate artifact as soon as its stage succeeds.

On failure:
- preserve prior completed artifacts;
- stop at the failed stage;
- give a clear user-facing error;
- use bounded SDK retries for transient failures.

Do not build an elaborate checkpoint/resume engine in V1.

---

## 2026-09-30 — API / LLM Integration

### Use the OpenAI SDK directly

**Decision:** No agent framework is required for V1.

Use:
- shared `utils/llm.py`;
- one public function per specialized component;
- clear system instructions per component;
- structured outputs where appropriate.

### Model configuration

The model is configured through:

```text
OPENAI_MODEL=...
```

rather than hard-coded throughout the application.

---

## 2026-09-30 — Initial Model Choice

### Use `gpt-6-sol` as the V1 baseline model

**Decision:**

```text
OPENAI_MODEL=gpt-6-sol
```

**Why:**
- strong reasoning for nuanced job-description interpretation;
- strong professional writing/editing;
- better suited to experience selection and reviewer-feedback resolution than a cost-first model;
- gives the project one consistent quality baseline before introducing model routing.

### Why not optimize model-by-model yet?

Using one model first lets us evaluate the pipeline itself without confounding model differences.

After the pipeline works, cheaper models can be benchmarked on selected stages.

A likely future routing experiment:

- Experience Extractor → cheaper model or Sol
- JD Analyst → cheaper model or Sol
- Experience Selector → Sol
- Resume Writer → Sol
- Alignment Reviewer → cheaper model or Sol
- Recruiter Reviewer → cheaper model or Sol
- Final Editor → Sol
- User Revision Agent → Sol

The goal would be to reduce cost without degrading the stages where nuanced reasoning and writing matter most.

---

## 2026-09-30 — Git / Privacy

Private candidate data and generated output should not be committed.

`.gitignore` should cover at minimum:
- `.env`
- Python caches;
- `inputs/*` except `.gitkeep`;
- `data/experience_bank.json`;
- `outputs/*` except `.gitkeep`.

**Why:** Real resume/background material and generated job-specific resumes should remain local.

---

## 2026-09-30 — Implementation Strategy

### Build incrementally

Do not ask Codex to build the whole system in one pass.

First implemented slice:
- project scaffolding;
- `.gitignore`;
- `.env.example`;
- requirements;
- Pydantic schemas;
- shared OpenAI helper;
- file I/O;
- Experience Extractor;
- `python main.py ingest`;
- tests.

Only after that slice is inspected and run successfully should the project proceed to JD analysis and downstream tailoring.

**Why:** Smaller slices are easier to verify, understand, and debug and reduce the risk of owning a large opaque codebase.

---

## 2026-09-30 — Current Implementation Status

Implemented:
- repo scaffolding;
- strict experience-bank schemas;
- OpenAI helper;
- `.md` / `.txt` file handling;
- Experience Extractor;
- safe bank persistence;
- `python main.py ingest`;
- mocked ingestion tests.

Codex reported 11 mocked tests passing.

Not yet implemented:
- JD Analyst;
- Experience Selector;
- Resume Writer;
- Alignment Reviewer;
- Recruiter Reviewer;
- Final Editor;
- User Revision Agent;
- `tailor`;
- `revise`.

---

## Open Questions / Future Decisions

Track these when they become relevant:

- whether the extractor preserves enough detail or compresses too aggressively;
- whether `(organization, title)` remains sufficient as the bank grows;
- whether cheaper models can handle extraction/review without noticeable quality loss;
- how run names should be generated;
- whether exact-diff / surgical-edit tooling is needed for highly constrained revisions;
- whether PDF/DOCX parsing and exact layout checks are worth adding in V2;
- whether retrieval/embeddings become necessary if the candidate corpus expands substantially.

---

## 2026-09-30 — Independent JD Analysis

JD analysis is now implemented and independently testable through `analyze-jd`
before experience selection. The JD Analyst analyzes the job only and intentionally
receives no candidate context. Its `hiring_themes` permits synthesis grounded in
the posting's responsibilities and qualifications, rather than only verbatim
extraction. V1 job-description input is local pasted text only; URL fetching and
web scraping are intentionally deferred to a separate future feature.

---

## 2026-09-30 — Application Run Isolation

V1 isolates each application in its own timestamped run directory, snapshotting
the resume, JD, and validated experience bank used for that application. Downstream
stages should operate from those snapshots rather than mutable global inputs.
The run name is human-readable, while the timestamp separates repeated runs;
an existing directory is never overwritten, including on a timestamp collision.
Run folders and their files provide V1 persistence instead of a database.

`important_language` prioritizes terminology relevant to the role's work, skills,
domain, metrics, and hiring requirements rather than generic company marketing.

---

## 2026-09-30 — Company and Role Run Names

Run names now follow `<company>_<role>`. The first underscore separates the company
from the role; additional role underscores are preserved, allowing names such as
`notion_product-data-scientist` and `twin-health_product_analytics_engineer`.
Both components are required, lowercased, and normalized so spaces and punctuation
become hyphens. This supersedes the earlier all-hyphen naming convention and makes
the company/role boundary visible. Timestamp suffixes and existing run folders
remain unchanged.

---

## 2026-09-30 — Experience Selection Planning Stage

Experience selection is a separate planning stage before resume writing. For V1,
the selector consumes only the run's saved JD analysis and experience bank,
superseding the specification's original-resume input for this stage. Selected
experiences use exact `(organization, title)` pairs, validated deterministically
with surrounding whitespace trimmed and case ignored. No fuzzy reference matching
is used. Priorities remain qualitative (`high`, `medium`, `low`), not numeric.

The selector may emphasize transferable evidence but may not invent unsupported
qualifications or treat adjacent technologies as equivalent. Its resume strategy
explicitly identifies both what to emphasize and what to deemphasize. The configured
model must match the run's recorded model so its metadata remains accurate.

---

## 2026-09-30 — Grounded Resume Drafting

The Resume Writer operates from the application's saved source resume, experience
bank, JD analysis, and selection strategy. The source resume is the structural
baseline, including identity/contact details and actual employment entries. The
bank supplies deeper factual evidence; its records do not imply separate resume
jobs. Only the resume and bank establish candidate facts. Missing JD qualifications
must not become candidate claims through analysis or selection guidance.

Selected experiences are a relevance pool, not a mandatory inclusion list.
High-priority selections receive the strongest consideration, while the writer
retains discretion to produce a concise, coherent resume and retain useful source
material. Markdown drafting targets one-page intent without guaranteeing physical
page layout. Automated review remains a separate later stage rather than being
embedded in the writer.

---

## 2026-09-30 — Recency, Distinctiveness, and Source-Resume Density

V1 uses the source resume's content density as the primary qualitative proxy for
one-page capacity. Exact font, margin, and line-wrap rendering remain deferred.
Recency matters in selection and writing but is not a mechanical ranking rule:
recent work usually anchors the resume, while older experience can add distinct
role-relevant evidence missing from newer work. The writer should avoid unnecessary
compression and underused capacity when supported, relevant content remains,
balancing section depth and evidence diversity without fixed bullet counts or filler.

---

## 2026-09-30 — Upstream Reruns Invalidate Dependent Artifacts

Rerunning an upstream stage invalidates downstream artifacts only after its new
result has fully validated and successfully replaced the previous artifact.
Experience selection may therefore rerun after a draft exists: successful
reselection removes the stale draft and returns the run to `experiences_selected`.
Failed reselection preserves the prior selection, draft, and status. V1 handles
this direct dependency explicitly without a dependency graph or workflow framework.

---

## 2026-09-30 — Entry Bullet Cap and Section Balance

V1 caps each individual experience, project, or leadership entry at 5 bullets to
prevent over-concentration and redundant evidence. The cap is a maximum, not a
target; there is no minimum, and it does not apply to an entire resume section.
Recent and highly relevant experience should still receive the most space when
justified. Section balance remains qualitative: compare each additional bullet's
value with distinct evidence from other entries rather than equalizing counts.
This guardrail supplements existing relevance, recency, distinctiveness, and
source-resume density guidance.

---

## 2026-09-30 — Advisory Alignment Review

The Alignment Reviewer is the primary job-alignment reviewer, assessing the draft
against saved JD analysis and candidate evidence. It may identify unsupported JD
gaps but may not recommend fabricating qualifications. ATS assessment is qualitative
and limited to structure, terminology, and alignment, with no fake scores or
company-specific ATS simulation. Issue severity is qualitative (`high`, `medium`,
`low`). The review is advisory and does not modify the resume.

Successful writer reruns invalidate the downstream alignment review only after
the new draft is saved. Reselection likewise invalidates both the draft and review
after successful selection replacement, preserving the upstream-rerun principle.

---

## 2026-09-30 — Advisory Recruiter Review

Recruiter review is separate from alignment review and focuses on human first-pass
screening, clarity, credibility, and career narrative. It consumes alignment advice
as context, without mechanically repeating it; overlapping feedback must identify
a distinct human-screening consequence. Only the source resume and bank establish
candidate facts. The review remains advisory and never edits the resume or produces
a generic recruiter score. Severity stays qualitative (`high`, `medium`, `low`).

The recruiter output uses `needs_revision`, `strengths`, and structured `issues`,
superseding the specification's earlier concerns/recommended-changes suggestion.
Writer reruns invalidate both reviews; alignment reruns invalidate recruiter review;
reselection invalidates the draft and both reviews. Invalidation occurs only after
the upstream replacement succeeds.


---

## 2026-09-30 — Grounded Final Editing

The Final Editor is the first stage allowed to modify the reviewed resume. It
uses both Alignment and Recruiter reviews as advisory inputs; source-resume and
experience-bank evidence remains authoritative over every reviewer suggestion.
Alignment guides truthful job fit; recruiter guidance informs clarity, ordering,
and human readability. Conflicts are reconciled rather than applied mechanically.
Reviewers remain advisory and separate from editing.

Finalization performs one revision pass, not an autonomous iterative loop, using
the same model recorded in the run. Output is Markdown; finalization reruns replace
the final from the saved draft without versioning. Lightweight identity validation
expects the candidate name alone on the first nonblank source-resume line, allowing
Markdown formatting; full semantic and layout validation remain deferred.
Upstream reruns invalidate stale final output only after successful replacement,
including reselection, draft writing, and either review.

---

## 2026-09-30 — Intentional Source Content Preservation

The source resume is an editorial baseline as well as factual evidence. Brief,
supported baseline/presentation content should generally be preserved unless an
affirmative reason such as space pressure, redundancy, strategy, readability, or
stronger evidence justifies removal. Strategic content remains freely reallocatable.
Omissions in intermediate drafts are not assumed intentional; the Final Editor
reconsiders restoration before choosing a deletion-based structural fix.
Preservation is qualitative rather than rigid protected fields. Interests are
optional examples of baseline content, neither mandatory nor special-cased.

---

## 2026-09-30 — End-to-End Tailoring Orchestration

`tailor` is deterministic orchestration over the six existing application stages,
not an autonomous agent. It creates one new run, persists every intermediate
artifact, and uses one configured model per run. Ingestion remains explicit and
separate; tailoring neither rebuilds the bank nor inspects background freshness.
Failures preserve the partially completed run instead of rolling back successful
stages. Standalone commands remain available for inspection, manual continuation,
and targeted reruns with their existing invalidation rules.


---

## 2026-10-01 — Grounded User Revisions

User revisions are explicit human-in-the-loop edits after finalization. Each request
makes one grounded, targeted editing pass: user editorial intent has high priority
but cannot create unsupported candidate facts. Saved source resume and bank remain
authoritative. Old reviews are omitted from revision context to avoid overriding
intentional changes in emphasis; reviewers are not automatically rerun.

History is linear, saved as `revision_XXX.md`, superseding the earlier illustrative
revision filenames. Prior versions are preserved across revisions; the highest
number is the editable base. `revision_count` tracks successful revisions and must
agree with contiguous file numbering. Successful upstream reruns, including
reselection and finalization, invalidate stale revisions and reset the count only
after replacement succeeds. `tailor` continues stopping at `finalized`.
