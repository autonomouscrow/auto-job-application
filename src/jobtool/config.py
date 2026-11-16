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
