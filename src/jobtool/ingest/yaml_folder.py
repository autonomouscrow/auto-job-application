"""Parser for a folder with one YAML file per company.

This is the layout of the Northwestern Fintech quant list::

    name: "Example Capital"
    locations: "Chicago, NYC"
    roles:
      - role_type: "SWE"
        links:
          - url: "https://example.com/1"
            label: "C++"
"""

from __future__ import annotations

import re

import yaml

from jobtool.ingest.base import ParseError, RawListing

# A link that disappears is the only way this format closes a role.
HAS_CLOSED_MARKER = False

ROLE_NAMES = {
    "QR": "Quantitative Researcher",
    "QT": "Quantitative Trader",
    "QD": "Quantitative Developer",
    "SWE": "Software Engineer",
    "HW": "Hardware Engineer",
    "FPGA": "FPGA Engineer",
    "ML": "Machine Learning Engineer",
}


def _title(role_type: str, label: str | None) -> str:
    name = ROLE_NAMES.get(role_type.upper(), role_type)
    title = f"{name} Intern"
    return f"{title} ({label})" if label else title


def _locations(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]
    parts = [part.strip() for part in text.split(",") if part.strip()]
    # "Westport, CT" is one place; "Chicago, NYC" is two.
    if len(parts) == 2 and re.fullmatch(r"[A-Z]{2}", parts[1]) and parts[1] != "NYC":
        return [text]
    return parts


def parse(files: dict[str, str]) -> list[RawListing]:
    """``files`` maps file name to file content."""
    listings: list[RawListing] = []
    companies = 0
    for name in sorted(files):
        if not name.lower().endswith((".yaml", ".yml")):
            continue
        try:
            data = yaml.safe_load(files[name])
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict) or "roles" not in data:
            continue
        companies += 1
        company = str(data.get("name") or name.rsplit(".", 1)[0]).strip()
        locations = _locations(data.get("locations"))
        for role in data.get("roles") or []:
            if not isinstance(role, dict):
                continue
            role_type = str(role.get("role_type") or "").strip()
            for link in role.get("links") or []:
                url = str((link or {}).get("url") or "").strip()
                if not url:
                    continue
                label = str((link or {}).get("label") or "").strip() or None
                listings.append(
                    RawListing(
                        company=company,
                        title=_title(role_type, label),
                        url=url,
                        locations=locations,
                        category=role_type or None,
                        company_url=str(data.get("website") or "") or None,
                    )
                )
    if companies == 0:
        raise ParseError("no company files with a 'roles' key were found")
    return listings
