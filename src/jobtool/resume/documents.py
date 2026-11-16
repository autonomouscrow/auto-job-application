"""Archetype resumes, per-job documents, and where their files live.

An archetype is a resume for a kind of role, written once from the profile
and approved by the user. Most jobs reuse one as it is. A job that needs a
little more gets a touch-up, which only reorders and adds skills from the
profile and involves no model. A job that fits no archetype is tailored: a
fresh resume written from the profile for that posting.

Changing the style of anything re-renders the stored document. The wording
is not touched and no model is called.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from jobtool import llm
from jobtool.config import Config
from jobtool.db import Archetype, Document, JobText, Listing
from jobtool.enrich.requirements import Extracted
from jobtool.llm import Reader
from jobtool.profile.models import Profile
from jobtool.resume import render
from jobtool.resume.model import LetterDoc, ResumeDoc, SkillRow
from jobtool.resume.write import Brief, Report, write_letter, write_resume

REUSE, TOUCH_UP, TAILOR = "reuse", "touch-up", "tailor"
RESUME, LETTER = "resume", "letter"
# Share of the skills a posting asks for (and you have) that a resume must show.
REUSE_AT = 0.8
TOUCH_UP_AT = 0.5


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "resume"


def archetype_dir(home: Path, archetype: Archetype) -> Path:
    return home / "data" / "archetypes" / archetype.slug


def listing_dir(home: Path, listing_id: int) -> Path:
    return home / "data" / "documents" / str(listing_id)


def file_stem(profile: Profile | None, kind: str) -> str:
    """The file name an employer will see, for example ``Alex_Doe_Resume``."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", profile.identity.name).strip("_") if profile else ""
    return f"{name or 'Applicant'}_{'Resume' if kind == RESUME else 'Cover_Letter'}"


def pdf_path(folder: Path, profile: Profile | None, kind: str) -> Path:
    return folder / f"{file_stem(profile, kind)}.pdf"


# ----- rendering --------------------------------------------------------------


@dataclass
class Rendered:
    doc: ResumeDoc | LetterDoc
    pages: int = 0
    notes: list[str] | None = None
    error: str = ""


def style_name(config: Config, wanted: str = "") -> str:
    return wanted or config.resume.style


def render_resume_files(
    home: Path, config: Config, doc: ResumeDoc, folder: Path, stem: str, style: str
) -> Rendered:
    """Write the .tex and the PDF, trimming low-priority bullets until it is one page.

    A missing LaTeX engine or a failed compile is reported in ``error``; the .tex
    file is still written so nothing is lost.
    """
    try:
        loaded = render.load_style(home, style)

        def compile_doc(candidate: ResumeDoc):
            tex = render.render_resume(candidate, loaded, config.resume.options)
            return render.compile_pdf(tex, folder, stem, config.resume.engine)

        fitted, compiled, cut = render.fit_to_one_page(doc, compile_doc)
    except render.RenderError as exc:
        return Rendered(doc, error=str(exc))
    # The full document is what gets stored, so a roomier style can show the cut
    # bullets again. ``cut`` tells the user what this style had no room for.
    del fitted
    return Rendered(doc, pages=compiled.pages, notes=cut)


def render_letter_files(
    home: Path, config: Config, doc: LetterDoc, folder: Path, stem: str, style: str
) -> Rendered:
    try:
        tex = render.render_letter(doc, render.load_style(home, style), config.resume.options)
        compiled = render.compile_pdf(tex, folder, stem, config.resume.engine)
    except render.RenderError as exc:
        return Rendered(doc, error=str(exc))
    return Rendered(doc, pages=compiled.pages)


def _log_calls(session: Session, purpose: str, report: Report) -> None:
    for reply in report.calls:
        llm.log_usage(session, purpose, reply)
    llm.record_ok(session)


def _report_json(report: Report | None, rendered: Rendered, previous: str = "") -> str:
    """The verifier's report plus what the layout cut. ``previous`` keeps an earlier
    report's findings when only the style changed."""
    if report is not None:
        data = report.to_json()
        data["written"] = data["notes"]
    else:
        try:
            data = json.loads(previous or "{}")
        except ValueError:
            data = {}
        data = {"checked": 0, "findings": [], "written": [], **data}
    data["notes"] = [*data["written"], *(rendered.notes or [])]
    return json.dumps(data)


# ----- archetypes -------------------------------------------------------------


def create_archetype(
    session: Session, *, name: str, role_type: str = "any", focus: str = "", style: str = ""
) -> Archetype:
    base = slug = slugify(name)
    number = 2
    while session.exec(select(Archetype).where(Archetype.slug == slug)).first():
        slug, number = f"{base}-{number}", number + 1
    archetype = Archetype(
        slug=slug, name=name.strip(), role_type=role_type, focus=focus.strip(), style=style,
        updated_at=datetime.now(),
    )  # fmt: skip
    session.add(archetype)
    session.commit()
    session.refresh(archetype)
    return archetype


def delete_archetype(session: Session, home: Path, archetype: Archetype) -> None:
    """Remove the archetype and its files, so a later one with the same name starts clean."""
    shutil.rmtree(archetype_dir(home, archetype), ignore_errors=True)
    session.delete(archetype)
    session.commit()


def archetype_doc(archetype: Archetype) -> ResumeDoc | None:
    if not archetype.doc_json:
        return None
    try:
        return ResumeDoc.model_validate_json(archetype.doc_json)
    except ValueError:
        return None


def generate_archetype(
    session: Session,
    home: Path,
    archetype: Archetype,
    *,
    reader: Reader,
    profile: Profile,
    config: Config,
) -> Archetype:
    """Write the archetype's resume with the model. Approval is reset: it is new text.

    Raises ``LlmProblem`` (recorded for the banner) or ``WriteError``.
    """
    brief = Brief(
        name=archetype.name, role_type=archetype.role_type, focus=archetype.focus,
        projects_title=config.resume.projects_title,
    )  # fmt: skip
    try:
        doc, report = write_resume(reader, profile, brief)
    except llm.LlmProblem as problem:
        llm.record_problem(session, problem)
        session.commit()
        raise
    _log_calls(session, "write archetype", report)
    style = style_name(config, archetype.style)
    rendered = render_resume_files(
        home, config, doc, archetype_dir(home, archetype), file_stem(profile, RESUME), style
    )
    archetype.doc_json = rendered.doc.model_dump_json()
    archetype.report_json = _report_json(report, rendered)
    archetype.pages, archetype.error = rendered.pages, rendered.error
    archetype.approved, archetype.updated_at = False, datetime.now()
    session.add(archetype)
    session.commit()
    return archetype


def restyle_archetype(
    session: Session, home: Path, archetype: Archetype, *, style: str, profile: Profile | None,
    config: Config,
) -> Archetype:  # fmt: skip
    """Render the same wording in another style. No model call, approval is kept."""
    doc = archetype_doc(archetype)
    archetype.style = style
    if doc is not None:
        rendered = render_resume_files(
            home, config, doc, archetype_dir(home, archetype), file_stem(profile, RESUME),
            style_name(config, style),
        )  # fmt: skip
        archetype.report_json = _report_json(None, rendered, archetype.report_json)
        archetype.pages, archetype.error = rendered.pages, rendered.error
    session.add(archetype)
    session.commit()
    return archetype


# ----- choosing for a job -----------------------------------------------------


@dataclass
class Recommendation:
    mode: str  # REUSE, TOUCH_UP, or TAILOR
    archetype: Archetype | None
    coverage: float  # of the posting's skills you have, the share this resume shows
    missing: list[str]  # names of skills you have that the resume does not show
    reason: str


def _wanted(requirements: Extracted | None) -> dict[str, tuple[str, int]]:
    """Skills the posting asks for that the applicant has: id -> (name, weight)."""
    wanted: dict[str, tuple[str, int]] = {}
    if requirements is None:
        return wanted
    for need in requirements.nice_to_have_skills:
        if need.profile_skill:
            wanted[need.profile_skill] = (need.name, 1)
    for need in requirements.must_have_skills:
        if need.profile_skill:
            wanted[need.profile_skill] = (need.name, 2)
    return wanted


def _shown(doc: ResumeDoc, profile: Profile | None) -> tuple[set[str], set[str]]:
    """Skill ids shown anywhere on the resume, and those backed by a bullet."""
    facts = profile.facts() if profile else {}
    in_bullets = {
        skill_id
        for line in doc.lines()
        for fact_id in line.fact_ids
        if fact_id in facts
        for skill_id in facts[fact_id].skills
    }
    return doc.skill_ids() | in_bullets, in_bullets


def recommend(
    session: Session, listing: Listing, requirements: Extracted | None, profile: Profile | None
) -> Recommendation:
    """Pick the cheapest way to get a fitting resume for this job."""
    wanted = _wanted(requirements)
    total = sum(weight for _, weight in wanted.values()) or 1
    candidates = []
    for archetype in session.exec(select(Archetype).where(Archetype.approved == True)).all():  # noqa: E712
        doc = archetype_doc(archetype)
        if doc is None:
            continue
        shown, in_bullets = _shown(doc, profile)
        coverage = sum(w for i, (_, w) in wanted.items() if i in shown) / total if wanted else 1.0
        depth = sum(w for i, (_, w) in wanted.items() if i in in_bullets) / total if wanted else 1.0
        # A resume written for this kind of role beats one that was not.
        fit = 0.15 if archetype.role_type == listing.role_type else 0.0
        fit -= 0.6 if archetype.role_type not in (listing.role_type, "any") else 0.0
        missing = [name for i, (name, _) in wanted.items() if i not in shown]
        rank = coverage + depth + fit
        candidates.append((rank, -archetype.id, archetype, coverage, depth, missing))
    if not candidates:
        return Recommendation(TAILOR, None, 0.0, [], "There is no approved archetype resume yet.")
    _, _, archetype, coverage, depth, missing = max(candidates, key=lambda item: item[:2])
    wrong_role = archetype.role_type not in (listing.role_type, "any")
    if wrong_role:
        reason = f"Your closest archetype, {archetype.name}, was written for another kind of role."
        return Recommendation(TAILOR, archetype, coverage, missing, reason)
    if coverage >= REUSE_AT:
        reason = f"{archetype.name} already shows {coverage:.0%} of the skills this job asks for."
        return Recommendation(REUSE, archetype, coverage, missing, reason)
    if depth >= TOUCH_UP_AT:
        reason = (
            f"{archetype.name} backs most of what this job asks for; "
            f"its skills list needs {len(missing)} addition(s)."
        )
        return Recommendation(TOUCH_UP, archetype, coverage, missing, reason)
    reason = f"{archetype.name} shows only {coverage:.0%} of the skills this job asks for."
    return Recommendation(TAILOR, archetype, coverage, missing, reason)


def touch_up(doc: ResumeDoc, requirements: Extracted | None, profile: Profile) -> ResumeDoc:
    """Bring the skills this job asks for to the front of the skills section.

    Skills come only from the profile: ones the posting wants that the resume lacks
    are added to the row for their category, and wanted skills are listed first.
    """
    wanted = _wanted(requirements)
    by_id = {skill.id: skill for skill in profile.skills}
    doc = doc.model_copy(deep=True)
    rows = [row for row in doc.skills if row.skill_ids]
    present = doc.skill_ids()
    for skill_id in wanted:
        if skill_id in present or skill_id not in by_id:
            continue
        category = by_id[skill_id].category
        home_row = next(
            (
                row
                for row in rows
                if row.label.lower() == category.lower()
                or any(by_id[i].category == category for i in row.skill_ids if i in by_id)
            ),
            None,
        )
        if home_row is None:
            home_row = SkillRow(label=category, items=[], skill_ids=[])
            position = max(len(doc.skills) - (0 if rows == doc.skills else 1), 0)
            doc.skills.insert(position, home_row)
            rows.append(home_row)
        home_row.skill_ids.append(skill_id)
    for row in rows:
        ordered = sorted(
            row.skill_ids, key=lambda i: -wanted[i][1] if i in wanted else 0
        )  # stable: ties keep their order
        row.skill_ids = ordered
        row.items = [by_id[i].name for i in ordered if i in by_id]
    return doc


# ----- documents for a job ----------------------------------------------------


def document_for(session: Session, listing_id: int, kind: str) -> Document | None:
    return session.exec(
        select(Document).where(Document.listing_id == listing_id, Document.kind == kind)
    ).first()


def _save(
    session: Session, listing: Listing, kind: str, *, mode: str, archetype_id: int | None,
    style: str, rendered: Rendered, report: Report | None,
) -> Document:  # fmt: skip
    document = document_for(session, listing.id, kind) or Document(
        listing_id=listing.id, kind=kind, created_at=datetime.now()
    )
    document.mode, document.archetype_id, document.style = mode, archetype_id, style
    document.doc_json = rendered.doc.model_dump_json()
    document.report_json = _report_json(report, rendered)
    document.pages, document.error = rendered.pages, rendered.error
    document.approved, document.created_at = False, datetime.now()
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def _brief_for(listing: Listing, requirements: Extracted | None, config: Config) -> Brief:
    return Brief(
        name=listing.title, role_type=listing.role_type, company=listing.company,
        title=listing.title, requirements=requirements,
        projects_title=config.resume.projects_title,
    )  # fmt: skip


def make_resume(
    session: Session,
    home: Path,
    listing: Listing,
    *,
    mode: str,
    profile: Profile,
    config: Config,
    requirements: Extracted | None = None,
    archetype: Archetype | None = None,
    reader: Reader | None = None,
    style: str = "",
) -> Document:
    """Prepare this job's resume. Only ``TAILOR`` calls the model."""
    report: Report | None = None
    if mode == TAILOR:
        if reader is None:
            raise llm.LlmProblem(llm.MISSING_KEY)
        try:
            doc, report = write_resume(reader, profile, _brief_for(listing, requirements, config))
        except llm.LlmProblem as problem:
            llm.record_problem(session, problem)
            session.commit()
            raise
        _log_calls(session, "tailor resume", report)
    else:
        base = archetype_doc(archetype) if archetype else None
        if base is None:
            raise ValueError("Choose an archetype that has been generated.")
        doc = touch_up(base, requirements, profile) if mode == TOUCH_UP else base
    style = style_name(config, style or (archetype.style if archetype and mode != TAILOR else ""))
    rendered = render_resume_files(
        home, config, doc, listing_dir(home, listing.id), file_stem(profile, RESUME), style
    )
    used = archetype.id if archetype and mode != TAILOR else None
    return _save(
        session, listing, RESUME, mode=mode, archetype_id=used, style=style,
        rendered=rendered, report=report,
    )  # fmt: skip


def make_letter(
    session: Session,
    home: Path,
    listing: Listing,
    *,
    profile: Profile,
    config: Config,
    reader: Reader,
    requirements: Extracted | None = None,
    style: str = "",
) -> Document:
    posting = session.exec(select(JobText).where(JobText.listing_id == listing.id)).first()
    try:
        doc, report = write_letter(
            reader, profile, _brief_for(listing, requirements, config),
            posting_text=posting.text if posting else "",
        )  # fmt: skip
    except llm.LlmProblem as problem:
        llm.record_problem(session, problem)
        session.commit()
        raise
    _log_calls(session, "write letter", report)
    style = style_name(config, style)
    rendered = render_letter_files(
        home, config, doc, listing_dir(home, listing.id), file_stem(profile, LETTER), style
    )
    return _save(
        session, listing, LETTER, mode=TAILOR, archetype_id=None, style=style,
        rendered=rendered, report=report,
    )  # fmt: skip


def restyle_document(
    session: Session, home: Path, document: Document, *, style: str, profile: Profile | None,
    config: Config,
) -> Document:  # fmt: skip
    """Render a job's document in another style. No model call."""
    folder = listing_dir(home, document.listing_id)
    stem = file_stem(profile, document.kind)
    if document.kind == RESUME:
        doc = ResumeDoc.model_validate_json(document.doc_json)
        rendered = render_resume_files(home, config, doc, folder, stem, style)
    else:
        letter = LetterDoc.model_validate_json(document.doc_json)
        rendered = render_letter_files(home, config, letter, folder, stem, style)
    document.style = style
    document.report_json = _report_json(None, rendered, document.report_json)
    document.pages, document.error = rendered.pages, rendered.error
    session.add(document)
    session.commit()
    return document


def set_approved(session: Session, row: Archetype | Document, approved: bool) -> None:
    row.approved = approved
    session.add(row)
    session.commit()

