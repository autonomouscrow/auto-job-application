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


def _profile_or_none(home: Path):
    try:
        return load_profile(profile_dir(home)).profile
    except ProfileError:
        typer.secho(
            "warning: your profile has problems, so skills are ignored in scoring. "
            "Run 'jobs profile check'.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None


def _report_problem(problem) -> None:
    from jobtool import llm

    typer.secho(f"Claude: {llm.TITLES[problem.kind]}.", fg=typer.colors.RED, err=True)
    if problem.detail:
        typer.echo(f"  {problem.detail}", err=True)
    typer.echo(f"  {llm.ADVICE[problem.kind]}", err=True)


def _read_ahead(home_dir: Path, limit: int | None) -> None:
    from sqlmodel import Session

    from jobtool import db
    from jobtool.config import load_config
    from jobtool.enrich import run

    config = load_config(home_dir)
    with Session(db.open_engine(home_dir)) as session:
        reader = run.make_reader(session, home_dir, config.llm.reading_model)
        if reader is None:
            from jobtool import llm

            alert = llm.current_alert(session, home_dir)
            if alert:
                typer.secho(f"Claude: {alert['title']}.", fg=typer.colors.RED, err=True)
                typer.echo(f"  {alert['advice']}", err=True)
            typer.echo("Checking that postings are still open, without reading them.")
        result = run.enrich_queue(
            session, config=config, profile=_profile_or_none(home_dir), reader=reader,
            limit=limit,
        )  # fmt: skip
    typer.echo(
        f"Fetched {result.fetched} posting(s): {result.read} read, {result.closed} closed, "
        f"{result.unreadable} could not be read, {result.filtered} filtered out."
    )
    if result.problem:
        _report_problem(result.problem)


@app.command()
def sync(
    home: HomeOption = None,
    read: Annotated[
        bool, typer.Option("--read/--no-read", help="Read the postings at the front of the queue.")
    ] = True,
) -> None:
    """Fetch every source and update the queue."""
    from sqlmodel import Session

    from jobtool import db
    from jobtool.config import load_config
    from jobtool.sync import sync_all

    home_dir = _home_or_exit(home)
    with Session(db.open_engine(home_dir)) as session:
        result = sync_all(session, load_config(home_dir), _profile_or_none(home_dir))
    if not result.sources:
        typer.echo("No sources are configured. Add one on the Sources page or in config.yaml.")
    for source in result.sources:
        if source.error:
            typer.secho(f"  {source.slug}: FAILED. {source.error}", fg=typer.colors.RED)
        elif source.unchanged:
            typer.echo(f"  {source.slug}: no change since last sync")
        else:
            typer.echo(
                f"  {source.slug}: {source.rows} rows, {source.new} new, {source.closed} closed"
            )
    typer.secho(f"{result.new} new listing(s).", fg=typer.colors.GREEN)
    if read and result.sources:
        _read_ahead(home_dir, None)


@app.command()
def enrich(
    home: HomeOption = None,
    limit: Annotated[
        int | None, typer.Option(help="How many jobs at the front of the queue to read.")
    ] = None,
) -> None:
    """Fetch and read the postings at the front of the queue, and score them fully."""
    _read_ahead(_home_or_exit(home), limit)


@app.command()
def doctor(home: HomeOption = None) -> None:
    """Check the setup: home folder, profile, database, and the Claude API key."""
    from sqlmodel import Session, func, select

    from jobtool import db, llm
    from jobtool.config import load_config

    home_dir = _home_or_exit(home)
    good = typer.style("ok  ", fg=typer.colors.GREEN)
    bad = typer.style("FAIL", fg=typer.colors.RED)
    typer.echo(f"{good} home folder found")
    try:
        check = load_profile(profile_dir(home_dir))
        typer.echo(f"{good} profile: {len(check.profile.facts())} facts")
    except ProfileError as exc:
        typer.echo(f"{bad} profile: {len(exc.problems)} problem(s). Run 'jobs profile check'.")
    config = load_config(home_dir)
    typer.echo(f"{good} config: {len(config.sources)} source(s)")
    with Session(db.open_engine(home_dir)) as session:
        listings = session.exec(select(func.count()).select_from(db.Listing)).one()
        typer.echo(f"{good} database: {listings} listing(s)")
        if not llm.load_key(home_dir):
            typer.echo(f"{bad} Claude API key: not found")
            typer.echo(f"     {llm.ADVICE[llm.MISSING_KEY]}")
            raise typer.Exit(1)
        typer.echo(f"{good} Claude API key: found (not shown)")
        failed = False
        for label, model in (
            ("reading", config.llm.reading_model),
            ("writing", config.llm.writing_model),
        ):
            problem = llm.check_now(session, home_dir, model)
            if problem is None:
                typer.echo(f"{good} {label} model {model}: reachable")
            else:
                failed = True
                typer.echo(f"{bad} {label} model {model}: {llm.TITLES[problem.kind]}")
                typer.echo(f"     {problem.detail} {llm.ADVICE[problem.kind]}".rstrip())
                if problem.kind != llm.MODEL:
                    break
        rows = session.exec(select(db.Usage)).all()
    from jobtool.resume import render

    engine = render.find_engine(config.resume.engine)
    if engine:
        typer.echo(f"{good} LaTeX: {Path(engine).stem} found, so resumes can be made into PDFs")
    else:
        typer.echo(f"{bad} LaTeX: no engine found. Install Tectonic or MiKTeX to get PDFs.")
        typer.echo("     Resumes are still written as .tex files in the meantime.")
    if rows:
        costs = [llm.cost_usd(row.model, row.input_tokens, row.output_tokens) for row in rows]
        total = sum(cost for cost in costs if cost is not None)
        typer.echo(f"     {len(rows)} Claude call(s) so far, about ${total:.2f}")
    if failed:
        raise typer.Exit(1)


@app.command()
def serve(
    home: HomeOption = None,
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Start the local web app. It only listens on this computer."""
    import threading
    import webbrowser

    import uvicorn

    from jobtool import maintenance
    from jobtool.web.app import create_app

    home_dir = _home_or_exit(home)
    if saved := maintenance.backup(home_dir):
        typer.echo(f"Backed up the database to {saved.name}")
    url = f"http://127.0.0.1:{port}"
    typer.secho(f"Job queue running at {url}  (Ctrl+C to stop)", fg=typer.colors.GREEN)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(
        create_app(home_dir, reading=True, background=True, scheduled=True),
        host="127.0.0.1", port=port, log_level="warning",
    )  # fmt: skip


@app.command()
def export(
    output: Annotated[Path, typer.Argument(help="Where to write the CSV file.")],
    home: HomeOption = None,
) -> None:
    """Write every application to a CSV file."""
    from sqlmodel import Session

    from jobtool import db, maintenance

    home_dir = _home_or_exit(home)
    with Session(db.open_engine(home_dir)) as session:
        output.write_text(maintenance.export_csv(session), encoding="utf-8", newline="")
    typer.echo(f"Wrote {output}")


@app.command()
def backup(home: HomeOption = None) -> None:
    """Copy the database into data/backups (at most once a day)."""
    from jobtool import maintenance

    saved = maintenance.backup(_home_or_exit(home))
    typer.echo(f"Backed up to {saved}" if saved else "Nothing to do: already backed up today.")


TASK_NAME = "jobtool-sync"


def schedule_plan(home_dir: Path, repo: Path, at: str, windows: bool) -> tuple[Path, str, str]:
    """The script to write, its text, and the command that registers a daily sync."""
    log_file = home_dir / "data" / "sync.log"
    if windows:
        script = home_dir / "sync-task.cmd"
        body = (
            "@echo off\r\n"
            f'cd /d "{repo}"\r\n'
            f'uv run jobs sync --home "{home_dir}" >> "{log_file}" 2>&1\r\n'
        )
        command = f'schtasks /Create /F /SC DAILY /ST {at} /TN "{TASK_NAME}" /TR "\\"{script}\\""'
    else:
        script = home_dir / "sync-task.sh"
        body = (
            "#!/bin/sh\n"
            f'cd "{repo}" || exit 1\n'
            f'uv run jobs sync --home "{home_dir}" >> "{log_file}" 2>&1\n'
        )
        hour, minute = at.split(":")
        line = f"{int(minute)} {int(hour)} * * * sh '{script}'"
        command = f'(crontab -l 2>/dev/null; echo "{line}") | crontab -'
    return script, body, command


@app.command()
def schedule(
    home: HomeOption = None,
    at: Annotated[str, typer.Option(help="Time of day to sync, as HH:MM.")] = "08:00",
) -> None:
    """Set up a daily sync that runs even when the app is closed.

    Writes a small script into your home folder and prints the one command that
    registers it with your system's scheduler. Nothing is registered for you.
    """
    import re
    import sys

    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        typer.secho("The time must look like 08:00.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    home_dir = _home_or_exit(home).resolve()
    windows = sys.platform == "win32"
    script, body, command = schedule_plan(home_dir, repo_root(), at, windows)
    script.write_text(body, encoding="utf-8", newline="")
    typer.echo(f"Wrote {script.name} to your home folder. To sync every day at {at}, run:")
    typer.echo("")
    typer.echo(f"  {command}")
    typer.echo("")
    if windows:
        typer.echo(f'To stop it later:  schtasks /Delete /F /TN "{TASK_NAME}"')
    typer.echo("Each run adds its output to data/sync.log in your home folder.")


@app.command()
def today(home: HomeOption = None) -> None:
    """Show what needs attention: deadlines, follow-ups, and this week's pace."""
    from sqlmodel import Session

    from jobtool import db, insights
    from jobtool.config import load_config

    home_dir = _home_or_exit(home)
    config = load_config(home_dir)
    with Session(db.open_engine(home_dir)) as session:
        numbers = insights.stats(session, config)
        typer.echo(f"This week: {numbers.this_week} sent, goal {numbers.goal}.")
        found = insights.reminders(session, config)
        if not found:
            typer.echo("Nothing needs attention today.")
        for item in found:
            typer.echo(f"  [{item.kind}] {item.listing.company}, {item.listing.title}: {item.text}")


if __name__ == "__main__":
    app()
