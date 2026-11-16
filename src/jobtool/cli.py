"""Command line entry point: ``jobs``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobtool import __version__, guard
from jobtool.paths import HomeNotFoundError, find_home, profile_dir, repo_root
from jobtool.profile.loader import ProfileError, load_profile

app = typer.Typer(
    help="A local, human-in-the-loop assistant for internship applications.",
    no_args_is_help=True,
    add_completion=False,
)
profile_app = typer.Typer(help="Work with the truth profile.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

HomeOption = Annotated[
    Path | None,
    typer.Option("--home", help="Home folder with your private data. Defaults to JOBTOOL_HOME."),
]


def _home_or_exit(home: Path | None) -> Path:
    try:
        return find_home(home)
    except HomeNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@profile_app.command("check")
def profile_check(
    home: HomeOption = None,
    strict: Annotated[bool, typer.Option(help="Treat warnings as errors.")] = False,
) -> None:
    """Validate the profile and report every problem found."""
    directory = profile_dir(_home_or_exit(home))
    try:
        result = load_profile(directory)
    except ProfileError as exc:
        typer.secho(f"Profile has {len(exc.problems)} problem(s):", fg=typer.colors.RED)
        for problem in exc.problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(1) from exc

    profile = result.profile
    for warning in result.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
    typer.secho(
        f"Profile OK: {len(profile.experience)} experience, {len(profile.projects)} projects, "
        f"{len(profile.education)} education, {len(profile.facts())} facts, "
        f"{len(profile.skills)} skills, {len(result.warnings)} warning(s).",
        fg=typer.colors.GREEN,
    )
    if strict and result.warnings:
        raise typer.Exit(1)


@profile_app.command("facts")
def profile_facts(home: HomeOption = None) -> None:
    """List every fact with its ID."""
    directory = profile_dir(_home_or_exit(home))
    try:
        profile = load_profile(directory).profile
    except ProfileError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    for entry in profile.entries():
        typer.secho(entry.id, bold=True)
        for fact in entry.facts:
            typer.echo(f"  {fact.id}: {fact.text}")


@app.command("guard")
def guard_command(
    home: HomeOption = None,
    staged: Annotated[bool, typer.Option(help="Scan only what the next commit adds.")] = False,
    require_home: Annotated[
        bool, typer.Option(help="Fail when no home folder is found, instead of skipping.")
    ] = False,
) -> None:
    """Refuse personal information in the repository. Used by the pre-commit hook."""
    try:
        home_dir = find_home(home)
    except HomeNotFoundError as exc:
        if require_home:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc
        typer.secho(
            "guard: no home folder found, so there is nothing to protect. Skipping.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return

    terms = guard.load_terms(home_dir)
    if not terms:
        typer.secho("guard: no private terms found in the home folder.", fg=typer.colors.YELLOW)
        return

    repo = repo_root()
    findings = guard.scan_staged(repo, terms) if staged else guard.scan_tracked(repo, terms)
    findings += guard.author_findings(repo, terms)
    if findings:
        typer.secho("Personal information found. Commit refused:", fg=typer.colors.RED, err=True)
        for finding in findings:
            typer.echo(f"  {finding}", err=True)
        typer.echo(
            "Remove it, or keep the file in your home folder instead of the repository.",
            err=True,
        )
        raise typer.Exit(1)
    scope = "staged changes" if staged else "tracked files"
    typer.secho(f"guard: no personal information in {scope}.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
