"""Load the profile folder and check that it is internally consistent."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobtool.profile.models import Profile

# file name -> (profile field, whether the file must exist)
FILES: dict[str, tuple[str, bool]] = {
    "identity.yaml": ("identity", True),
    "education.yaml": ("education", False),
    "experience.yaml": ("experience", False),
    "projects.yaml": ("projects", False),
    "skills.yaml": ("skills", False),
    "certifications.yaml": ("certifications", False),
    "answers.yaml": ("answers", False),
    "stories.yaml": ("stories", False),
}


class ProfileError(Exception):
    """The profile could not be loaded. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class CheckResult:
    profile: Profile
    warnings: list[str] = field(default_factory=list)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError([f"{path.name}: not valid YAML ({exc})"]) from exc


def _format_validation_error(exc: ValidationError, field_to_file: dict[str, str]) -> list[str]:
    problems = []
    for err in exc.errors():
        loc = list(err["loc"])
        file = field_to_file.get(str(loc[0]), str(loc[0])) if loc else "profile"
        where = ".".join(str(part) for part in loc[1:])
        problems.append(f"{file}: {where + ': ' if where else ''}{err['msg']}")
    return problems


def _consistency_errors(profile: Profile) -> list[str]:
    errors: list[str] = []

    ids: list[str] = []
    for entry in profile.entries():
        ids.append(entry.id)
        ids.extend(fact.id for fact in entry.facts)
    ids.extend(item.id for item in profile.certifications)
    ids.extend(item.id for item in profile.answers)
    ids.extend(item.id for item in profile.stories)
    for dup, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate id '{dup}' is used {count} times; ids must be unique")

    for dup, count in Counter(skill.id for skill in profile.skills).items():
        if count > 1:
            errors.append(f"skills.yaml: duplicate skill id '{dup}'")

    for entry in profile.entries():
        for fact in entry.facts:
            if not fact.id.startswith(entry.id + "."):
                errors.append(
                    f"fact '{fact.id}' belongs to '{entry.id}' and must start with '{entry.id}.'"
                )
        if entry.end not in (None, "present") and entry.end < entry.start:
            errors.append(f"'{entry.id}': end {entry.end} is before start {entry.start}")

    known = set(ids)
    for skill in profile.skills:
        for ref in skill.evidence:
            if ref not in known:
                errors.append(f"skills.yaml: skill '{skill.id}' cites unknown evidence '{ref}'")
    for kind, items in (("answers.yaml", profile.answers), ("stories.yaml", profile.stories)):
        for item in items:
            for ref in item.facts:
                if ref not in known:
                    errors.append(f"{kind}: '{item.id}' cites unknown fact '{ref}'")

    return errors


def _warnings(profile: Profile) -> list[str]:
    warnings: list[str] = []
    skill_ids = {skill.id for skill in profile.skills}

    used: set[str] = set()
    for entry in profile.entries():
        used.update(getattr(entry, "skills", []))
        for fact in entry.facts:
            used.update(fact.skills)
    for missing in sorted(used - skill_ids):
        warnings.append(f"skill '{missing}' is used by a fact but is not defined in skills.yaml")

    for skill in profile.skills:
        if not skill.evidence and skill.id not in used:
            warnings.append(
                f"skill '{skill.id}' has no evidence: no fact uses it and none is cited"
            )

    for entry in profile.experience + profile.projects:
        if not entry.facts:
            warnings.append(f"'{entry.id}' has no facts, so nothing from it can be used")
    return warnings


def load_profile(directory: Path) -> CheckResult:
    """Load and validate a profile folder.

    Raises :class:`ProfileError` listing every problem found. Warnings do not
    stop loading; they are returned alongside the profile.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ProfileError([f"profile folder not found: {directory}"])

    known = set(FILES)
    problems = [
        f"{path.name}: unexpected file (expected one of: {', '.join(sorted(known))})"
        for path in sorted(directory.glob("*.y*ml"))
        if path.name not in known
    ]

    data: dict[str, Any] = {}
    field_to_file = {field_name: name for name, (field_name, _) in FILES.items()}
    for name, (field_name, required) in FILES.items():
        path = directory / name
        if not path.exists():
            if required:
                problems.append(f"{name}: required file is missing")
            continue
        try:
            content = _read_yaml(path)
        except ProfileError as exc:
            problems.extend(exc.problems)
            continue
        if content is not None:
            data[field_name] = content
    if problems:
        raise ProfileError(problems)

    try:
        profile = Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(_format_validation_error(exc, field_to_file)) from exc

    errors = _consistency_errors(profile)
    if errors:
        raise ProfileError(errors)
    return CheckResult(profile=profile, warnings=_warnings(profile))
