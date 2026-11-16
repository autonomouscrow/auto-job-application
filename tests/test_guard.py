from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobtool import guard


@pytest.fixture
def terms(example_home: Path) -> list[guard.Term]:
    return guard.load_terms(example_home)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "1234+someone@users.noreply.github.com"],
        ["config", "user.name", "someone"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


def stage(repo: Path, name: str, text: str) -> None:
    file = repo / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


def test_terms_come_from_identity_and_config(terms: list[guard.Term]) -> None:
    assert sorted(term.label for term in terms) == [
        "email address",
        "name",
        "phone number",
        "private term",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "Author: Alex Doe",
        "author = 'alex doe'",
        "path/to/alex_doe/resume",
        "ALEX-DOE",
        "alexdoe",
        "contact: Alex.Doe@Example.com",
        "call +1 (555) 010-0199 today",
        "tel:5550100199",
        "github.com/alexdoe-example",
    ],
)
def test_personal_information_is_caught(terms: list[guard.Term], line: str) -> None:
    assert guard.scan_line(line, terms)


@pytest.mark.parametrize(
    "line",
    [
        "def load_profile(directory):",
        "Alexander the Great does not count",
        "phone: 555-010-0100",
        "",
    ],
)
def test_ordinary_lines_pass(terms: list[guard.Term], line: str) -> None:
    assert guard.scan_line(line, terms) == []


def test_staged_commit_with_email_is_refused(repo: Path, terms: list[guard.Term]) -> None:
    stage(repo, "notes.md", "first line\nreach me at alex.doe@example.com\n")
    findings = guard.scan_staged(repo, terms)
    assert {(f.path, f.line_number) for f in findings} == {("notes.md", 2)}
    assert "email address" in {f.label for f in findings}


def test_clean_staged_commit_passes(repo: Path, terms: list[guard.Term]) -> None:
    stage(repo, "src/app.py", "print('hello')\n")
    assert guard.scan_staged(repo, terms) == []


def test_only_added_lines_are_scanned(repo: Path, terms: list[guard.Term]) -> None:
    stage(repo, "notes.md", "by Alex Doe\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    stage(repo, "notes.md", "by Alex Doe\na new harmless line\n")
    assert guard.scan_staged(repo, terms) == []


def test_file_name_is_scanned(repo: Path, terms: list[guard.Term]) -> None:
    stage(repo, "Alex_Doe_Resume.tex", "\\documentclass{article}\n")
    findings = guard.scan_staged(repo, terms)
    assert any("file name" in finding.label for finding in findings)


def test_tracked_files_audit(repo: Path, terms: list[guard.Term]) -> None:
    stage(repo, "a.txt", "nothing here\n")
    stage(repo, "b.txt", "x\ny\nphone 555.010.0199\n")
    findings = guard.scan_tracked(repo, terms)
    assert [(f.path, f.line_number) for f in findings] == [("b.txt", 3)]


def test_personal_author_email_is_flagged(repo: Path, terms: list[guard.Term]) -> None:
    assert guard.author_findings(repo, terms) == []
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "alex.doe@example.com"], check=True
    )
    assert len(guard.author_findings(repo, terms)) == 1


def test_short_or_missing_terms_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "identity.yaml").write_text(
        "name: Al\nemail: ''\nphone: '12'\n", encoding="utf-8"
    )
    assert guard.load_terms(tmp_path) == []
