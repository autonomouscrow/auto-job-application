"""The shapes a document passes through.

``ResumeDraft`` and ``LetterDraft`` are what the model is asked to return:
ids and wording only. ``ResumeDoc`` and ``LetterDoc`` are what gets rendered:
every name, date, and contact detail in them was copied from the profile by
code, and every sentence has been through the verifier. A style turns a doc
into LaTeX and can be swapped at any time without asking the model again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SectionKey = Literal["education", "experience", "projects"]

# ----- what the model returns -------------------------------------------------


class DraftLine(BaseModel):
    text: str = Field(
        description=(
            "One resume bullet. Plain text; wrap the one or two strongest phrases in "
            "**double asterisks** to bold them. No trailing period is needed."
        )
    )
    fact_ids: list[str] = Field(
        description="The ids of the profile facts this bullet restates. At least one."
    )
    priority: int = Field(
        description="1 = must stay, 2 = should stay, 3 = first to cut if the page overflows."
    )


class DraftBlock(BaseModel):
    entry_id: str = Field(description="The id of the education, experience, or project entry.")
    lines: list[DraftLine]


class DraftSkillRow(BaseModel):
    label: str = Field(description="A short row label such as 'Languages' or 'Data & ML'.")
    skill_ids: list[str] = Field(description="Skill ids from the profile, most relevant first.")


class ResumeDraft(BaseModel):
    section_order: list[SectionKey] = Field(
        description="The order of the three sections. Skills always come last."
    )
    education: list[DraftBlock]
    experience: list[DraftBlock]
    projects: list[DraftBlock]
    skills: list[DraftSkillRow]


class DraftParagraph(BaseModel):
    text: str = Field(description="One paragraph of the letter body, plain text.")
    fact_ids: list[str] = Field(
        description=(
            "The ids of the profile facts behind every claim the paragraph makes about the "
            "applicant. Empty only for a paragraph that makes no such claim."
        )
    )


class LetterDraft(BaseModel):
    paragraphs: list[DraftParagraph] = Field(description="Three or four body paragraphs.")


# ----- what gets rendered -----------------------------------------------------


class Line(BaseModel):
    """One bullet. ``text`` may contain ``**bold**`` spans and nothing else special."""

    text: str
    fact_ids: list[str] = Field(default_factory=list)
    priority: int = 2


class Block(BaseModel):
    """One entry: a school, a job, or a project, with its bullets."""

    entry_id: str
    title: str  # school, employer, or project name
    subtitle: str = ""  # degree or role; empty for projects
    dates: str = ""  # already formatted, for example "Jan. 2026 -- Aug. 2026"
    location: str = ""
    stack: list[str] = Field(default_factory=list)  # projects: skill names shown beside the title
    lines: list[Line] = Field(default_factory=list)


class Section(BaseModel):
    key: SectionKey
    title: str
    blocks: list[Block] = Field(default_factory=list)


class SkillRow(BaseModel):
    label: str
    items: list[str]  # display names
    skill_ids: list[str] = Field(default_factory=list)  # empty for the certifications row


class Contact(BaseModel):
    text: str
    url: str = ""


class Header(BaseModel):
    name: str
    contacts: list[Contact] = Field(default_factory=list)


class ResumeDoc(BaseModel):
    header: Header
    sections: list[Section] = Field(default_factory=list)
    skills_title: str = "Technical Skills"
    skills: list[SkillRow] = Field(default_factory=list)

    def lines(self) -> list[Line]:
        return [line for s in self.sections for block in s.blocks for line in block.lines]

    def skill_ids(self) -> set[str]:
        return {skill_id for row in self.skills for skill_id in row.skill_ids}


class LetterDoc(BaseModel):
    header: Header
    date: str  # already formatted, for example "November 16, 2026"
    company: str
    role: str
    greeting: str = "Dear Hiring Team,"
    paragraphs: list[Line] = Field(default_factory=list)
    closing: str = "Sincerely,"
