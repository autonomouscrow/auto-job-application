from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobtool.profile import ProfileError, load_profile


def edit(home: Path, name: str, change) -> None:
    path = home / "profile" / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def problems(home: Path) -> str:
    with pytest.raises(ProfileError) as info:
        load_profile(home / "profile")
    return "\n".join(info.value.problems)


def test_example_profile_is_valid_with_no_warnings(example_home: Path) -> None:
    result = load_profile(example_home / "profile")
    assert result.warnings == []
    assert result.profile.identity.name == "Alex Doe"
    assert "exp-acme-2026.uploads" in result.profile.facts()


def test_missing_identity_is_an_error(home: Path) -> None:
    (home / "profile" / "identity.yaml").unlink()
    assert "identity.yaml: required file is missing" in problems(home)


def test_unknown_key_is_rejected(home: Path) -> None:
    edit(home, "experience.yaml", lambda data: data[0].update(organisation="Typo"))
    assert "experience.yaml" in problems(home)


def test_bad_date_is_rejected(home: Path) -> None:
    edit(home, "experience.yaml", lambda data: data[0].update(start="May 2026"))
    assert "YYYY-MM" in problems(home)


def test_end_before_start_is_rejected(home: Path) -> None:
    edit(home, "experience.yaml", lambda data: data[0].update(end="2025-01"))
    assert "is before start" in problems(home)


def test_duplicate_fact_id_is_rejected(home: Path) -> None:
    def change(data):
        data[0]["facts"][1]["id"] = data[0]["facts"][0]["id"]

    edit(home, "experience.yaml", change)
    assert "duplicate id 'exp-acme-2026.uploads'" in problems(home)


def test_fact_id_must_be_prefixed_by_its_entry(home: Path) -> None:
    edit(home, "experience.yaml", lambda data: data[0]["facts"][0].update(id="somewhere.else"))
    assert "must start with 'exp-acme-2026.'" in problems(home)


def test_unknown_evidence_is_rejected(home: Path) -> None:
    edit(home, "skills.yaml", lambda data: data[0].update(evidence=["no-such-fact"]))
    assert "cites unknown evidence 'no-such-fact'" in problems(home)


def test_answer_citing_unknown_fact_is_rejected(home: Path) -> None:
    edit(home, "answers.yaml", lambda data: data[1].update(facts=["no-such-fact"]))
    assert "cites unknown fact 'no-such-fact'" in problems(home)


def test_unexpected_file_is_rejected(home: Path) -> None:
    (home / "profile" / "experiance.yaml").write_text("[]", encoding="utf-8")
    assert "experiance.yaml: unexpected file" in problems(home)


def test_invalid_yaml_is_reported(home: Path) -> None:
    (home / "profile" / "projects.yaml").write_text("- id: [unclosed", encoding="utf-8")
    assert "projects.yaml: not valid YAML" in problems(home)


def test_skill_without_evidence_is_a_warning(home: Path) -> None:
    def change(data):
        data.append({"id": "rust", "name": "Rust", "category": "Languages"})

    edit(home, "skills.yaml", change)
    result = load_profile(home / "profile")
    assert any("'rust' has no evidence" in warning for warning in result.warnings)


def test_undefined_skill_is_a_warning(home: Path) -> None:
    edit(home, "experience.yaml", lambda data: data[0]["facts"][0]["skills"].append("kafka"))
    result = load_profile(home / "profile")
    assert any("'kafka' is used by a fact" in warning for warning in result.warnings)
