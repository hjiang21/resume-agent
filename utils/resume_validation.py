"""Lightweight identity checks, not a resume parser or factual validator."""

import re


def _plain_text(text: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", text).split())


def extract_candidate_name(source_resume: str) -> str:
    """V1 expects the name alone on the first nonblank line (Markdown allowed)."""
    first_line = next((line.strip() for line in source_resume.splitlines() if line.strip()), "")
    first_line = re.sub(r"^#{1,6}\s+", "", first_line)
    first_line = re.sub(r"\s+#+$", "", first_line)
    name = _plain_text(first_line)
    if not name or not any(character.isalpha() for character in name):
        raise ValueError("Cannot identify candidate name: source_resume.md must begin with the name on its own line.")
    return name


def validate_final_resume(text: str, candidate_name: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Resume output is blank; existing resumes were not replaced.")
    pattern = r"(?<!\w)" + re.escape(candidate_name.casefold()) + r"(?!\w)"
    if not re.search(pattern, _plain_text(text).casefold()):
        raise ValueError("Resume output is missing the candidate name from source_resume.md; existing resumes were not replaced.")
