"""Linear revision history checks for one saved application run."""

from pathlib import Path
import re


def revision_files(run_path: Path) -> list[Path]:
    return sorted(run_path.glob("revision_*.md"))


def revision_paths(run_path: Path, count: int, status: str) -> tuple[Path, Path]:
    files = revision_files(run_path)
    numbers = []
    for path in files:
        match = re.fullmatch(r"revision_([0-9]+)\.md", path.name)
        if not match or not path.is_file() or path.is_symlink():
            raise ValueError(f"Unsafe revision file: {path}")
        number = int(match[1])
        if number < 1 or path.name != f"revision_{number:03d}.md":
            raise ValueError(f"Invalid revision filename: {path}")
        numbers.append(number)
    if sorted(numbers) != list(range(1, count + 1)):
        raise ValueError("Revision files and revision_count are inconsistent (missing files, gaps, or unexpected revisions).")
    if (status == "finalized" and count != 0) or (status == "revised" and count == 0):
        raise ValueError("Run status and revision_count are inconsistent.")
    latest = run_path / (f"revision_{count:03d}.md" if count else "final_resume.md")
    target = run_path / f"revision_{count + 1:03d}.md"
    if target.exists() or target.is_symlink():
        raise ValueError(f"Revision target already exists: {target}")
    return latest, target
