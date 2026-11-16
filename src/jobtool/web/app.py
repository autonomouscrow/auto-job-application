"""Local web app: the one-job-at-a-time queue, the lists, the tracker, and sources."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from jobtool import assist, db, insights, llm, maintenance, queue
from jobtool.config import SourceConfig, load_config, save_sources
from jobtool.db import Application, Archetype, JobAnswer, Listing, ListingSource, Source
from jobtool.enrich import run as reading_run
from jobtool.enrich.posting import Getter, http_get
from jobtool.ingest import registry
from jobtool.ingest.base import ParseError
from jobtool.paths import profile_dir
from jobtool.profile.loader import ProfileError, load_profile
from jobtool.resume import documents, render
from jobtool.resume.write import WriteError
from jobtool.sync import ensure_sources, sync_all

HERE = Path(__file__).parent
log = logging.getLogger("jobtool")
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


def create_app(
    home: Path,
    engine=None,
    *,
    reading: bool = False,
    background: bool = False,
    scheduled: bool = False,
    get: Getter = http_get,
    reader_factory: reading_run.ReaderFactory | None = None,
    writer_factory: reading_run.ReaderFactory | None = None,
) -> FastAPI:
    """Build the app.

    ``reading`` makes the queue fetch and read a posting before showing it.
    ``background`` also reads the next few jobs on a worker thread. Both are off
    ``scheduled`` syncs the sources on a timer while the app runs. All are off
    by default so tests never touch the network.
    """
    engine = engine or db.open_engine(home)
    # One posting is fetched at a time, whether for the page or in the background.
    reading_lock = threading.Lock()

    def make_reader(session: Session):
        if reader_factory is not None:
            return reader_factory(session)
        return reading_run.make_reader(session, home, load_config(home).llm.reading_model)

    def make_writer(session: Session):
        """The model that writes resumes and letters, or None while Claude cannot be used."""
        if writer_factory is not None:
            return writer_factory(session)
        return reading_run.make_reader(session, home, load_config(home).llm.writing_model)
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

    def render_page(
        request: Request, template: str, session: Session, /, **context
    ) -> HTMLResponse:
        context.update(
            counts=queue.counts(session),
            last_sync=db.get_meta(session, "last_sync"),
            message=request.query_params.get("message", ""),
            alert=llm.current_alert(session, home),
            statuses=db.STATUSES,
            # How many reminders are waiting on the Today page, for the badge in the menu.
            reminder_count=insights.reminder_count(session, load_config(home)),
        )
        return templates.TemplateResponse(request, template, context)

    def get_listing(session: Session, listing_id: int) -> Listing:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404, "No such job")
        return listing

    def back(
        request: Request, default: str = "/", message: str = "", anchor: str = ""
    ) -> RedirectResponse:
        target = request.headers.get("referer") or default
        target = target.split("#", 1)[0].split("?", 1)[0]
        if message:
            target += f"?message={message}"
        if anchor:
            target += f"#{anchor}"
        return RedirectResponse(target, status_code=303)

    def sources_for(session: Session, listing: Listing) -> list[str]:
        rows = session.exec(
            select(Source.repo)
            .join(ListingSource, ListingSource.source_id == Source.id)
            .where(ListingSource.listing_id == listing.id)
        ).all()
        return list(rows)

    def card_context(session: Session, listing: Listing | None, question: str = "") -> dict:
        if listing is None:
            return {"job": None}
        repeat = session.get(Listing, listing.repeat_of) if listing.repeat_of else None
        read = reading_run.read_state(session, listing)
        requirements = read["requirements"]
        profile = profile_or_none()
        facts = profile.facts() if profile else {}
        return {
            "job": listing,
            # What was fetched from the employer's page: status, error, fetched_at.
            "read": read,
            # The extracted requirements (jobtool.enrich.requirements.Extracted), or None.
            "req": requirements if requirements and requirements.is_job_posting else None,
            # Profile facts the model picked as the best support for this job.
            "relevant_facts": [
                facts[fact_id].text
                for fact_id in (requirements.relevant_fact_ids if requirements else [])
                if fact_id in facts
            ],
            "reading": reading,
            **documents_context(session, listing, requirements, profile),
            "assist": assist_context(session, listing, profile, question),
            "job_sources": sources_for(session, listing),
            "repeat": repeat,
            "application": session.exec(
                select(Application).where(Application.listing_id == listing.id)
            ).first(),
        }

    # ----- resumes and cover letters --------------------------------------

    def report_of(row) -> dict:
        try:
            return json.loads(row.report_json or "{}")
        except ValueError:
            return {}

    def styles() -> list:
        return render.list_styles(home)

    def documents_context(session: Session, listing: Listing, requirements, profile) -> dict:
        """What the job card shows about this job's resume and cover letter."""
        resume = documents.document_for(session, listing.id, documents.RESUME)
        letter = documents.document_for(session, listing.id, documents.LETTER)
        usable = requirements if requirements and requirements.is_job_posting else None
        return {
            # jobtool.db.Document rows, or None when not prepared yet.
            "resume": resume,
            "letter": letter,
            "resume_report": report_of(resume) if resume else {},
            "letter_report": report_of(letter) if letter else {},
            # jobtool.resume.documents.Recommendation: mode, archetype, coverage, missing, reason.
            "recommendation": documents.recommend(session, listing, usable, profile),
            "archetypes": [
                row
                for row in session.exec(select(Archetype).order_by(Archetype.name)).all()
                if row.doc_json
            ],
            "styles": styles(),
            "default_style": load_config(home).resume.style,
        }

    def requirements_of(session: Session, listing: Listing):
        found = reading_run.read_state(session, listing)["requirements"]
        return found if found and found.is_job_posting else None

    def writing_failed(request: Request, exc: Exception) -> RedirectResponse:
        """Send the user back with the reason. Claude problems also show in the banner."""
        if isinstance(exc, llm.LlmProblem):
            text = llm.TITLES[exc.kind]
        else:
            text = str(exc)
        return back(request, message=quote_plus(text))

    def get_archetype(session: Session, archetype_id: int) -> Archetype:
        row = session.get(Archetype, archetype_id)
        if row is None:
            raise HTTPException(404, "No such archetype")
        return row

    def pdf_response(folder: Path, kind: str, extension: str):
        path = documents.pdf_path(folder, profile_or_none(), kind).with_suffix(f".{extension}")
        if not path.exists():
            raise HTTPException(404, "That file has not been made yet")
        media = "application/pdf" if extension == "pdf" else "text/plain; charset=utf-8"
        return FileResponse(
            path, media_type=media, filename=path.name, content_disposition_type="inline"
        )

    @app.get("/archetypes", response_class=HTMLResponse)
    def archetypes_page(request: Request):
        with Session(engine) as session:
            rows = session.exec(select(Archetype).order_by(Archetype.name)).all()
            return render_page(
                request, "archetypes.html", session, archetypes=rows, styles=styles(),
                default_style=load_config(home).resume.style,
                engine_found=render.find_engine(load_config(home).resume.engine),
            )  # fmt: skip

    @app.post("/archetypes")
    def create_archetype(
        name: str = Form(...),
        role_type: str = Form("any"),
        focus: str = Form(""),
        style: str = Form(""),
    ):
        if not name.strip():
            raise HTTPException(400, "Give the archetype a name")
        if role_type not in ("any", "quant", "swe", "adjacent"):
            raise HTTPException(400, "Unknown role type")
        with Session(engine) as session:
            row = documents.create_archetype(
                session, name=name, role_type=role_type, focus=focus, style=style
            )
            return RedirectResponse(f"/archetypes/{row.id}", status_code=303)

    @app.get("/archetypes/{archetype_id}", response_class=HTMLResponse)
    def archetype_page(request: Request, archetype_id: int):
        with Session(engine) as session:
            row = get_archetype(session, archetype_id)
            profile = profile_or_none()
            return render_page(
                request, "archetype.html", session, archetype=row,
                # jobtool.resume.model.ResumeDoc, or None before the first generation.
                doc=documents.archetype_doc(row),
                report=report_of(row),
                # fact id -> the applicant's own text, to show beside each bullet.
                facts={k: fact.text for k, fact in (profile.facts() if profile else {}).items()},
                styles=styles(), default_style=load_config(home).resume.style,
                has_pdf=documents.pdf_path(
                    documents.archetype_dir(home, row), profile, documents.RESUME
                ).exists(),
            )  # fmt: skip

    @app.post("/archetypes/{archetype_id}/{action}")
    def archetype_action(
        request: Request,
        archetype_id: int,
        action: str,
        name: str = Form(""),
        role_type: str = Form(""),
        focus: str = Form(""),
        style: str = Form(""),
    ):
        with Session(engine) as session:
            row = get_archetype(session, archetype_id)
            config, profile = load_config(home), profile_or_none()
            if action == "generate":
                if profile is None:
                    return back(request, message="Fix+your+profile+first:+run+jobs+profile+check")
                writer = make_writer(session)
                if writer is None:
                    return back(request, message="Claude+cannot+be+used+right+now")
                try:
                    documents.generate_archetype(
                        session, home, row, reader=writer, profile=profile, config=config
                    )
                except (llm.LlmProblem, WriteError) as exc:
                    return writing_failed(request, exc)
            elif action == "style":
                documents.restyle_archetype(
                    session, home, row, style=style, profile=profile, config=config
                )
            elif action in ("approve", "unapprove"):
                if action == "approve" and not row.doc_json:
                    raise HTTPException(400, "Generate it first")
                documents.set_approved(session, row, action == "approve")
            elif action == "edit":
                if name.strip():
                    row.name = name.strip()
                if role_type in ("any", "quant", "swe", "adjacent"):
                    row.role_type = role_type
                row.focus = focus.strip()
                session.add(row)
                session.commit()
            elif action == "delete":
                documents.delete_archetype(session, home, row)
                return RedirectResponse("/archetypes", status_code=303)
            else:
                raise HTTPException(404, "Unknown action")
        return back(request, f"/archetypes/{archetype_id}")

    @app.get("/archetypes/{archetype_id}/resume.{extension}")
    def archetype_file(archetype_id: int, extension: str):
        if extension not in ("pdf", "tex"):
            raise HTTPException(404, "No such file")
        with Session(engine) as session:
            folder = documents.archetype_dir(home, get_archetype(session, archetype_id))
        return pdf_response(folder, documents.RESUME, extension)

    @app.post("/job/{listing_id}/resume")
    def make_job_resume(
        request: Request,
        listing_id: int,
        mode: str = Form(...),
        archetype_id: int | None = Form(None),
        style: str = Form(""),
    ):
        if mode not in (documents.REUSE, documents.TOUCH_UP, documents.TAILOR):
            raise HTTPException(400, "Unknown way to make a resume")
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            profile = profile_or_none()
            if profile is None:
                return back(request, message="Fix+your+profile+first:+run+jobs+profile+check")
            archetype = session.get(Archetype, archetype_id) if archetype_id else None
            writer = make_writer(session) if mode == documents.TAILOR else None
            if mode == documents.TAILOR and writer is None:
                return back(request, message="Claude+cannot+be+used+right+now")
            try:
                documents.make_resume(
                    session, home, listing, mode=mode, profile=profile, config=load_config(home),
                    requirements=requirements_of(session, listing), archetype=archetype,
                    reader=writer, style=style,
                )  # fmt: skip
            except (llm.LlmProblem, WriteError, ValueError) as exc:
                return writing_failed(request, exc)
        return back(request)

    @app.post("/job/{listing_id}/letter")
    def make_job_letter(request: Request, listing_id: int, style: str = Form("")):
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            profile = profile_or_none()
            if profile is None:
                return back(request, message="Fix+your+profile+first:+run+jobs+profile+check")
            writer = make_writer(session)
            if writer is None:
                return back(request, message="Claude+cannot+be+used+right+now")
            try:
                documents.make_letter(
                    session, home, listing, profile=profile, config=load_config(home),
                    reader=writer, requirements=requirements_of(session, listing), style=style,
                )  # fmt: skip
            except (llm.LlmProblem, WriteError) as exc:
                return writing_failed(request, exc)
        return back(request)

    @app.post("/job/{listing_id}/document/{kind}/{action}")
    def document_action(
        request: Request, listing_id: int, kind: str, action: str, style: str = Form("")
    ):
        with Session(engine) as session:
            document = documents.document_for(session, listing_id, kind)
            if document is None:
                raise HTTPException(404, "That document has not been made yet")
            if action == "style":
                documents.restyle_document(
                    session, home, document, style=style or load_config(home).resume.style,
                    profile=profile_or_none(), config=load_config(home),
                )  # fmt: skip
            elif action in ("approve", "unapprove"):
                documents.set_approved(session, document, action == "approve")
            elif action == "delete":
                session.delete(document)
                session.commit()
            else:
                raise HTTPException(404, "Unknown action")
        return back(request)

    @app.get("/job/{listing_id}/{kind}.{extension}")
    def job_file(listing_id: int, kind: str, extension: str):
        if kind not in (documents.RESUME, documents.LETTER) or extension not in ("pdf", "tex"):
            raise HTTPException(404, "No such file")
        return pdf_response(documents.listing_dir(home, listing_id), kind, extension)

    # ----- applying: details, files and answers to copy -------------------

    def assist_context(session: Session, listing: Listing, profile, question: str) -> dict | None:
        """What the panel beside an application form shows. None without a profile."""
        if profile is None:
            return None
        groups: dict[str, list] = {}
        for item in assist.details(profile):
            groups.setdefault(item.group, []).append(item)
        question = " ".join(question.split())[:1000]
        return {
            # {"You": [Detail(group, label, value), ...], "Links": [...], ...} in display order.
            "groups": groups,
            # {"resume": Path or None, "letter": Path or None}: PDFs for the form's file picker.
            "files": assist.document_files(home, profile, listing.id),
            # jobtool.db.JobAnswer rows kept for this job: question, answer, source, remarks.
            "answers": assist.answers_for(session, listing.id),
            # The question being looked up (from ?q=), and saved answers to similar questions:
            # Suggestion(answer=profile Answer(id, question, answer), score 0..1), best first.
            "question": question,
            "suggestions": assist.suggest(profile, question) if question else [],
            # Every saved answer, for browsing: profile Answer(id, question, answer).
            "bank": profile.answers,
            # Folded questions already in the bank, so "save to bank" is offered only once.
            "banked": {assist.fold(item.question) for item in profile.answers},
            "fold": assist.fold,
            "default_words": assist.DEFAULT_WORDS,
        }

    def get_answer(session: Session, listing_id: int, answer_id: int) -> JobAnswer:
        row = session.get(JobAnswer, answer_id)
        if row is None or row.listing_id != listing_id:
            raise HTTPException(404, "No such answer")
        return row

    def bank_message(question: str, answer: str, fact_ids: list[str]) -> str:
        """Add to the bank and say what happened, in words for the message bar."""
        try:
            assist.add_to_bank(home, question, answer, fact_ids)
        except (ValueError, ProfileError) as exc:
            return f"Not added to your saved answers: {exc}"
        return "Added to your saved answers"

    @app.post("/job/{listing_id}/answer/draft")
    def draft_job_answer(
        request: Request,
        listing_id: int,
        question: str = Form(""),
        q: str = Form(""),
        words: int = Form(assist.DEFAULT_WORDS),
    ):
        question = q or question  # the question box is named "q" for looking up
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            profile = profile_or_none()
            if profile is None:
                return back(request, message="Fix+your+profile+first:+run+jobs+profile+check")
            if not question.strip():
                return back(request, message="Paste+the+question+first", anchor="assist")
            writer = make_writer(session)
            if writer is None:
                return back(request, message="Claude+cannot+be+used+right+now", anchor="assist")
            try:
                assist.draft_answer(
                    session, writer, profile, listing, question,
                    requirements=requirements_of(session, listing), words=words,
                )  # fmt: skip
            except (llm.LlmProblem, WriteError) as exc:
                text = llm.TITLES[exc.kind] if isinstance(exc, llm.LlmProblem) else str(exc)
                return back(request, message=quote_plus(text), anchor="assist")
        return back(request, anchor="answers")

    @app.post("/job/{listing_id}/answer")
    def save_job_answer(
        request: Request,
        listing_id: int,
        question: str = Form(""),
        answer: str = Form(""),
        answer_id: int | None = Form(None),
        bank_id: str = Form(""),
        to_bank: str = Form(""),
    ):
        """Keep an answer for this job: typed, edited, or copied from the saved answers."""
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            profile = profile_or_none()
            row = get_answer(session, listing_id, answer_id) if answer_id else None
            fact_ids, source, remarks = None, assist.WRITTEN, []
            if bank_id and profile is not None:
                saved = next((item for item in profile.answers if item.id == bank_id), None)
                if saved is None:
                    raise HTTPException(404, "No such saved answer")
                question, answer = question.strip() or saved.question, saved.answer
                fact_ids, source = list(saved.facts), assist.BANK
            if not question.strip() or not answer.strip():
                return back(
                    request, message="An+answer+needs+a+question+and+some+text", anchor="assist"
                )
            if row is not None:
                source = row.source
                # An edit to Claude's draft is checked again. The remarks are advice only.
                if row.source == assist.DRAFTED and profile is not None:
                    remarks = assist.remarks_on(
                        profile, answer, row.fact_ids,
                        context=assist._context(
                            session, listing, requirements_of(session, listing)
                        )
                        + f" {question}",
                    )  # fmt: skip
            row = assist.save_answer(
                session, listing, question, answer, fact_ids, source, remarks=remarks, row=row
            )
            message = bank_message(row.question, row.answer, row.fact_ids) if to_bank else ""
        return back(request, message=quote_plus(message), anchor="answers")

    @app.post("/job/{listing_id}/answer/{answer_id}/{action}")
    def answer_action(request: Request, listing_id: int, answer_id: int, action: str):
        with Session(engine) as session:
            row = get_answer(session, listing_id, answer_id)
            message = ""
            if action == "delete":
                session.delete(row)
                session.commit()
            elif action == "bank":
                message = bank_message(row.question, row.answer, row.fact_ids)
            else:
                raise HTTPException(404, "Unknown action")
        return back(request, message=quote_plus(message), anchor="answers")

    # ----- reading postings ----------------------------------------------

    def read_one(session: Session, listing: Listing, *, force: bool = False) -> None:
        """Fetch and read one posting. A Claude failure becomes the banner, not an error."""
        with reading_lock:
            session.refresh(listing)
            config = load_config(home)
            reader = make_reader(session)
            if not force and not reading_run.pending(
                session, listing, config, have_reader=reader is not None, at=datetime.now()
            ):
                return
            try:
                reading_run.enrich_listing(
                    session, listing, config=config, profile=profile_or_none(), reader=reader,
                    get=get, force=force,
                )  # fmt: skip
            except llm.LlmProblem:
                pass  # already recorded for the banner

    def next_read(session: Session, state: str) -> Listing | None:
        """The next job, read first. Reading can filter a job out, so look again."""
        listing = queue.next_listing(session, state)
        if not reading:
            return listing
        for _ in range(4):
            if listing is None:
                return None
            read_one(session, listing)
            following = queue.next_listing(session, state)
            if following is None or following.id == listing.id:
                return following
            listing = following
        return listing

    ahead_running = threading.Lock()

    def read_ahead() -> None:
        """Read the next few jobs, one at a time, so a page never waits on the whole batch."""
        if not ahead_running.acquire(blocking=False):
            return  # already under way
        try:
            for _ in range(max(load_config(home).llm.read_ahead * 4, 4)):
                with reading_lock, Session(engine) as session:
                    result = reading_run.enrich_queue(
                        session, config=load_config(home), profile=profile_or_none(),
                        reader=make_reader(session), get=get, budget=1,
                    )  # fmt: skip
                if result.attempted == 0 or result.problem is not None:
                    break
        except Exception:  # noqa: BLE001 - a background failure must not take the app down
            log.exception("reading ahead failed")
        finally:
            ahead_running.release()

    def kick() -> None:
        if background:
            threading.Thread(target=read_ahead, daemon=True).start()

    if background:

        def startup_check() -> None:
            try:
                with Session(engine) as session:
                    if llm.load_key(home):
                        llm.check_now(session, home, load_config(home).llm.reading_model)
            except Exception:  # noqa: BLE001
                log.exception("the API key check failed")
            read_ahead()

        threading.Thread(target=startup_check, daemon=True).start()

    @app.post("/job/{listing_id}/read")
    def read_now(request: Request, listing_id: int):
        with Session(engine) as session:
            read_one(session, get_listing(session, listing_id), force=True)
        return back(request)

    @app.post("/claude/check")
    def check_claude(request: Request):
        with Session(engine) as session:
            if not llm.load_key(home):
                return back(request, message="No+API+key+found+yet")
            problem = llm.check_now(session, home, load_config(home).llm.reading_model)
        kick()
        return back(request, message="Claude+is+working" if problem is None else "")

    # ----- queue ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def main_queue(request: Request, q: str = ""):
        with Session(engine) as session:
            listing = next_read(session, db.MAIN)
            return render_page(
                request, "queue.html", session, mode="main", **card_context(session, listing, q)
            )

    @app.get("/set-aside/work", response_class=HTMLResponse)
    def set_aside_queue(request: Request, q: str = ""):
        with Session(engine) as session:
            listing = next_read(session, db.SET_ASIDE)
            return render_page(
                request, "queue.html", session, mode="set-aside",
                **card_context(session, listing, q),
            )  # fmt: skip

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
        kick()
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
            return render_page(
                request, "list.html", session, name=name, title=title, blurb=blurb,
                jobs=jobs[:500], total=len(jobs),
            )  # fmt: skip

    @app.get("/job/{listing_id}", response_class=HTMLResponse)
    def job_page(request: Request, listing_id: int, q: str = ""):
        with Session(engine) as session:
            listing = get_listing(session, listing_id)
            return render_page(
                request, "job.html", session,
                timeline=queue.timeline(session, listing.id),
                **card_context(session, listing, q),
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
            return render_page(
                request, "tracker.html", session, rows=session.exec(query).all(), status=status
            )

    @app.post("/application/{application_id}")
    def update_application(
        request: Request,
        application_id: int,
        status: str = Form(""),
        notes: str = Form(None),
        next_followup: str = Form(None),
        has_followup: str = Form(""),
    ):
        # A cleared date field arrives as "missing", so forms that carry the date say so.
        if has_followup and next_followup is None:
            next_followup = ""
        with Session(engine) as session:
            application = session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, "No such application")
            if notes is not None:
                application.notes = notes.strip()
            if next_followup is not None:
                try:
                    application.next_followup = (
                        date.fromisoformat(next_followup) if next_followup.strip() else None
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

    @app.post("/application/{application_id}/snooze")
    def snooze_application(request: Request, application_id: int, days: int = Form(7)):
        """Ask to hear about this application again in a few days."""
        with Session(engine) as session:
            application = session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, "No such application")
            insights.snooze(session, application, min(max(days, 1), 90))
        return back(request, "/today")

    @app.get("/today", response_class=HTMLResponse)
    def today_page(request: Request):
        with Session(engine) as session:
            config = load_config(home)
            return render_page(
                request, "today.html", session,
                # jobtool.insights.Reminder: kind ("deadline", "follow-up", "unfinished",
                # "quiet"), listing, text, due (date), application (or None). Most pressing first.
                reminders=insights.reminders(session, config),
                # jobtool.insights.Stats, for the week's pace line.
                stats=insights.stats(session, config),
                tracking=config.tracking,
            )  # fmt: skip

    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request):
        with Session(engine) as session:
            return render_page(
                request, "board.html", session,
                # {status: [(Application, Listing), ...]} in the order of db.STATUSES.
                columns=insights.board(session), today=date.today(),
            )  # fmt: skip

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request):
        with Session(engine) as session:
            config = load_config(home)
            return render_page(
                request, "stats.html", session,
                stats=insights.stats(session, config), tracking=config.tracking,
            )  # fmt: skip

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
            return render_page(request, "sources.html", session, sources=rows, preview=None)

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
            return render_page(
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

    sync_lock = threading.Lock()

    def run_sync():
        with sync_lock, Session(engine) as session:
            return sync_all(session, load_config(home), profile_or_none())

    def sync_due(now: datetime) -> bool:
        """True when the sources have not been synced for the configured time."""
        hours = load_config(home).tracking.sync_every_hours
        if hours <= 0:
            return False
        with Session(engine) as session:
            last = db.get_meta(session, "last_sync_at")
        try:
            return now - datetime.fromisoformat(last) >= timedelta(hours=hours)
        except ValueError:
            return True  # never synced

    def sync_if_due(now: datetime | None = None) -> bool:
        if not sync_due(now or datetime.now()):
            return False
        try:
            run_sync()
        except Exception:  # noqa: BLE001 - a failed sync is tried again at the next check
            log.exception("the scheduled sync failed")
            return False
        kick()
        return True

    app.state.sync_if_due = sync_if_due
    stop_schedule = threading.Event()
    app.state.stop_schedule = stop_schedule

    if scheduled:

        def schedule_loop() -> None:
            # Checked every few minutes, so a laptop that slept catches up soon after waking.
            while not stop_schedule.wait(300):
                sync_if_due()

        threading.Thread(target=schedule_loop, daemon=True).start()

    @app.post("/sync")
    def sync_now(request: Request):
        result = run_sync()
        kick()
        failed = sum(1 for source in result.sources if source.error)
        message = f"Synced: {result.new} new" + (f", {failed} source(s) failed" if failed else "")
        return back(request, message=message.replace(" ", "+"))

    return app
