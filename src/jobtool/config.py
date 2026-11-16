"""Private configuration, read from ``<home>/config.yaml`` and ``<home>/sources.yaml``."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from jobtool.paths import config_path


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Term(Lenient):
    earliest_start: date | None = None
    latest_end: date | None = None
    max_duration_months: int | None = None

    def labels(self) -> list[str]:
        """Season labels a matching listing may carry, for example 'Summer 2027'."""
        if self.earliest_start is None:
            return []
        month, year = self.earliest_start.month, self.earliest_start.year
        season = "Winter" if month <= 2 else "Spring" if month <= 4 else "Summer"
        if month >= 9:
            season = "Fall"
        return [f"{season} {year}"]


class Targets(Lenient):
    roles: list[str] = Field(default_factory=lambda: ["swe", "quant"])
    term: Term = Field(default_factory=Term)
    regions: list[str] = Field(default_factory=list)
    working_language: str = "english"
    work_mode_preference: list[str] = Field(default_factory=list)


class Ingest(Lenient):
    # Listings older than this, from sources that never mark roles closed, are filtered.
    max_age_days: int = 60
    # A source link must be missing from this many syncs in a row to count as closed.
    missing_syncs_to_close: int = 2


class Applicant(Lenient):
    # The degree you are studying for: "bachelor", "master", or "phd".
    degree: str = "bachelor"
    year_of_study: int | None = None
    # Expected graduation as "YYYY-MM". Postings that ask for another window are flagged.
    graduation: str | None = None


class Llm(Lenient):
    # Reads job descriptions. A small, cheap model is enough.
    reading_model: str = "claude-haiku-5-5"
    # Writes resumes and cover letters.
    writing_model: str = "claude-opus-5-5"
    # How many jobs at the front of the queue are read before you reach them.
    read_ahead: int = 10
    # A posting is fetched again after this many days, to notice that it closed.
    refresh_days: int = 4


class Resume(Lenient):
    # A built-in style ("navy", "classic") or a folder in <home>/resume/styles/.
    style: str = "navy"
    # Overrides for the style's options: accent, font_size, margin, bold_highlights.
    options: dict = Field(default_factory=dict)
    # "tectonic", "pdflatex", "xelatex" or "lualatex"; None picks the first one installed.
    engine: str | None = None
    projects_title: str = "Projects"


class Tracking(Lenient):
    # Applications you aim to send each week, for the pace on the Stats page.
    weekly_goal: int = 30
    # An application with no news for this many days is brought up as a reminder. 0 turns it off.
    quiet_after_days: int = 14
    # A job in your queue whose deadline is this close is brought up. 0 turns it off.
    deadline_within_days: int = 5
    # While the app is running, sources are synced this often. 0 turns it off.
    sync_every_hours: float = 6


class SourceConfig(Lenient):
    repo: str
    branch: str = "HEAD"
    path: str = "README.md"
    parser: str = "markdown-table"
    default_role: str | None = None
    enabled: bool = True

    @property
    def slug(self) -> str:
        return f"{self.repo}:{self.path}"


class Config(Lenient):
    targets: Targets = Field(default_factory=Targets)
    ingest: Ingest = Field(default_factory=Ingest)
    applicant: Applicant = Field(default_factory=Applicant)
    llm: Llm = Field(default_factory=Llm)
    resume: Resume = Field(default_factory=Resume)
    tracking: Tracking = Field(default_factory=Tracking)
    sources: list[SourceConfig] = Field(default_factory=list)


def load_config(home: Path) -> Config:
    path = config_path(home)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = Config.model_validate(data or {})
    managed = load_sources(home)
    if managed is not None:
        config.sources = managed
    return config


def sources_path(home: Path) -> Path:
    return home / "sources.yaml"


def load_sources(home: Path) -> list[SourceConfig] | None:
    """Sources managed from the Sources page. None when the file does not exist yet."""
    path = sources_path(home)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [SourceConfig.model_validate(item) for item in data]


def save_sources(home: Path, sources: list[SourceConfig]) -> None:
    header = "# Job-list repositories. Managed from the Sources page; safe to edit by hand.\n"
    body = yaml.safe_dump(
        [source.model_dump(exclude_none=True) for source in sources], sort_keys=False
    )
    sources_path(home).write_text(header + body, encoding="utf-8")
