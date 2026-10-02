"""UTF-8 inputs and atomic artifact writes."""

import os
from pathlib import Path
import tempfile
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from schemas import ExperienceBank

SUPPORTED_SUFFIXES = {".md", ".txt"}
Artifact = TypeVar("Artifact", bound=BaseModel)


def load_json_artifact(path: Path, schema: type[Artifact]) -> Artifact:
    """Validate a saved artifact and report its filename without exposing its data."""
    try:
        return schema.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Required run artifact not found: {path}") from None
    except (ValidationError, UnicodeError):
        raise ValueError(f"Invalid run artifact: {path}") from None


def read_text_bytes(path: Path) -> bytes:
    """Read once and validate UTF-8 text while retaining exact snapshot bytes."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported input format: {path}. Use .md or .txt.")
    try:
        content = path.read_bytes()
        content.decode("utf-8-sig")
        return content
    except FileNotFoundError:
        raise ValueError(f"Input file not found: {path}") from None
    except UnicodeError:
        raise ValueError(f"Input must be UTF-8 text: {path}") from None


def read_text(path: Path) -> str:
    # Preserve the previous UTF-8 BOM and universal-newline reading behavior.
    return read_text_bytes(path).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def read_background(directory: Path) -> list[tuple[str, str]]:
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError(f"Background path is not a directory: {directory}")
    documents = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            content = read_text(path)
            if content.strip():
                documents.append((path.relative_to(directory).as_posix(), content))
    return documents


def write_text_atomic(path: Path, content: str) -> None:
    write_bytes_atomic(path, content.encode("utf-8"))


def write_bytes_atomic(path: Path, content: bytes, *, exclusive: bool = False) -> None:
    """Replace only after a complete temporary file is flushed to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Publish the complete file atomically, refusing any existing target.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_text_atomic(path: Path, content: str) -> None:
    """Create a complete new artifact without ever replacing an existing file."""
    write_bytes_atomic(path, content.encode("utf-8"), exclusive=True)


def save_experience_bank(path: Path, bank: ExperienceBank) -> None:
    # Revalidate at the persistence boundary, including mutated/model-constructed objects.
    validated = ExperienceBank.model_validate(bank.model_dump())
    write_text_atomic(path, validated.model_dump_json(indent=2) + "\n")


def load_experience_bank(path: Path) -> ExperienceBank:
    # Malformed data propagates as an error; never rebuild implicitly.
    return ExperienceBank.model_validate_json(path.read_text(encoding="utf-8"))
