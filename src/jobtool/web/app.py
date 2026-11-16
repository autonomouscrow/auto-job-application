"""Local web app: the one-job-at-a-time queue, the lists, the tracker, and sources."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from jobtool import db, maintenance, queue
from jobtool.config import SourceConfig, load_config, save_sources
from jobtool.db import Application, Listing, ListingSource, Source
from jobtool.ingest import registry
from jobtool.ingest.base import ParseError
from jobtool.paths import profile_dir
from jobtool.profile.loader import ProfileError, load_profile
from jobtool.sync import ensure_sources, sync_all

HERE = Path(__file__).parent
LISTS = {
    "set-aside": (
        db.SET_ASIDE,
        "Set aside",
        "Jobs you put off. Work through them or bring one back.",
    ),
    "rejected": (
        db.REJECTED,
        "Rejected",
        "Jobs you turned down. They never return to the queue.",
    ),
    "filtered": (
        db.FILTERED,
        "Filtered out",
        "Removed automatically, with the reason. Restore any that are wrong.",
    ),
}


def create_app(home: Path, engine=None) -> FastAPI:
    engine = engine or db.open_engine(home)
    templates = Jinja2Templates(directory=HERE / "templates")
    app = FastAPI(title="jobtool", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Refuse form posts that come from another website."""
        origin = request.headers.get("origin")
        if request.method == "POST" and origin:
            if origin.split("://", 1)[-1] != request.headers.get("host"):
                return PlainTextResponse("Cross-site request refused", status_code=403)
        return await call_next(request)

    def profile_or_none():
        try:
            return load_profile(profile_dir(home)).profile
        except ProfileError:
            return None

    def render(request: Request, template: str, session: Session, /, **context) -> HTMLResponse:
        context.update(
            counts=queue.counts(session),
            last_sync=db.get_meta(session, "last_sync"),
            message=request.query_params.get("message", ""),
            statuses=db.STATUSES,
        )
        return templates.TemplateResponse(request, template, context)

    def get_listing(session: Session, listing_id: int) -> Listing:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404, "No such job")
        return listing

    def back(request: Request, default: str = "/", message: str = "") -> RedirectResponse:
        target = request.headers.get("referer") or default
        target = target.split("?", 1)[0]
        if message:
            target += f"?message={message}"
        return RedirectResponse(target, status_code=303)

    def sources_for(session: Session, listing: Listing) -> list[str]:
        rows = session.exec(
            select(Source.repo)
            .join(ListingSource, ListingSource.source_id == Source.id)
            .where(ListingSource.listing_id == listing.id)
        ).all()
        return list(rows)

    def card_context(session: Session, listing: Listing | None) -> dict:
        if listing is None:
            return {"job": None}
        repeat = session.get(Listing, listing.repeat_of) if listing.repeat_of else None
        return {
            "job": listing,
            "job_sources": sources_for(session, listing),
            "repeat": repeat,
            "application": session.exec(
                select(Application).where(Application.listing_id == listing.id)
            ).first(),
        }

    # ----- queue ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def main_queue(request: Request):
        with Session(engine) as session:
            listing = queue.next_listing(session, db.MAIN)
            return render(
                request, "queue.html", session, mode="main", **card_context(session, listing)
            )

    @app.get("/set-aside/work", response_class=HTMLResponse)
    def set_aside_queue(request: Request):
        with Session(engine) as session:
            listing = queue.next_listing(session, db.SET_ASIDE)
            return render(
                request, "queue.html", session, mode="set-aside", **card_context(session, listing)
            )

    @app.post("/job/{listing_id}/{action}")
    def act(request: Request, listing_id: int, action: str, note: str = Form("")):
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            if action == "apply":
                queue.start_application(session, listing)
            elif action == "applied":
                queue.mark_applied(session, listing)
            elif action == "set-aside":
                queue.set_aside(session, listing, note.strip())
            elif action == "reject":
                queue.reject(session, listing)
            elif action == "restore":
                queue.back_to_main(session, listing)
            else:
                raise HTTPException(404, "Unknown action")
        return back(request)

    @app.post("/undo")
    def undo(request: Request):
        with Session(engine) as session:
            listing = queue.undo(session)
        return back(request, message="Undone" if listing else "Nothing to undo")

    # ----- lists ---------------------------------------------------------

    @app.get("/list/{name}", response_class=HTMLResponse)
    def show_list(request: Request, name: str):
        if name not in LISTS:
            raise HTTPException(404, "No such list")
        state, title, blurb = LISTS[name]
        with Session(engine) as session:
            jobs = queue.listings_in(session, state, open_only=state == db.SET_ASIDE)
            return render(
                request, "list.html", session, name=name, title=title, blurb=blurb,
                jobs=jobs[:500], total=len(jobs),
            )  # fmt: skip

    @app.get("/job/{listing_id}", response_class=HTMLResponse)
    def job_page(request: Request, listing_id: int):
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            return render(
                request, "job.html", session,
                timeline=queue.timeline(session, listing.id),
                **card_context(session, listing),
            )  # fmt: skip

    # ----- tracker -------------------------------------------------------

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker(request: Request, status: str = ""):
        with Session(engine) as session:
            query = (
                select(Application, Listing)
                .join(Listing, Listing.id == Application.listing_id)
                .order_by(Application.applied_at.desc())
            )
            if status:
                query = query.where(Application.status == status)
            return render(
                request, "tracker.html", session, rows=session.exec(query).all(), status=status
            )

    @app.post("/application/{application_id}")
    def update_application(
        request: Request,
        application_id: int,
        status: str = Form(""),
        notes: str = Form(None),
        next_followup: str = Form(None),
    ):
        with Session(engine) as session:
            application = session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, "No such application")
            if notes is not None:
                application.notes = notes.strip()
            if next_followup is not None:
                try:
                    application.next_followup = (
                        date.fromisoformat(next_followup) if next_followup else None
                    )
                except ValueError as exc:
                    raise HTTPException(400, "Follow-up must be a date") from exc
            session.add(application)
            session.commit()
            if status:
                try:
                    queue.set_status(session, application, status)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
        return back(request, "/tracker")

    @app.get("/export.csv")
    def export():
        with Session(engine) as session:
            body = maintenance.export_csv(session)
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="applications.csv"'},
        )

    # ----- sources -------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        with Session(engine) as session:
            ensure_sources(session, load_config(home))
            session.commit()
            rows = session.exec(select(Source).order_by(Source.id)).all()
            return render(request, "sources.html", session, sources=rows, preview=None)

    @app.post("/sources/preview", response_class=HTMLResponse)
    def preview_source(request: Request, repo: str = Form(...)):
        with Session(engine) as session:
            rows = session.exec(select(Source).order_by(Source.id)).all()
            try:
                detection = registry.detect(registry.parse_repo_url(repo))
                preview = {"source": detection.source, "rows": detection.listings[:8],
                           "total": len(detection.listings)}  # fmt: skip
                error = ""
            except (ParseError, ValueError) as exc:
                preview, error = None, str(exc)
            return render(
                request, "sources.html", session, sources=rows, preview=preview, error=error
            )

    @app.post("/sources/add")
    def add_source(
        repo: str = Form(...),
        branch: str = Form("HEAD"),
        path: str = Form("README.md"),
        parser: str = Form("markdown-table"),
        default_role: str = Form(""),
    ):
        if parser not in registry.PARSERS:
            raise HTTPException(400, "Unknown parser")
        config = load_config(home)
        new = SourceConfig(
            repo=registry.parse_repo_url(repo), branch=branch, path=path, parser=parser,
            default_role=default_role or None,
        )  # fmt: skip
        if new.slug not in {source.slug for source in config.sources}:
            save_sources(home, [*config.sources, new])
        return RedirectResponse("/sources?message=Source+added", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    def toggle_source(source_id: int):
        config = load_config(home)
        with Session(engine) as session:
            row = session.get(Source, source_id)
            if row is None:
                raise HTTPException(404, "No such source")
            for item in config.sources:
                if item.slug == row.slug:
                    item.enabled = not item.enabled
        save_sources(home, config.sources)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sync")
    def sync_now(request: Request):
        with Session(engine) as session:
            result = sync_all(session, load_config(home), profile_or_none())
        failed = sum(1 for source in result.sources if source.error)
        message = f"Synced: {result.new} new" + (f", {failed} source(s) failed" if failed else "")
        return back(request, message=message.replace(" ", "+"))

    return app
