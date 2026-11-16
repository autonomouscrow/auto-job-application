"""The common shape every parser produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RawListing:
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    sponsorship: str | None = None
    # Degree levels the posting accepts, when the source says, e.g. ["Bachelor's"].
    degrees: list[str] = field(default_factory=list)
    active: bool = True
    date_posted: date | None = None
    # The source's own idea of the role, for example 'Software' or 'QR'.
    category: str | None = None
    row_id: str | None = None
    company_url: str | None = None


class ParseError(Exception):
    """The source could not be understood by this parser."""
