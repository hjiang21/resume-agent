# Resume Agent — V1 Product Specification

## 1. Project Goal

Build a Python application that tailors a candidate's resume to a specific job description using:

* the candidate's current resume,
* the target job description,
* and optionally additional background documents containing richer career context.

The system should use several specialized LLM components to:

1. build or load a reusable experience bank,
2. analyze the job description,
3. identify the candidate experiences most relevant to the job,
4. draft a tailored resume,
5. review the draft from both job-alignment and recruiter perspectives,
6. revise the draft into an initial final resume,
7. allow the user to provide iterative natural-language feedback for further revision.

The V1 should prioritize clarity, inspectability, and ease of debugging over complex agent autonomy.

The system should work with only:

* a resume, and
* a job description.

Optional background documents should improve the quality and breadth of tailoring but should not be required.

---

# 2. Core Design Principles

## 2.1 Minimum input should be sufficient

A user must be able to use the system with only:

* `resume.md` or `resume.txt`
* `job_description.md` or `job_description.txt`

The application must not require a manually created experience bank.

If no experience bank exists, the system should create one automatically from the resume and any optional background documents.

---

## 2.2 Additional context should improve results

Users may optionally provide background documents such as:

* career notes,
* work-experience narratives,
* interview-preparation documents,
* project notes,
* past resumes,
* project descriptions,
* personal career summaries.

These documents should be treated as additional candidate context when constructing the experience bank.

The application should still function normally if the optional background directory is empty.

---

## 2.3 The experience bank should contain context, not job-specific framing

The experience bank should capture what is contained in the candidate's source material.

It should not attempt to pre-classify each experience for different job types.

For example, the experience bank should not contain fields such as:

* `product_data_science_angle`
* `consulting_angle`
* `ats_keywords`
* `best_resume_bullet`
* `fintech_relevance`

Those interpretations should be made dynamically later based on the target job description.

---

## 2.4 Specialized components should have distinct responsibilities

Each LLM component should have one clear job.

Do not have one giant model call perform the entire workflow.

The components should be:

1. Experience Extractor
2. JD Analyst
3. Experience Selector
4. Resume Writer
5. Job Alignment / ATS Reviewer
6. Recruiter Reviewer
7. Final Editor
8. User Revision Agent

---

## 2.5 V1 orchestration should be deterministic Python

The workflow should be controlled explicitly in normal Python code.

Do not implement an autonomous manager/orchestrator agent in V1.

Python should determine:

* which component runs,
* in what order,
* which outputs are passed to which downstream components.

Example conceptual flow:

```python
resume = load_resume()
jd = load_job_description()

experience_bank = load_or_build_experience_bank()

jd_analysis = analyze_jd(jd)

selection = select_experiences(
    resume=resume,
    experience_bank=experience_bank,
    jd_analysis=jd_analysis
)

draft = write_resume(
    resume=resume,
    jd=jd,
    jd_analysis=jd_analysis,
    selection=selection
)

alignment_review = review_alignment(
    jd=jd,
    jd_analysis=jd_analysis,
    draft=draft
)

recruiter_review = review_as_recruiter(
    jd=jd,
    draft=draft
)

final_resume = revise_resume(
    draft=draft,
    jd_analysis=jd_analysis,
    selection=selection,
    alignment_review=alignment_review,
    recruiter_review=recruiter_review
)
```

---

# 3. Supported Input Formats

V1 should initially support:

* `.txt`
* `.md`

Do not add PDF or DOCX parsing until the text/Markdown workflow works end-to-end.

The resume input should contain the full logical resume, not just bullet points.

For example:

```markdown
# Jane Doe

San Francisco, CA | email | LinkedIn | GitHub

## EDUCATION

### University Name
M.S. Data Science | December 2025

## EXPERIENCE

### Company Name
Associate | 2025–Present

- Built ...
- Analyzed ...
- Developed ...

## PROJECTS

### Project Name
- ...
```

The model should understand:

* resume sections,
* organization names,
* role titles,
* dates,
* bullets,
* education,
* skills,
* projects,
* section ordering.

V1 does not need to understand exact physical page geometry such as:

* font size,
* margins,
* precise line wrapping,
* exact page count,
* exact visual spacing.

Those can be added later with DOCX/PDF rendering.

---

# 4. Suggested Project Structure

Use a clean, simple structure such as:

```text
resume-agent/
│
├── SPEC.md
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
│
├── main.py
│
├── agents/
│   ├── experience_extractor.py
│   ├── jd_analyst.py
│   ├── experience_selector.py
│   ├── resume_writer.py
│   ├── alignment_reviewer.py
│   ├── recruiter_reviewer.py
│   ├── final_editor.py
│   └── user_revision.py
│
├── utils/
│   ├── file_io.py
│   ├── paths.py
│   └── llm.py
│
├── inputs/
│   ├── resume.md
│   ├── job_description.md
│   └── background/
│
├── data/
│   └── experience_bank.json
│
└── outputs/
```

Exact filenames may change if there is a good reason, but the architecture should remain simple and understandable.

Avoid unnecessary abstraction.

---

# 5. Experience Bank

## 5.1 Purpose

The experience bank is a reusable representation of the candidate's career and project context.

It should be created from:

* the current resume,
* plus all optional supported files in `inputs/background/`.

The experience bank should persist between job applications so it does not need to be rebuilt every time.

---

## 5.2 Schema

Each experience should contain only:

```json
{
  "organization": "Berkeley Research Group",
  "title": "Retail Pilot / Store Matching",
  "content": "Detailed narrative containing the available context about this experience."
}
```

Do not add additional schema fields in V1 unless they are necessary for correct operation.

In particular, do not require:

* experience IDs,
* source-document provenance,
* skills lists,
* job-family labels,
* pre-generated resume bullets,
* pre-generated framing.

The experience bank is intended to remain lightweight.

---

## 5.3 Experience Extractor behavior

The Experience Extractor should:

* read the resume,
* read every supported file in the optional background directory,
* identify distinct career/project experiences,
* consolidate duplicate references to the same experience,
* retain useful details from longer source documents,
* create concise but sufficiently detailed narrative entries.

The extractor should favor preserving useful context over aggressively compressing everything.

The purpose is to create a rich internal knowledge base that downstream components can reason over.

---

## 5.4 Persistence

If `data/experience_bank.json` already exists, the normal tailoring workflow should load it rather than automatically rebuild it.

Provide a separate command or mode to build/rebuild the experience bank.

Example:

```bash
python main.py ingest
```

The exact CLI syntax may differ if there is a cleaner implementation.

---

# 6. JD Analyst

## 6.1 Input

* full job description

## 6.2 Responsibility

Analyze the job description independently of the candidate.

The JD Analyst should identify both explicit requirements and broader hiring themes.

## 6.3 Output

Use structured JSON.

Suggested structure:

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

`hiring_themes` should capture broader patterns such as:

* experimentation,
* cross-functional partnership,
* product judgment,
* technical ownership,
* stakeholder communication,
* operational rigor.

The JD Analyst should not merely extract keywords.

---

# 7. Experience Selector

## 7.1 Inputs

* original resume
* experience bank
* JD analysis

## 7.2 Responsibility

Determine which candidate experiences best support the target role.

The Experience Selector should connect the candidate's available experience to the needs identified by the JD Analyst.

It should determine:

* which experiences are most relevant,
* why they are relevant,
* which themes should be emphasized,
* which less-relevant content should be deprioritized.

This is where job-specific framing should occur.

---

## 7.3 Output

Use structured JSON.

Suggested structure:

```json
{
  "selected_experiences": [
    {
      "organization": "",
      "title": "",
      "relevance": [],
      "priority": "high"
    }
  ],
  "resume_strategy": {
    "emphasize": [],
    "deemphasize": []
  }
}
```

Priority values may be simple values such as:

* `high`
* `medium`
* `low`

Do not introduce numeric scoring unless it provides clear value.

---

# 8. Resume Writer

## 8.1 Inputs

* original resume
* original job description
* JD analysis
* selected experiences
* resume strategy
* relevant experience-bank content

## 8.2 Responsibility

Produce the first tailored resume draft.

The Resume Writer should:

* preserve the candidate's overall resume structure unless there is a strong reason to modify it,
* select and reorder content where useful,
* tailor language to the target job,
* highlight the most relevant experiences,
* preserve important metrics,
* use concise resume-style writing,
* avoid unnecessarily verbose bullets,
* avoid blindly stuffing keywords,
* maintain natural human-readable language.

The output should be a complete resume in Markdown.

Save as:

```text
draft_resume.md
```

---

# 9. Reviewer 1 — Job Alignment / ATS Reviewer

## 9.1 Importance

This is the primary reviewer.

Its feedback should have more influence on the Final Editor than the Recruiter Reviewer's feedback.

Do not implement a fake numeric weighting such as 70/30 in V1.

Instead, encode the priority explicitly in the Final Editor instructions.

---

## 9.2 Inputs

* original job description
* JD analysis
* draft resume

## 9.3 Responsibility

Evaluate how well the resume communicates alignment with the target role and whether relevant information is easy for automated or structured screening systems to identify.

It should review:

* whether major JD requirements are represented,
* whether important terminology is reflected naturally,
* whether highly relevant experience is underemphasized,
* whether useful technical skills are easy to identify,
* whether terminology mismatches obscure genuine alignment,
* whether standard resume sections are understandable,
* whether important content is buried,
* whether less-relevant content is occupying valuable space.

Do not claim to simulate a specific company's ATS.

Do not generate a fabricated ATS pass percentage or score.

---

## 9.4 Output

Use structured JSON.

Suggested structure:

```json
{
  "needs_revision": true,
  "strengths": [],
  "issues": [
    {
      "severity": "high",
      "issue": "",
      "recommendation": ""
    }
  ]
}
```

Possible severity values:

* `high`
* `medium`
* `low`

---

# 10. Reviewer 2 — Recruiter Reviewer

## 10.1 Importance

This is a secondary reviewer.

Its purpose is to catch human-readability and positioning problems that the Job Alignment Reviewer may miss.

Its feedback should inform the Final Editor but should not override important job-alignment improvements unless there is a clear conflict in readability or clarity.

---

## 10.2 Inputs

* original job description
* draft resume

## 10.3 Responsibility

Review the resume as a recruiter performing an initial screen.

Focus on:

* whether the candidate's fit is immediately understandable,
* whether strongest qualifications are easy to notice,
* whether bullet points are clear and readable,
* whether the candidate's level and trajectory are understandable,
* whether key accomplishments are buried,
* whether bullets show meaningful impact,
* whether parts of the resume feel confusing, dense, or unfocused,
* what would make the recruiter hesitate during an initial screen.

The recruiter reviewer should not primarily behave like a keyword checker.

---

## 10.4 Output

Use structured JSON.

Suggested structure:

```json
{
  "strengths": [],
  "concerns": [],
  "recommended_changes": []
}
```

---

# 11. Final Editor

## 11.1 Inputs

* draft resume
* JD analysis
* experience selection / resume strategy
* Job Alignment / ATS review
* Recruiter review
* relevant experience-bank context

## 11.2 Responsibility

Produce the initial final tailored resume.

The Final Editor should resolve feedback rather than blindly applying every suggestion.

Priority rules:

1. Treat Job Alignment / ATS feedback as the primary optimization objective.
2. Treat Recruiter feedback as a secondary constraint focused on clarity, readability, and human persuasion.
3. If reviewer suggestions conflict, prefer the option that preserves strong job alignment while remaining reasonably concise and readable.
4. Do not make unnecessary edits to portions of the resume that are already strong.
5. Preserve important metrics and evidence where possible.
6. Avoid making the final resume substantially more verbose than the draft.

Only one automatic review/revision cycle should occur in V1.

Do not implement endless self-review loops.

Output:

```text
final_resume.md
```

---

# 12. Human-in-the-Loop Revision Mode

## 12.1 Purpose

After the automated tailoring pipeline produces `final_resume.md`, the user should be able to provide natural-language feedback and request targeted changes.

This is a core V1 feature.

Examples of valid user feedback:

* "Bullet 3 is too long. Shorten it without losing the metric."
* "Move Education below Experience."
* "This version overemphasizes modeling. Make it more analytics-focused."
* "Keep the first BRG bullet exactly as-is."
* "The second bullet sounds too technical."
* "Emphasize the $4.6M result more."
* "Do not change anything except the third bullet."
* "This section ordering does not make sense. Use Experience, Projects, Education, Skills."
* "Shorten all bullets so they are more likely to fit within two lines."

The system should accept ordinary natural-language feedback rather than requiring rigid commands.

---

## 12.2 Revision Agent inputs

* current final resume
* user feedback
* target JD
* JD analysis
* experience bank or relevant experience context

## 12.3 Revision behavior

The User Revision Agent should:

* follow the user's requested edits,
* preserve unaffected content where possible,
* make only the changes necessary to satisfy the feedback,
* understand both local and strategic edits,
* respect explicit preservation instructions such as "do not change anything else."

Do not automatically rerun the full automated review pipeline after every user revision.

User-directed iteration should remain lightweight.

---

## 12.4 CLI interaction

Provide a revision mode such as:

```bash
python main.py revise
```

The application may then prompt:

```text
Enter feedback:
>
```

The user enters natural-language feedback.

The revised resume should be saved as a new file or version rather than silently overwriting history.

For example:

```text
final_resume.md
revised_resume_01.md
revised_resume_02.md
```

A simpler naming scheme is acceptable if it clearly preserves revision history.

---

# 13. Intermediate Outputs and Observability

Every important intermediate artifact should be saved.

For one job application, use an output directory such as:

```text
outputs/<job_name>/
├── jd_analysis.json
├── selected_experiences.json
├── draft_resume.md
├── alignment_review.json
├── recruiter_review.json
├── final_resume.md
└── revised_resume_01.md
```

The exact job-folder naming method can be simple in V1.

Do not hide intermediate model outputs only in memory.

The purpose is to make it easy to diagnose whether a poor final result came from:

* JD analysis,
* experience selection,
* resume writing,
* reviewer feedback,
* or final editing.

---

# 14. CLI / Application Modes

At minimum, support the following conceptual workflows.

## Build or rebuild experience bank

```bash
python main.py ingest
```

Responsibilities:

* read resume,
* read optional background files,
* build experience bank,
* save `data/experience_bank.json`.

---

## Tailor resume

```bash
python main.py tailor
```

Responsibilities:

1. load resume,
2. load JD,
3. load existing experience bank or create one if missing,
4. run JD Analyst,
5. run Experience Selector,
6. run Resume Writer,
7. run Job Alignment Reviewer,
8. run Recruiter Reviewer,
9. run Final Editor,
10. save all outputs.

---

## Revise existing result

```bash
python main.py revise
```

Responsibilities:

1. load the latest final/revised resume,
2. collect natural-language user feedback,
3. run User Revision Agent,
4. save a new revised version.

Exact CLI syntax can differ if Codex proposes a cleaner simple design, but these three workflows must exist.

---

# 15. LLM/API Design

Use the OpenAI API / SDK.

Prefer:

* clear system instructions for each component,
* structured outputs where JSON is expected,
* reusable helper functions for model calls,
* simple configuration of the model used.

Avoid duplicating API boilerplate across every agent module.

A shared utility such as:

```text
utils/llm.py
```

should manage common model invocation behavior where appropriate.

Do not over-engineer an abstraction framework.

---

# 16. Error Handling

V1 should handle basic failure cases clearly.

Examples:

* missing resume file,
* missing job description,
* empty background directory,
* malformed or missing experience-bank JSON,
* model/API failure,
* invalid structured model output.

Errors should produce understandable messages.

Do not silently continue when a required artifact is missing.

---

# 17. Configuration and Secrets

The OpenAI API key must not be hardcoded.

Use an environment variable.

Provide:

```text
.env.example
```

Example:

```text
OPENAI_API_KEY=
```

Make sure `.env` is ignored by Git.

---

# 18. Git Safety

Include a `.gitignore`.

At minimum ignore:

```text
.env
__pycache__/
*.pyc
.DS_Store
```

Consider also ignoring private candidate inputs and generated outputs so users do not accidentally commit sensitive career information.

For example:

```text
inputs/*
!inputs/.gitkeep
data/experience_bank.json
outputs/*
!outputs/.gitkeep
```

The repository should not contain personal resume data by default.

Provide sample/mock files later rather than committing real candidate information.

---

# 19. README Requirements

The README should eventually explain:

* what the project does,
* the minimum required inputs,
* optional background context,
* how the experience bank works,
* how to install dependencies,
* how to configure the API key,
* how to run `ingest`,
* how to run `tailor`,
* how to run `revise`,
* what output files are created.

Do not spend significant V1 implementation time polishing the README before the core pipeline works.

---

# 20. V1 Non-Goals

Do not implement the following in V1:

* graphical frontend,
* Streamlit,
* React,
* web deployment,
* vector database,
* embeddings,
* semantic retrieval infrastructure,
* autonomous manager/orchestrator agent,
* dynamic agent handoffs,
* repeated autonomous revision loops,
* LinkedIn scraping,
* automatic job searching,
* automatic job application submission,
* PDF parsing,
* DOCX parsing,
* PDF generation,
* DOCX generation,
* exact resume page-layout validation,
* exact line-wrap measurement,
* company-specific ATS simulation,
* fabricated ATS scores,
* persistent cloud database,
* user authentication.

These may be considered later.

---

# 21. V1 Success Criteria

V1 is successful when a user can:

1. place a Markdown/text resume into the inputs directory,
2. place a Markdown/text job description into the inputs directory,
3. optionally add Markdown/text background documents,
4. build an experience bank automatically,
5. run one tailoring command,
6. inspect all intermediate outputs,
7. receive a complete tailored resume,
8. provide natural-language feedback,
9. receive a targeted revised resume,
10. repeat user-directed revisions without rebuilding the entire pipeline.

The system should remain understandable enough that a developer can trace exactly how the final resume was produced.

---

# 22. Preferred Implementation Philosophy

Keep the first implementation simple.

Before adding any sophisticated framework, prioritize:

* working end-to-end behavior,
* readable Python,
* clear component boundaries,
* visible intermediate outputs,
* structured outputs where appropriate,
* easy debugging,
* easy prompt iteration.

Do not introduce a framework merely because the project involves multiple LLM components.

The architecture should be easy to explain:

> Candidate context is normalized into a reusable experience bank. The job description is independently analyzed. A selector connects the most relevant candidate experiences to the job's needs. A writer creates a tailored draft. Two specialized reviewers evaluate job alignment and human readability. A final editor resolves their feedback, with job alignment given higher priority. The user can then iteratively refine the result through natural-language feedback.

---

# 23. Initial Build Instruction for Codex

Before implementing the full project:

1. Read this entire specification.
2. Propose the simplest Python architecture that satisfies it.
3. Explain the proposed file structure and execution flow.
4. Identify any parts of the specification that are unnecessarily complex for V1.
5. Do not add a frontend, database, vector store, or autonomous orchestration.
6. Do not begin full implementation until the architecture has been reviewed.

Once the architecture is approved, implement the project incrementally and keep each stage runnable.
