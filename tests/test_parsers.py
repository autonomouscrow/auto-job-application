from __future__ import annotations

import io
import json
import tarfile
from datetime import date

import pytest

from jobtool.ingest import json_listings, markdown_table, yaml_folder
from jobtool.ingest.base import ParseError
from jobtool.ingest.fetch import extract_folder
from jobtool.ingest.registry import parse_repo_url

TODAY = date(2026, 11, 16)


def test_json_listings() -> None:
    rows = [
        {"id": "a1", "company_name": "Acme", "title": "Software Engineer Intern",
         "url": "https://acme.test/jobs/1", "locations": ["NYC"], "terms": ["Summer 2027"],
         "active": True, "is_visible": True, "date_posted": 1794700800,
         "sponsorship": "Other", "category": "Software"},
        {"id": "a2", "company_name": "Acme", "title": "Closed Role", "url": "https://acme.test/2",
         "active": False, "is_visible": True},
        {"id": "a3", "company_name": "Acme", "title": "Hidden Role", "url": "https://acme.test/3",
         "active": True, "is_visible": False},
        {"id": "a4", "company_name": "", "title": "No company", "url": "https://acme.test/4"},
    ]  # fmt: skip
    parsed = json_listings.parse(json.dumps(rows))
    assert [(row.row_id, row.active) for row in parsed] == [
        ("a1", True), ("a2", False), ("a3", False)
    ]  # fmt: skip
    assert parsed[0].date_posted == date(2026, 11, 15)
    assert parsed[0].terms == ["Summer 2027"]


def test_json_rejects_other_shapes() -> None:
    with pytest.raises(ParseError):
        json_listings.parse("{}")
    with pytest.raises(ParseError):
        json_listings.parse("not json")


def test_yaml_folder() -> None:
    files = {
        "example-capital.yaml": """
name: "Example Capital"
website: "https://example.test/careers"
locations: "Chicago, NYC"
roles:
  - role_type: "SWE"
    links:
      - url: "https://example.test/1"
        label: "C++"
      - url: "https://example.test/2"
  - role_type: "QR"
    links:
      - url: "https://example.test/3"
""",
        "not-open-yet.yaml": 'name: "Later LLC"\nlocations: "Westport, CT"\nroles: []\n',
        "README.md": "# not data",
    }
    parsed = yaml_folder.parse(files)
    assert [(row.title, row.category) for row in parsed] == [
        ("Software Engineer Intern (C++)", "SWE"),
        ("Software Engineer Intern", "SWE"),
        ("Quantitative Researcher Intern", "QR"),
    ]
    assert parsed[0].locations == ["Chicago", "NYC"]
    assert yaml_folder._locations("Westport, CT") == ["Westport, CT"]
    assert yaml_folder._locations("New York, NY; Boston, MA") == ["New York, NY", "Boston, MA"]


def test_yaml_folder_needs_company_files() -> None:
    with pytest.raises(ParseError):
        yaml_folder.parse({"README.md": "hello"})


MARKDOWN = """
# Some list

| Company | Title | Location | Link | Date Posted |
| ------- | ----- | -------- | ---- | ----------- |
| **[Acme](https://acme.test)** | Software Intern | Paris, France | [Apply](https://acme.test/jobs/1?utm_source=x) | Nov 13 |
| ↳ | Data Intern 🔒 | Berlin, Germany</br>Munich, Germany | [Apply](https://acme.test/jobs/2) | Dec 20 |
| Globex | Quant Intern | London, UK | <a href="https://globex.test/q"><img src="x"></a> | 2026-10-01 |
| No Link Co | Nothing | Rome, Italy | | Nov 1 |

Some text.

| Name | Stars |
| ---- | ----- |
| not a job table | 5 |
"""  # noqa: E501


def test_markdown_table() -> None:
    parsed = markdown_table.parse(MARKDOWN, TODAY)
    assert [(row.company, row.title, row.active) for row in parsed] == [
        ("Acme", "Software Intern", True),
        ("Acme", "Data Intern", False),
        ("Globex", "Quant Intern", True),
    ]
    assert parsed[0].company_url == "https://acme.test"
    assert parsed[1].locations == ["Berlin, Germany", "Munich, Germany"]
    assert parsed[2].url == "https://globex.test/q"
    # No year: the most recent such date that is not in the future.
    assert parsed[0].date_posted == date(2026, 11, 13)
    assert parsed[1].date_posted == date(2025, 12, 20)
    assert parsed[2].date_posted == date(2026, 10, 1)


def test_markdown_without_job_table_is_an_error() -> None:
    with pytest.raises(ParseError):
        markdown_table.parse("| Name | Stars |\n| --- | --- |\n| x | 1 |\n", TODAY)


def test_extract_folder_from_tarball() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in {
            "repo-abc123/README.md": b"top",
            "repo-abc123/data/one.yaml": b"name: One",
            "repo-abc123/data/nested/skip.yaml": b"name: Skip",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    assert extract_folder(buffer.getvalue(), "data") == {"one.yaml": "name: One"}


@pytest.mark.parametrize(
    "text",
    [
        "owner/name",
        "https://github.com/owner/name",
        "https://github.com/owner/name/",
        "https://github.com/owner/name.git",
        "github.com/owner/name/tree/main/data",
    ],
)
def test_parse_repo_url(text: str) -> None:
    assert parse_repo_url(text) == "owner/name"


def test_parse_repo_url_rejects_junk() -> None:
    with pytest.raises(ValueError):
        parse_repo_url("just-a-word")
