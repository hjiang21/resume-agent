"""Validated contracts for source context, analysis, selection, and run metadata."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization: str
    title: str
    content: str

    @field_validator("organization", "title", "content")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Experience fields must not be blank")
        return value


class ExperienceBank(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    experiences: list[Experience] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_titles(self) -> "ExperienceBank":
        keys = [(entry.organization.casefold(), entry.title.casefold())
                for entry in self.experiences]
        if len(keys) != len(set(keys)):
            raise ValueError("Experience titles must be unique within an organization")
        return self


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class JDAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: NonEmptyText
    core_responsibilities: list[NonEmptyText]
    required_qualifications: list[NonEmptyText]
    preferred_qualifications: list[NonEmptyText]
    technical_skills: list[NonEmptyText]
    business_skills: list[NonEmptyText]
    important_language: list[NonEmptyText]
    hiring_themes: list[NonEmptyText]

    @field_validator(
        "core_responsibilities", "required_qualifications", "preferred_qualifications",
        "technical_skills", "business_skills", "important_language", "hiring_themes",
    )
    @classmethod
    def unique_items(cls, values: list[str]) -> list[str]:
        # Preserve the first spelling and source order within each category.
        return unique_text_items(values)


def unique_text_items(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


UniqueTextList = Annotated[list[NonEmptyText], AfterValidator(unique_text_items)]


class SelectedExperience(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization: NonEmptyText
    title: NonEmptyText
    relevance: UniqueTextList = Field(min_length=1)
    priority: Literal["high", "medium", "low"]

    @field_validator("priority", mode="before")
    @classmethod
    def strip_priority(cls, value):
        return value.strip() if isinstance(value, str) else value


class ResumeStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    emphasize: UniqueTextList = Field(min_length=1)
    deemphasize: UniqueTextList


class ExperienceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    selected_experiences: list[SelectedExperience] = Field(min_length=1)
    resume_strategy: ResumeStrategy

    @model_validator(mode="after")
    def unique_references(self) -> "ExperienceSelection":
        keys = [(item.organization.casefold(), item.title.casefold())
                for item in self.selected_experiences]
        if len(keys) != len(set(keys)):
            raise ValueError("Selected experiences must have unique organization/title pairs")
        return self

    def validate_references(self, bank: ExperienceBank) -> None:
        keys = {(item.organization.casefold(), item.title.casefold())
                for item in bank.experiences}
        for item in self.selected_experiences:
            if (item.organization.casefold(), item.title.casefold()) not in keys:
                raise ValueError(
                    f"Selected experience not found in run's bank snapshot: "
                    f"({item.organization!r}, {item.title!r}). Exact references are required."
                )


class AlignmentIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["high", "medium", "low"]
    issue: NonEmptyText
    recommendation: NonEmptyText

    @field_validator("severity", mode="before")
    @classmethod
    def strip_severity(cls, value):
        return value.strip() if isinstance(value, str) else value


class AlignmentReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    needs_revision: bool
    strengths: UniqueTextList
    issues: list[AlignmentIssue]

    @model_validator(mode="after")
    def revision_requires_issue(self) -> "AlignmentReview":
        if self.needs_revision and not self.issues:
            raise ValueError("needs_revision must be false when issues is empty")
        return self


class RecruiterIssue(AlignmentIssue):
    """Same strict issue contract, interpreted for human screening."""


class RecruiterReview(AlignmentReview):
    """Human-screening advice with the same validation rules as alignment review."""

    issues: list[RecruiterIssue]


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: NonEmptyText
    created_at: NonEmptyText
    model: NonEmptyText
    status: Literal["initializing", "initialized", "jd_analyzed", "experiences_selected", "draft_written", "alignment_reviewed", "recruiter_reviewed", "finalized", "revised"]
    revision_count: int = Field(ge=0)

    @field_validator("created_at")
    @classmethod
    def offset_aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("created_at must be an offset-aware ISO timestamp")
        return value
