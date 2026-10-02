"""Small, local run folders with validated source snapshots."""

from datetime import datetime
import json
from pathlib import Path
import re

from pydantic import ValidationError

from utils.file_io import load_experience_bank, read_text_bytes, write_bytes_atomic, write_text_atomic


class RunCreationError(ValueError):
    """An expected failure with the location of an already-created partial run."""

    def __init__(self, message: str, run_path: Path):
        super().__init__(message)
        self.run_path = run_path


def normalize_run_name(name: str) -> str:
    company, separator, role = name.strip().lower().partition("_")
    company = re.sub(r"[^a-z0-9]+", "-", company).strip("-")
    # The first underscore separates company and role; retain role underscores.
    role = re.sub(r"[^a-z0-9_]+", "-", role).strip("-_")
    role = re.sub(r"_+", "_", role)
    if not separator or not company or not re.search(r"[a-z0-9]", role):
        raise ValueError("Run name must use <company>_<role>, with letters or digits in both components.")
    return f"{company}_{role}"


def read_run_sources(resume_path: Path, job_description_path: Path,
                     bank_path: Path) -> dict[str, bytes]:
    """Validate all sources before creating a run; never reread global inputs."""
    resume = read_text_bytes(resume_path)
    if not resume.decode("utf-8-sig").strip():
        raise ValueError(f"Resume is empty: {resume_path}")
    job_description = read_text_bytes(job_description_path)
    if not job_description.decode("utf-8-sig").strip():
        raise ValueError(f"Job description is empty: {job_description_path}")
    try:
        bank = load_experience_bank(bank_path)
    except FileNotFoundError:
        raise ValueError(f"Experience bank not found: {bank_path}. Run `python main.py ingest` first.") from None
    except (ValidationError, UnicodeError):
        raise ValueError(f"Invalid experience bank: {bank_path}. Run `python main.py ingest` to rebuild it.") from None
    except OSError:
        raise ValueError(f"Cannot read experience bank: {bank_path}. Check access or run `python main.py ingest`.") from None
    return {
        "source_resume.md": resume,
        "source_job_description.md": job_description,
        "experience_bank_snapshot.json": (bank.model_dump_json(indent=2) + "\n").encode("utf-8"),
    }


def save_manifest(run_path: Path, manifest: dict) -> None:
    write_text_atomic(run_path / "run.json", json.dumps(manifest, indent=2) + "\n")


def initialize_run(name: str, sources: dict[str, bytes], model: str,
                   output_root: Path) -> tuple[Path, dict]:
    name = normalize_run_name(name)
    created_at = datetime.now().astimezone()
    run_path = output_root / f"{name}-{created_at:%Y%m%d-%H%M%S}"
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        run_path.mkdir()  # Exclusive creation; same-second collisions must never overwrite.
    except FileExistsError:
        raise ValueError(f"Run directory already exists: {run_path}. Retry later or use another name.") from None
    manifest = {
        "name": name,
        "created_at": created_at.isoformat(timespec="seconds"),
        "model": model,
        "status": "initializing",
        "revision_count": 0,
    }
    print(f"Run directory: {run_path}", flush=True)
    try:
        save_manifest(run_path, manifest)
        for filename, content in sources.items():
            write_bytes_atomic(run_path / filename, content)
        manifest["status"] = "initialized"
        save_manifest(run_path, manifest)
    except OSError as exc:
        raise RunCreationError(str(exc), run_path) from exc
    return run_path, manifest
