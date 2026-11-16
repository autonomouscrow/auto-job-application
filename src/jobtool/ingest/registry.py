"""Which parser handles which kind of source, and how to detect the kind."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from jobtool.config import SourceConfig
from jobtool.ingest import json_listings, markdown_table, yaml_folder
from jobtool.ingest.base import ParseError, RawListing
from jobtool.ingest.fetch import Fetched, FetchError, fetch_file, fetch_folder

PARSERS = {
    "json": json_listings,
    "yaml-folder": yaml_folder,
    "markdown-table": markdown_table,
}


def has_closed_marker(parser: str) -> bool:
    return PARSERS[parser].HAS_CLOSED_MARKER


def fetch(source: SourceConfig, etag: str | None = None) -> Fetched:
    if source.parser not in PARSERS:
        raise FetchError(f"unknown parser '{source.parser}'")
    if source.parser == "yaml-folder":
        return fetch_folder(source.repo, source.branch, source.path, etag)
    return fetch_file(source.repo, source.branch, source.path, etag)


def parse(
    parser: str, content: str | dict[str, str], today: date | None = None
) -> list[RawListing]:
    if parser == "json":
        return json_listings.parse(content)  # type: ignore[arg-type]
    if parser == "yaml-folder":
        return yaml_folder.parse(content)  # type: ignore[arg-type]
    if parser == "markdown-table":
        return markdown_table.parse(content, today)  # type: ignore[arg-type]
    raise ParseError(f"unknown parser '{parser}'")


@dataclass
class Detection:
    source: SourceConfig
    listings: list[RawListing]


# Tried in order: the most structured format first.
CANDIDATES = [
    ("json", ".github/scripts/listings.json", ("dev", "main", "HEAD")),
    ("json", "listings.json", ("HEAD",)),
    ("yaml-folder", "data", ("HEAD",)),
    ("markdown-table", "README.md", ("HEAD",)),
]


def parse_repo_url(text: str) -> str:
    """Accepts 'owner/name' or any github.com URL and returns 'owner/name'."""
    text = text.strip().removesuffix(".git").rstrip("/")
    if "github.com" in text:
        text = text.split("github.com", 1)[1].lstrip(":/")
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise ValueError("expected a GitHub repository such as owner/name")
    return f"{parts[0]}/{parts[1]}"


def detect(repo: str) -> Detection:
    """Find the first format that yields listings for a repository."""
    problems: list[str] = []
    for parser, path, branches in CANDIDATES:
        for branch in branches:
            source = SourceConfig(repo=repo, branch=branch, path=path, parser=parser)
            try:
                fetched = fetch(source)
                listings = parse(parser, fetched.content or "")
            except (FetchError, ParseError) as exc:
                problems.append(f"{parser} at {branch}/{path}: {exc}")
                continue
            if listings:
                return Detection(source, listings)
    raise ParseError("no supported format found. Tried: " + "; ".join(problems))
