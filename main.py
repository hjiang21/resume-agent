"""Deterministic CLI workflows for source ingestion and individual resume stages."""

import argparse
from pathlib import Path
import sys

from dotenv import load_dotenv
from pydantic import ValidationError

from agents.experience_extractor import extract_experiences
from agents.jd_analyst import analyze_jd
from agents.experience_selector import select_experiences
from agents.resume_writer import write_resume
from agents.alignment_reviewer import review_alignment
from agents.recruiter_reviewer import review_recruiter
from agents.final_editor import finalize_resume
from agents.user_revision_agent import revise_resume
from utils.revisions import revision_files, revision_paths
from utils.resume_validation import extract_candidate_name, validate_final_resume
from schemas import AlignmentReview, ExperienceBank, ExperienceSelection, JDAnalysis, RecruiterReview, RunManifest
from utils.file_io import create_text_atomic, load_json_artifact, read_background, read_text, save_experience_bank, write_bytes_atomic, write_text_atomic
from utils.llm import LLM, ModelError
from utils.runs import RunCreationError, initialize_run, normalize_run_name, read_run_sources, save_manifest

ROOT = Path(__file__).resolve().parent


def ingest(resume_path: Path, background_path: Path, bank_path: Path) -> int:
    resume = read_text(resume_path)
    if not resume.strip():
        raise ValueError(f"Resume is empty: {resume_path}")
    background = read_background(background_path)
    print(f"Extracting from the full resume and {len(background)} background document(s)...", flush=True)
    llm = LLM()
    try:
        bank = extract_experiences(resume, background, llm)
        save_experience_bank(bank_path, bank)
    finally:
        llm.close()
    print(f"Saved {len(bank.experiences)} experience(s) to {bank_path}")
    return 0


def analyze_job_description(name: str, resume_path: Path, job_description_path: Path,
                            bank_path: Path) -> Path:
    name = normalize_run_name(name)
    sources = read_run_sources(resume_path, job_description_path, bank_path)
    llm = LLM()
    run_path = None
    try:
        run_path, manifest = initialize_run(name, sources, llm.model, ROOT / "outputs")
        job_description = read_text(run_path / "source_job_description.md")
        print("Analyzing the saved job description...", flush=True)
        analysis = analyze_jd(job_description, llm)
        # Revalidate at the persistence boundary before touching the output.
        validated = JDAnalysis.model_validate(analysis.model_dump())
        write_text_atomic(run_path / "jd_analysis.json", validated.model_dump_json(indent=2) + "\n")
        manifest["status"] = "jd_analyzed"
        save_manifest(run_path, manifest)
    except (ValueError, OSError, ModelError) as exc:
        if run_path is not None:
            message = "JD analysis validation failed." if isinstance(exc, ValidationError) else str(exc)
            raise RunCreationError(message, run_path) from exc
        raise
    finally:
        llm.close()
    print(f"JD analysis complete. Run directory: {run_path}")
    return run_path


def tailor(name: str, resume_path: Path, job_description_path: Path, bank_path: Path) -> int:
    """Run existing artifact-based stages once, preserving progress on failure."""
    run_path = None
    stage = "JD Analyst"
    try:
        print("[1/6] Creating application run and analyzing job description...", flush=True)
        run_path = analyze_job_description(name, resume_path, job_description_path, bank_path)
        for number, (stage, action) in enumerate([
            ("Experience Selector", select_run_experiences),
            ("Resume Writer", write_run_resume),
            ("Alignment Reviewer", review_run_alignment),
            ("Recruiter Reviewer", review_run_recruiter),
            ("Final Editor", finalize_run_resume),
        ], start=2):
            print(f"[{number}/6] {stage}...", flush=True)
            action(run_path)
    except (ValueError, OSError, ModelError) as exc:
        if isinstance(exc, RunCreationError):
            run_path = exc.run_path
        print(f"Tailor failed during {stage}.", file=sys.stderr)
        if run_path is not None:
            print(f"Run preserved at: {run_path}", file=sys.stderr)
            try:
                status = load_json_artifact(run_path / "run.json", RunManifest).status
            except (ValueError, OSError):
                status = "unavailable (run initialization incomplete)"
            print(f"Current status: {status}", file=sys.stderr)
            print("Completed artifacts were preserved.", file=sys.stderr)
        # Pydantic's full error text may contain private candidate data.
        message = "Structured validation failed; existing output was not replaced." if isinstance(exc, ValidationError) else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1
    print(f"Tailoring complete.\nRun: {run_path}\nFinal resume: {run_path / 'final_resume.md'}")
    return 0


def select_run_experiences(run_path: Path) -> int:
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"jd_analyzed", "experiences_selected", "draft_written", "alignment_reviewed", "recruiter_reviewed", "finalized", "revised"}:
        raise ValueError("Experience selection requires a run with completed JD analysis.")
    jd_analysis = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Selecting experiences for run: {run_path}", flush=True)
        selection = select_experiences(jd_analysis, bank, llm)
        validated = ExperienceSelection.model_validate(selection.model_dump())
        validated.validate_references(bank)
        selection_path = run_path / "selected_experiences.json"
        draft_path = run_path / "draft_resume.md"
        review_path = run_path / "alignment_review.json"
        recruiter_path = run_path / "recruiter_review.json"
        previous_selection = selection_path.read_bytes() if selection_path.exists() else None
        previous_draft = draft_path.read_bytes() if draft_path.exists() else None
        final_path = run_path / "final_resume.md"
        previous_revisions = {path: path.read_bytes() for path in revision_files(run_path)}
        previous_final = final_path.read_bytes() if final_path.exists() else None
        previous_review = review_path.read_bytes() if review_path.exists() else None
        previous_recruiter = recruiter_path.read_bytes() if recruiter_path.exists() else None
        write_text_atomic(selection_path, validated.model_dump_json(indent=2) + "\n")
        try:
            final_path.unlink(missing_ok=True)
            # Only a persisted upstream replacement can invalidate the draft.
            draft_path.unlink(missing_ok=True)
            review_path.unlink(missing_ok=True)
            recruiter_path.unlink(missing_ok=True)
            for path in previous_revisions:
                path.unlink()
            manifest.revision_count = 0
            manifest.status = "experiences_selected"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            # Restore prior artifacts if invalidation or status saving fails.
            if previous_selection is None:
                selection_path.unlink(missing_ok=True)
            else:
                write_bytes_atomic(selection_path, previous_selection)
            if previous_draft is not None:
                write_bytes_atomic(draft_path, previous_draft)
            if previous_review is not None:
                write_bytes_atomic(review_path, previous_review)
            if previous_recruiter is not None:
                write_bytes_atomic(recruiter_path, previous_recruiter)
            if previous_final is not None:
                write_bytes_atomic(final_path, previous_final)
            for path, content in previous_revisions.items():
                write_bytes_atomic(path, content)
            raise
    finally:
        llm.close()
    print(f"Saved experience selection to {run_path / 'selected_experiences.json'}")
    return 0


def write_run_resume(run_path: Path) -> int:
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"experiences_selected", "draft_written", "alignment_reviewed", "recruiter_reviewed", "finalized", "revised"}:
        raise ValueError("Resume writing requires a run with completed experience selection.")
    source_resume = read_text(run_path / "source_resume.md")
    if not source_resume.strip():
        raise ValueError(f"Source resume is empty: {run_path / 'source_resume.md'}")
    jd_analysis = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    selection = load_json_artifact(run_path / "selected_experiences.json", ExperienceSelection)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    selection.validate_references(bank)
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Writing resume draft for run: {run_path}", flush=True)
        draft = write_resume(source_resume, jd_analysis, selection, bank, llm)
        if not isinstance(draft, str) or not draft.strip():
            raise ModelError("Resume Writer returned blank text; existing draft was not replaced.")
        draft_path = run_path / "draft_resume.md"
        review_path = run_path / "alignment_review.json"
        recruiter_path = run_path / "recruiter_review.json"
        previous_draft = draft_path.read_bytes() if draft_path.exists() else None
        final_path = run_path / "final_resume.md"
        previous_revisions = {path: path.read_bytes() for path in revision_files(run_path)}
        previous_final = final_path.read_bytes() if final_path.exists() else None
        previous_review = review_path.read_bytes() if review_path.exists() else None
        previous_recruiter = recruiter_path.read_bytes() if recruiter_path.exists() else None
        write_text_atomic(draft_path, draft if draft.endswith("\n") else draft + "\n")
        try:
            final_path.unlink(missing_ok=True)
            review_path.unlink(missing_ok=True)
            recruiter_path.unlink(missing_ok=True)
            for path in previous_revisions:
                path.unlink()
            manifest.revision_count = 0
            manifest.status = "draft_written"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            if previous_draft is None:
                draft_path.unlink(missing_ok=True)
            else:
                write_bytes_atomic(draft_path, previous_draft)
            if previous_review is not None:
                write_bytes_atomic(review_path, previous_review)
            if previous_recruiter is not None:
                write_bytes_atomic(recruiter_path, previous_recruiter)
            if previous_final is not None:
                write_bytes_atomic(final_path, previous_final)
            for path, content in previous_revisions.items():
                write_bytes_atomic(path, content)
            raise
    finally:
        llm.close()
    print(f"Saved resume draft to {run_path / 'draft_resume.md'}")
    return 0


def review_run_alignment(run_path: Path) -> int:
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"draft_written", "alignment_reviewed", "recruiter_reviewed", "finalized", "revised"}:
        raise ValueError("Alignment review requires a run with a completed draft.")
    jd_analysis = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    selection = load_json_artifact(run_path / "selected_experiences.json", ExperienceSelection)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    selection.validate_references(bank)
    source_resume = read_text(run_path / "source_resume.md")
    draft = read_text(run_path / "draft_resume.md")
    if not source_resume.strip():
        raise ValueError(f"Source resume is empty: {run_path / 'source_resume.md'}")
    if not draft.strip():
        raise ValueError(f"Draft resume is empty: {run_path / 'draft_resume.md'}")
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Reviewing alignment for run: {run_path}", flush=True)
        review = review_alignment(draft, source_resume, jd_analysis, selection, bank, llm)
        validated = AlignmentReview.model_validate(review.model_dump())
        review_path = run_path / "alignment_review.json"
        recruiter_path = run_path / "recruiter_review.json"
        final_path = run_path / "final_resume.md"
        previous_revisions = {path: path.read_bytes() for path in revision_files(run_path)}
        previous_final = final_path.read_bytes() if final_path.exists() else None
        previous_review = review_path.read_bytes() if review_path.exists() else None
        previous_recruiter = recruiter_path.read_bytes() if recruiter_path.exists() else None
        write_text_atomic(review_path, validated.model_dump_json(indent=2) + "\n")
        try:
            final_path.unlink(missing_ok=True)
            recruiter_path.unlink(missing_ok=True)
            for path in previous_revisions:
                path.unlink()
            manifest.revision_count = 0
            manifest.status = "alignment_reviewed"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            if previous_review is None:
                review_path.unlink(missing_ok=True)
            else:
                write_bytes_atomic(review_path, previous_review)
            if previous_recruiter is not None:
                write_bytes_atomic(recruiter_path, previous_recruiter)
            if previous_final is not None:
                write_bytes_atomic(final_path, previous_final)
            for path, content in previous_revisions.items():
                write_bytes_atomic(path, content)
            raise
    finally:
        llm.close()
    print(f"Saved alignment review to {run_path / 'alignment_review.json'}")
    return 0


def review_run_recruiter(run_path: Path) -> int:
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"alignment_reviewed", "recruiter_reviewed", "finalized", "revised"}:
        raise ValueError("Recruiter review requires a run with completed alignment review.")
    jd_analysis = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    alignment = load_json_artifact(run_path / "alignment_review.json", AlignmentReview)
    source_resume = read_text(run_path / "source_resume.md")
    draft = read_text(run_path / "draft_resume.md")
    if not source_resume.strip():
        raise ValueError(f"Source resume is empty: {run_path / 'source_resume.md'}")
    if not draft.strip():
        raise ValueError(f"Draft resume is empty: {run_path / 'draft_resume.md'}")
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Reviewing recruiter screening for run: {run_path}", flush=True)
        review = review_recruiter(draft, source_resume, jd_analysis, alignment, bank, llm)
        validated = RecruiterReview.model_validate(review.model_dump())
        review_path = run_path / "recruiter_review.json"
        final_path = run_path / "final_resume.md"
        previous_revisions = {path: path.read_bytes() for path in revision_files(run_path)}
        previous_final = final_path.read_bytes() if final_path.exists() else None
        previous_review = review_path.read_bytes() if review_path.exists() else None
        write_text_atomic(review_path, validated.model_dump_json(indent=2) + "\n")
        try:
            final_path.unlink(missing_ok=True)
            for path in previous_revisions:
                path.unlink()
            manifest.revision_count = 0
            manifest.status = "recruiter_reviewed"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            if previous_review is None:
                review_path.unlink(missing_ok=True)
            else:
                write_bytes_atomic(review_path, previous_review)
            if previous_final is not None:
                write_bytes_atomic(final_path, previous_final)
            for path, content in previous_revisions.items():
                write_bytes_atomic(path, content)
            raise
    finally:
        llm.close()
    print(f"Saved recruiter review to {run_path / 'recruiter_review.json'}")
    return 0


def finalize_run_resume(run_path: Path) -> int:
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"recruiter_reviewed", "finalized", "revised"}:
        raise ValueError("Final editing requires a run with completed recruiter review.")
    jd_analysis = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    selection = load_json_artifact(run_path / "selected_experiences.json", ExperienceSelection)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    selection.validate_references(bank)
    alignment = load_json_artifact(run_path / "alignment_review.json", AlignmentReview)
    recruiter = load_json_artifact(run_path / "recruiter_review.json", RecruiterReview)
    source_resume = read_text(run_path / "source_resume.md")
    draft = read_text(run_path / "draft_resume.md")
    if not source_resume.strip():
        raise ValueError(f"Source resume is empty: {run_path / 'source_resume.md'}")
    if not draft.strip():
        raise ValueError(f"Draft resume is empty: {run_path / 'draft_resume.md'}")
    candidate_name = extract_candidate_name(source_resume)
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Finalizing resume for run: {run_path}", flush=True)
        final = finalize_resume(draft, source_resume, jd_analysis, selection, bank,
                                alignment, recruiter, llm)
        validate_final_resume(final, candidate_name)
        final_path = run_path / "final_resume.md"
        previous_revisions = {path: path.read_bytes() for path in revision_files(run_path)}
        previous_final = final_path.read_bytes() if final_path.exists() else None
        write_text_atomic(final_path, final if final.endswith("\n") else final + "\n")
        try:
            for path in previous_revisions:
                path.unlink()
            manifest.revision_count = 0
            manifest.status = "finalized"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            if previous_final is None:
                final_path.unlink(missing_ok=True)
            else:
                write_bytes_atomic(final_path, previous_final)
            for path, content in previous_revisions.items():
                write_bytes_atomic(path, content)
            raise
    finally:
        llm.close()
    print(f"Saved final resume to {run_path / 'final_resume.md'}")
    return 0


def revise_run_resume(run_path: Path, instruction: str) -> int:
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("Revision instruction must not be blank.")
    manifest = load_json_artifact(run_path / "run.json", RunManifest)
    if manifest.status not in {"finalized", "revised"}:
        raise ValueError("Revision requires a finalized or revised run.")
    final = read_text(run_path / "final_resume.md")
    if not final.strip():
        raise ValueError("final_resume.md must not be blank before revision.")
    latest, target = revision_paths(run_path, manifest.revision_count, manifest.status)
    current = read_text(latest)
    if not current.strip():
        raise ValueError(f"Latest resume is empty: {latest}")
    source = read_text(run_path / "source_resume.md")
    name = extract_candidate_name(source)
    bank = load_json_artifact(run_path / "experience_bank_snapshot.json", ExperienceBank)
    jd = load_json_artifact(run_path / "jd_analysis.json", JDAnalysis)
    selection = load_json_artifact(run_path / "selected_experiences.json", ExperienceSelection)
    selection.validate_references(bank)
    previous_manifest = (run_path / "run.json").read_bytes()
    llm = LLM()
    try:
        if llm.model != manifest.model:
            raise ValueError(f"OPENAI_MODEL must match this run's model ({manifest.model}).")
        print(f"Revising {latest.name} for run: {run_path}", flush=True)
        result = revise_resume(current, instruction, source, bank, jd, selection, llm)
        validate_final_resume(result, name)
        create_text_atomic(target, result if result.endswith("\n") else result + "\n")
        try:
            manifest.revision_count += 1
            manifest.status = "revised"
            save_manifest(run_path, manifest.model_dump())
        except OSError:
            target.unlink()
            write_bytes_atomic(run_path / "run.json", previous_manifest)
            raise
    finally:
        llm.close()
    print(f"Saved revision to {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build candidate context and run individual resume-tailoring stages.")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("ingest", help="Build or explicitly rebuild the experience bank")
    command.add_argument("--resume", type=Path, default=ROOT / "inputs/resume.md")
    command.add_argument("--background", type=Path, default=ROOT / "inputs/background")
    command.add_argument("--bank", type=Path, default=ROOT / "data/experience_bank.json")
    command = commands.add_parser("select-experiences", help="Select experience evidence from an existing run")
    command.add_argument("--run", type=Path, required=True, help="Existing application run directory")
    command = commands.add_parser("write-resume", help="Write a grounded Markdown draft in an existing run")
    command.add_argument("--run", type=Path, required=True, help="Existing application run directory")
    command = commands.add_parser("review-alignment", help="Review draft alignment using saved run evidence")
    command.add_argument("--run", type=Path, required=True, help="Existing application run directory")
    command = commands.add_parser("review-recruiter", help="Review a draft for human screening clarity and credibility")
    command.add_argument("--run", type=Path, required=True, help="Existing application run directory")
    command = commands.add_parser("finalize-resume", help="Edit the reviewed draft once using saved evidence and both reviews")
    command.add_argument("--run", type=Path, required=True, help="Existing application run directory")
    command = commands.add_parser("revise", help="Apply one grounded user edit and save a numbered revision")
    command.add_argument("--run", type=Path, required=True)
    command.add_argument("--instruction", required=True, help="Nonblank editorial instruction")
    for name, help_text in [("analyze-jd", "Analyze a local job description independently"),
                            ("tailor", "Run all six tailoring stages in one new application run")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--name", required=True, help="Application name: <company>_<role>")
        command.add_argument("--resume", type=Path, default=ROOT / "inputs/resume.md")
        command.add_argument("--job-description", type=Path, default=ROOT / "inputs/job_description.md")
        command.add_argument("--bank", type=Path, default=ROOT / "data/experience_bank.json")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        if args.command == "analyze-jd":
            analyze_job_description(args.name, args.resume, args.job_description, args.bank)
            return 0
        if args.command == "tailor":
            return tailor(args.name, args.resume, args.job_description, args.bank)
        if args.command == "select-experiences":
            return select_run_experiences(args.run)
        if args.command == "write-resume":
            return write_run_resume(args.run)
        if args.command == "review-alignment":
            return review_run_alignment(args.run)
        if args.command == "review-recruiter":
            return review_run_recruiter(args.run)
        if args.command == "finalize-resume":
            return finalize_run_resume(args.run)
        if args.command == "revise":
            return revise_run_resume(args.run, args.instruction)
        return ingest(args.resume, args.background, args.bank)
    except ValidationError:
        artifact = {"analyze-jd": "JD analysis", "ingest": "experience-bank",
                    "select-experiences": "Experience selection", "write-resume": "Resume Writer input",
                    "review-alignment": "Alignment review", "review-recruiter": "Recruiter review",
                    "finalize-resume": "Final Editor input", "revise": "User revision input"}[args.command]
        print(f"Error: {artifact} validation failed; existing output was not replaced.", file=sys.stderr)
    except (ValueError, OSError, ModelError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
