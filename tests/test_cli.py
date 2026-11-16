from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jobtool.cli import app

runner = CliRunner()


def test_profile_check_passes_on_example(example_home: Path) -> None:
    result = runner.invoke(app, ["profile", "check", "--home", str(example_home), "--strict"])
    assert result.exit_code == 0, result.output
    assert "Profile OK" in result.output


def test_profile_check_lists_problems(home: Path) -> None:
    (home / "profile" / "identity.yaml").unlink()
    result = runner.invoke(app, ["profile", "check", "--home", str(home)])
    assert result.exit_code == 1
    assert "required file is missing" in result.output


def test_missing_home_is_explained(tmp_path: Path) -> None:
    result = runner.invoke(app, ["profile", "check", "--home", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_profile_facts_lists_ids(example_home: Path) -> None:
    result = runner.invoke(app, ["profile", "facts", "--home", str(example_home)])
    assert result.exit_code == 0
    assert "proj-orderbook.engine" in result.output
