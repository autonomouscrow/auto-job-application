"""Parser for job tables written in Markdown inside a README."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from jobtool.ingest.base import ParseError, RawListing

HAS_CLOSED_MARKER = False

_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.IGNORECASE)
_CLOSED = ("🔒", "closed", "~~")

COLUMNS = {
    "company": ("company", "employer", "firm", "name", "organization"),
    "title": ("title", "role", "position", "job"),
    "location": ("location", "locations", "city", "where"),
    "link": ("link", "apply", "application", "posting", "url"),
    "date": ("date", "posted", "added"),
}


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def _text(cell: str) -> str:
    cell = _LINK.sub(r"\1", cell)
    cell = _TAG.sub(" ", cell)
    cell = re.sub(r"[*_`~]+", "", cell)
    return re.sub(r"\s+", " ", cell).strip()


def _first_url(cell: str) -> str | None:
    if match := _LINK.search(cell):
        return match.group(2)
    if match := _HREF.search(cell):
        return match.group(1)
    if match := re.search(r"https?://\S+", cell):
        return match.group(0).rstrip(")>")
    return None


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        words = set(re.findall(r"[a-z]+", _text(cell).lower()))
        for field, names in COLUMNS.items():
            if field not in mapping and words & set(names):
                mapping[field] = index
                break
    return mapping


def parse_date(text: str, today: date) -> date | None:
    """Understands 'Nov 13', 'Nov 13, 2026', '2026-11-13', and '3d' style ages."""
    text = text.strip()
    if not text:
        return None
    if match := re.fullmatch(r"(\d+)\s*(d|day|days|w|wk|weeks?|mo|months?)(\s+ago)?", text, re.I):
        amount, unit = int(match.group(1)), match.group(2).lower()
        days = amount * (1 if unit.startswith("d") else 7 if unit.startswith("w") else 30)
        return today - timedelta(days=days)
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ("%b %d", "%B %d", "%d %b"):
        try:
            parsed = datetime.strptime(f"{text} 2000", f"{fmt} %Y").date()
        except ValueError:
            continue
        # No year given: take the most recent such date that is not in the future.
        candidate = parsed.replace(year=today.year)
        if candidate > today + timedelta(days=1):
            candidate = candidate.replace(year=today.year - 1)
        return candidate
    return None


def parse(text: str, today: date | None = None) -> list[RawListing]:
    today = today or date.today()
    lines = text.splitlines()
    listings: list[RawListing] = []
    tables = 0
    index = 0
    while index < len(lines) - 1:
        if "|" not in lines[index] or not _is_separator(lines[index + 1]):
            index += 1
            continue
        mapping = _map_columns(_cells(lines[index]))
        index += 2
        usable = {"company", "link"} <= mapping.keys() or {"company", "title"} <= mapping.keys()
        if usable:
            tables += 1
        last_company, last_company_url = "", None
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            cells = _cells(lines[index])
            index += 1
            if not usable:
                continue

            def cell(field: str, cells=cells, mapping=mapping) -> str:
                position = mapping.get(field)
                return cells[position] if position is not None and position < len(cells) else ""

            company = _text(cell("company"))
            if company in ("↳", "") and last_company:
                company, company_url = last_company, last_company_url
            else:
                company_url = _first_url(cell("company"))
                last_company, last_company_url = company, company_url
            title = _text(cell("title"))
            url = _first_url(cell("link")) or _first_url(cell("title"))
            if not (company and url):
                continue
            row = " ".join(cells).lower()
            locations = [
                _text(part) for part in _BREAK.split(cell("location")) if _text(part)
            ]
            listings.append(
                RawListing(
                    company=company.replace("🔥", "").strip(),
                    title=(title or "Intern").replace("🔒", "").strip(),
                    url=url,
                    locations=locations,
                    active=not any(marker in row for marker in _CLOSED),
                    date_posted=parse_date(_text(cell("date")), today),
                    company_url=company_url,
                )
            )
    if tables == 0:
        raise ParseError("no table with company and link columns was found")
    return listings
