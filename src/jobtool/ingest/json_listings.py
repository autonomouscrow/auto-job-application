"""Parser for a ``listings.json`` data file, as published by SimplifyJobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from jobtool.ingest.base import ParseError, RawListing

HAS_CLOSED_MARKER = True


def parse(text: str) -> list[RawListing]:
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ParseError("expected a JSON list of listings")

    listings: list[RawListing] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = str(row.get("company_name") or "").strip()
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not (company and title and url):
            continue
        posted = row.get("date_posted")
        listings.append(
            RawListing(
                company=company,
                title=title,
                url=url,
                locations=[str(item) for item in row.get("locations") or []],
                terms=[str(item) for item in row.get("terms") or []],
                sponsorship=row.get("sponsorship") or None,
                degrees=[str(item) for item in row.get("degrees") or []],
                active=bool(row.get("active", True)) and bool(row.get("is_visible", True)),
                date_posted=(
                    datetime.fromtimestamp(posted, tz=UTC).date()
                    if isinstance(posted, int | float)
                    else None
                ),
                category=row.get("category") or None,
                row_id=str(row["id"]) if row.get("id") else None,
                company_url=row.get("company_url") or None,
            )
        )
    if rows and not listings:
        raise ParseError("no rows had company_name, title, and url")
    return listings
