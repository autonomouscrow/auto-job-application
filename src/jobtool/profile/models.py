"""Schema for the truth profile.

The profile is a folder of hand-edited YAML files. Every claim that may ever
appear in a generated document is a :class:`Fact` with a stable ID. Generated
text must cite fact IDs, which is what lets a verifier reject anything that
was not written here by the applicant.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ID_PATTERN = r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN, max_length=80)]
SkillId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9+#.-]*$", max_length=60)]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Strict(BaseModel):
    """Base model: unknown keys are errors, so typos are caught early."""

    model_config = ConfigDict(extra="forbid")


def _check_month(value: str | None) -> str | None:
    if value is None or value == "present":
        return value
    if not re.match(MONTH_PATTERN, value):
        raise ValueError("must be YYYY-MM or 'present'")
    return value


class Dated(Strict):
    start: str
    end: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _month(cls, value: str | None) -> str | None:
        return _check_month(value)

    @field_validator("start")
    @classmethod
    def _start_not_present(cls, value: str) -> str:
        if value == "present":
            raise ValueError("start cannot be 'present'")
        return value


class Fact(Strict):
    """One atomic, true statement. The unit that generated text must cite."""

    id: Id
    text: NonEmpty
    skills: list[SkillId] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Link(Strict):
    label: NonEmpty
    url: NonEmpty


class WorkAuthorization(Strict):
    """Stored per country code, for example 'CA', 'US', 'GB'."""

    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    authorized: bool
    requires_sponsorship: bool
    note: str = ""


class Identity(Strict):
    name: NonEmpty
    email: NonEmpty
    phone: str = ""
    location: str = ""
    links: list[Link] = Field(default_factory=list)
    citizenships: list[str] = Field(default_factory=list)
    work_authorization: list[WorkAuthorization] = Field(default_factory=list)


class Education(Dated):
    id: Id
    school: NonEmpty
    degree: NonEmpty
    location: str = ""
    year_of_study: int | None = Field(default=None, ge=1, le=8)
    facts: list[Fact] = Field(default_factory=list)


class Experience(Dated):
    id: Id
    org: NonEmpty
    role: NonEmpty
    location: str = ""
    kind: Literal["internship", "co-op", "part-time", "full-time", "research", "volunteer"] = (
        "internship"
    )
    facts: list[Fact] = Field(default_factory=list)


class Project(Dated):
    id: Id
    name: NonEmpty
    summary: str = ""
    url: str = ""
    skills: list[SkillId] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)


class Skill(Strict):
    id: SkillId
    name: NonEmpty
    category: NonEmpty
    aliases: list[str] = Field(default_factory=list)
    # Fact or entry IDs that prove the skill. A skill with no evidence is a warning.
    evidence: list[Id] = Field(default_factory=list)


class Certification(Strict):
    id: Id
    name: NonEmpty
    issuer: str = ""
    date: str | None = None

    @field_validator("date")
    @classmethod
    def _month(cls, value: str | None) -> str | None:
        return _check_month(value)


class Answer(Strict):
    """A reusable answer to a common application question."""

    id: Id
    question: NonEmpty
    answer: NonEmpty
    # Facts the answer relies on. Empty is allowed for pure preferences.
    facts: list[Id] = Field(default_factory=list)


class Story(Strict):
    """A longer anecdote that cover letters can draw on."""

    id: Id
    title: NonEmpty
    text: NonEmpty
    facts: list[Id] = Field(default_factory=list)


class Profile(Strict):
    identity: Identity
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)
    stories: list[Story] = Field(default_factory=list)

    def entries(self) -> list[Education | Experience | Project]:
        return [*self.education, *self.experience, *self.projects]

    def facts(self) -> dict[str, Fact]:
        """Every fact in the profile, by ID. Assumes IDs were checked as unique."""
        return {fact.id: fact for entry in self.entries() for fact in entry.facts}
