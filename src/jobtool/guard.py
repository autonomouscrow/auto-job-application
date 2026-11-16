"""Keep personal information out of the public repository.

The guard reads the applicant's own name, email, and phone number from the
home folder (which is outside the repository) and refuses any commit whose
added lines contain them. It is wired in as a git pre-commit hook.

The terms themselves are never stored in the repository: they are read at
run time from ``<home>/profile/identity.yaml`` and from ``guard.extra_terms``
in ``<home>/config.yaml``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from jobtool.paths import config_path, profile_dir

MIN_TERM_LENGTH = 4


@dataclass(frozen=True)
class Term:
    label: str
    pattern: re.Pattern[str]
    digits_only: bool = False


@dataclass(frozen=True)
class Finding:
    path: str
    line_number: int
    label: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: contains your {self.label}"


def _name_pattern(name: str) -> re.Pattern[str] | None:
    parts = [re.escape(part) for part in re.split(r"\s+", name.strip()) if part]
    if len(parts) < 2:
        return None
    # "Alex Doe", "alex_doe", "alex-doe", "alex.doe", and "alexdoe" all match.
    return re.compile(r"[\s._-]*".join(parts), re.IGNORECASE)


def load_terms(home: Path) -> list[Term]:
    """Build the list of private terms from the home folder."""
    terms: list[Term] = []
    identity_file = profile_dir(home) / "identity.yaml"
    if identity_file.exists():
        identity = yaml.safe_load(identity_file.read_text(encoding="utf-8")) or {}
        name = str(identity.get("name") or "")
        if (pattern := _name_pattern(name)) is not None:
            terms.append(Term("name", pattern))
        email = str(identity.get("email") or "").strip()
        if len(email) >= MIN_TERM_LENGTH:
            terms.append(Term("email address", re.compile(re.escape(email), re.IGNORECASE)))
        digits = re.sub(r"\D", "", str(identity.get("phone") or ""))
        if len(digits) >= 7:
            # Compare on the last ten digits so the country code is optional.
            terms.append(Term("phone number", re.compile(re.escape(digits[-10:])), True))

    config_file = config_path(home)
    if config_file.exists():
        config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        for extra in (config.get("guard") or {}).get("extra_terms") or []:
            extra = str(extra).strip()
            if len(extra) >= MIN_TERM_LENGTH:
                terms.append(Term("private term", re.compile(re.escape(extra), re.IGNORECASE)))
    return terms


def scan_line(line: str, terms: list[Term]) -> list[str]:
    """Return the labels of every term found in one line."""
    hits = []
    digits = None
    for term in terms:
        if term.digits_only:
            if digits is None:
                digits = re.sub(r"\D", "", line)
            if term.pattern.search(digits):
                hits.append(term.label)
        elif term.pattern.search(line):
            hits.append(term.label)
    return hits


def scan_text(path: str, text: str, terms: list[Term]) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for label in scan_line(line, terms):
            findings.append(Finding(path, number, label))
    return findings


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_staged(repo: Path, terms: list[Term]) -> list[Finding]:
    """Scan only the lines that the pending commit would add."""
    diff = _git(repo, "diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff")
    findings: list[Finding] = []
    path = "?"
    line_number = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
            for label in scan_line(path, terms):
                findings.append(Finding(path, 0, f"{label} (in the file name)"))
        elif match := _HUNK.match(line):
            line_number = int(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            for label in scan_line(line[1:], terms):
                findings.append(Finding(path, line_number, label))
            line_number += 1
    return findings


def scan_tracked(repo: Path, terms: list[Term]) -> list[Finding]:
    """Scan every file git tracks. Slower; useful as a one-off audit."""
    findings: list[Finding] = []
    for name in _git(repo, "ls-files", "-z").split("\0"):
        if not name:
            continue
        for label in scan_line(name, terms):
            findings.append(Finding(name, 0, f"{label} (in the file name)"))
        file = repo / name
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(name, text, terms))
    return findings


def author_findings(repo: Path, terms: list[Term]) -> list[Finding]:
    """A commit publishes its author email, so check that too."""
    try:
        email = _git(repo, "config", "user.email").strip()
    except subprocess.CalledProcessError:
        return []
    return [
        Finding("git config user.email", 0, f"{label} (it would be public in the commit)")
        for label in scan_line(email, terms)
        if label != "name"
    ]
