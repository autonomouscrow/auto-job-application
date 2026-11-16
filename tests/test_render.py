"""Rendering resumes and letters to LaTeX and PDF, with switchable styles."""

from __future__ import annotations

import re
import shutil
import zlib
from pathlib import Path

import pytest

from jobtool.resume import render
from jobtool.resume.model import (
    Block,
    Contact,
    Header,
    LetterDoc,
    Line,
    ResumeDoc,
    Section,
    SkillRow,
)
from jobtool.resume.render import (
    Compiled,
    EngineMissing,
    RenderError,
    compile_pdf,
    count_pdf_pages,
    find_engine,
    fit_to_one_page,
    list_styles,
    load_style,
    render_letter,
    render_resume,
    tex_escape,
    tex_rich,
    tex_url,
)

needs_engine = pytest.mark.skipif(find_engine() is None, reason="no LaTeX engine installed")


# ----- a made-up applicant -------------------------------------------------------


def header() -> Header:
    return Header(
        name="Alex Doe",
        contacts=[
            Contact(text="+1-555-010-0199"),
            Contact(text="alex.doe@example.com", url="mailto:alex.doe@example.com"),
            Contact(text="linkedin.com/in/alexdoe", url="https://linkedin.com/in/alexdoe"),
            Contact(text="github.com/alexdoe", url="https://github.com/alexdoe"),
        ],
    )


def ln(text: str, priority: int = 2) -> Line:
    return Line(text=text, fact_ids=["f1"], priority=priority)


def full_doc() -> ResumeDoc:
    education = Section(
        key="education",
        title="Education",
        blocks=[
            Block(
                entry_id="edu1",
                title="Example State University",
                subtitle="Bachelor of Science, Computer Science",
                dates="Sept. 2024 -- Apr. 2028",
                location="Springfield, IL",
                lines=[
                    ln("**GPA 3.9/4.0** -- Dean's List (4 terms), Merit Scholarship", 1),
                    ln("Coursework: Algorithms, Operating Systems, Databases, Linear Algebra"),
                ],
            )
        ],
    )
    experience = Section(
        key="experience",
        title="Experience",
        blocks=[
            Block(
                entry_id="exp1",
                title="Northwind Analytics",
                subtitle="Software Engineering Intern",
                dates="May 2026 -- Aug. 2026",
                location="Chicago, IL",
                lines=[
                    ln("Built a **streaming ingestion service** in Go handling 40k events/s", 1),
                    ln("Cut nightly batch runtime by **35%** by rewriting joins in SQL"),
                    ln("Added integration tests that caught 12 regressions before release"),
                    ln("Wrote runbooks and on-call dashboards used by a team of 8", 3),
                ],
            ),
            Block(
                entry_id="exp2",
                title="Contoso Robotics",
                subtitle="Embedded Software Co-op",
                dates="Sept. 2025 -- Dec. 2025",
                location="Remote",
                lines=[
                    ln("Ported motor-control firmware to a new microcontroller in **C++**", 1),
                    ln("Reduced sensor read latency from 4 ms to ~1.2 ms with DMA transfers"),
                    ln("Automated hardware-in-the-loop tests with Python and pytest", 3),
                ],
            ),
            Block(
                entry_id="exp3",
                title="Example State University",
                subtitle="Teaching Assistant, Data Structures",
                dates="Jan. 2025 -- Apr. 2025",
                location="Springfield, IL",
                lines=[
                    ln("Led weekly tutorials for 30 students and graded assignments", 2),
                    ln("Wrote autograder tests that cut grading time by half", 3),
                ],
            ),
        ],
    )
    projects = Section(
        key="projects",
        title="Projects",
        blocks=[
            Block(
                entry_id="p1",
                title="Pathfinder -- Warehouse Route Planner",
                stack=["Python", "OR-Tools", "FastAPI"],
                dates="Feb. 2026 -- Present",
                lines=[
                    ln("Solved vehicle routing for **200 stops** in under 2 seconds", 1),
                    ln("Exposed the solver as a REST API with typed request validation"),
                    ln("Visualised routes on a map with a small React front end", 3),
                ],
            ),
            Block(
                entry_id="p2",
                title="Tiny Ray Tracer",
                stack=["Rust"],
                dates="Oct. 2025",
                lines=[
                    ln("Implemented a multithreaded path tracer with BVH acceleration", 2),
                ],
            ),
        ],
    )
    return ResumeDoc(
        header=header(),
        sections=[education, experience, projects],
        skills=[
            SkillRow(label="Languages", items=["Python", "Go", "C++", "Rust", "SQL"]),
            SkillRow(label="Frameworks", items=["FastAPI", "React", "pytest", "OR-Tools"]),
            SkillRow(label="Data & ML", items=["NumPy", "Pandas", "scikit-learn"]),
            SkillRow(label="Tools", items=["Git", "Docker", "Linux", "PostgreSQL"]),
            SkillRow(label="Certifications", items=["Example Cloud Practitioner (2025)"]),
        ],
    )


def letter_doc() -> LetterDoc:
    return LetterDoc(
        header=header(),
        date="November 16, 2026",
        company="Northwind Trading",
        role="Software Engineering Intern, Summer 2027",
        paragraphs=[
            Line(text="I am applying for the **Software Engineering Intern** role."),
            Line(text="Last summer I built a streaming ingestion service in Go."),
            Line(text="I would welcome the chance to talk about how I could help your team."),
        ],
    )


HOSTILE = (
    r"\input{/etc/passwd} \write18{rm -rf /} \immediate\write18{ls} 100% & #1 a_b $x$ {} "
    r"^^5c x^2 ~5 a~b \\ \textbf{x} \def\x{y} \catcode`\@=11 \end{document} "
    '\u2014 \u2013 \u201cquoted\u201d \u2018single\u2019 "straight" \u2248 \u00d7 '
    "a\u00a0b caf\u00e9 \u00df \U0001f680 \x00\x07 <tag> |pipe| \u2026"
)


def hostile_doc() -> ResumeDoc:
    doc = full_doc()
    block = doc.sections[1].blocks[0]
    block.title = HOSTILE[:40]
    block.lines = [ln(HOSTILE, 1), ln("**bold " + HOSTILE[:30] + "**", 1)]
    doc.header.contacts.append(Contact(text=r"\input{x}", url=r"https://x.example/\input{a}%#"))
    doc.header.contacts.append(Contact(text="evil", url="javascript:alert(1)"))
    doc.skills[0].items.append(r"\write18{whoami}")
    doc.header.name = "Alex \\input{x} Doe_%"
    doc.sections[2].title = r"Projects \def & ~ ^ % $"
    doc.skills_title = r"Skills}\end{document}"
    doc.header.contacts.append(Contact(text="query", url="https://example.com/a_b?q=1&r=2~x#top"))
    return doc


def styles() -> list[str]:
    return ["navy", "classic"]


# ----- escaping -----------------------------------------------------------------


def test_tex_escape_specials() -> None:
    assert tex_escape(r"a\b") == r"a\textbackslash{}b"
    assert tex_escape("& % $ # _ { }") == r"\& \% \$ \# \_ \{ \}"
    assert tex_escape("a~b ^") == r"a\textasciitilde{}b \textasciicircum{}"
    assert tex_escape("~5 ms") == r"$\sim$5 ms"
    assert tex_escape("\u2248 3 \u00d7 4") == r"$\approx$ 3 $\times$ 4"
    assert tex_escape("a\u2014b\u2013c") == "a---b--c"
    assert tex_escape("\u201chi\u201d \u2019") == "``hi'' '"
    assert tex_escape('say "hi" (and "bye")') == "say ``hi'' (and ``bye'')"
    assert tex_escape("a\u00a0b") == "a~b"
    assert tex_escape("<a|b>") == r"\textless{}a\textbar{}b\textgreater{}"
    assert tex_escape("caf\u00e9 na\u00efve") == r"caf\'{e} na\"{\i}ve"
    assert tex_escape("go \U0001f680\x00\x07 now") == "go  now"
    assert tex_escape("a\n\nb") == "a b"


def test_tex_rich_bold() -> None:
    assert tex_rich("cut **35%** time") == r"cut \textbf{35\%} time"
    assert tex_rich("cut **35%** time", bold=False) == r"cut 35\% time"
    assert tex_rich("a ** b") == "a ** b"
    assert tex_rich("**unclosed") == "**unclosed"
    assert tex_rich('an **"Outstanding"** review') == r"an \textbf{``Outstanding''} review"
    assert tex_rich(r"**\input{x}**") == r"\textbf{\textbackslash{}input\{x\}}"


def test_tex_url() -> None:
    assert tex_url("https://example.com/a_b?q=1&r=2#top") == r"https://example.com/a_b?q=1&r=2\#top"
    assert tex_url("github.com/alexdoe") == "https://github.com/alexdoe"
    assert tex_url("javascript:alert(1)") == ""
    assert tex_url("file:///etc/passwd") == ""
    assert "\\input" not in tex_url(r"https://x/\input{a}")
    assert tex_url("mailto:alex.doe@example.com") == "mailto:alex.doe@example.com"


def test_hostile_text_is_inert() -> None:
    for name in styles():
        tex = render_resume(hostile_doc(), load_style(None, name))
        body = tex.split(r"\begin{document}", 1)[1]
        for bad in (r"\input", r"\write18", r"\immediate", r"\def\x", r"\catcode", "^^5c"):
            assert bad not in tex
        assert body.count(r"\end{document}") == 1
        assert "javascript" not in tex
        assert "\x00" not in tex and "\U0001f680" not in tex
        # every % in the body is either escaped or a template line-end comment
        for line in body.splitlines():
            for m in re.finditer(r"%", line):
                assert line[m.start() - 1] == "\\" or line[m.start() :].strip() == "%"


@needs_engine
def test_hostile_text_compiles(tmp_path: Path) -> None:
    for name in styles():
        tex = render_resume(hostile_doc(), load_style(None, name))
        result = compile_pdf(tex, tmp_path / name, "hostile")
        assert result.pdf.exists() and result.pages >= 1


# ----- styles -------------------------------------------------------------------


def test_builtin_styles_listed() -> None:
    found = {s.name: s for s in list_styles(None)}
    assert {"navy", "classic"} <= set(found)
    assert found["navy"].built_in and found["navy"].label
    assert found["navy"].options["accent"] == "1F3A5F"
    assert found["classic"].options["accent"] == "1F4E9A"
    for info in found.values():
        assert set(info.options) >= {"accent", "font_size", "margin", "bold_highlights"}


def test_style_references_match_layouts() -> None:
    navy = render_resume(full_doc(), load_style(None, "navy"))
    assert "hmargin=0.5in" in navy and "{HTML}{1F3A5F}" in navy
    assert r"label={\color{accent}\textbullet}" in navy and "{HTML}{555555}" in navy
    classic = render_resume(full_doc(), load_style(None, "classic"))
    assert "left=0.65in,right=0.65in" in classic and "{HTML}{1F4E9A}" in classic
    assert r"\linespread{1.06}" in classic


def test_section_order_and_skills_last() -> None:
    doc = full_doc()
    doc.sections = [doc.sections[2], doc.sections[0], doc.sections[1]]
    doc.skills_title = "Skills & Tools"
    tex = render_resume(doc, load_style(None, "navy"))
    order = [m.group(1) for m in re.finditer(r"\\section\{([^}]*)\}", tex)]
    assert order == ["Projects", "Education", "Experience", r"Skills \& Tools"]
    assert r"\textbf{Data \& ML} & NumPy, Pandas, scikit-learn \\" in tex
    assert r"\project{Tiny Ray Tracer}{Rust}{Oct. 2025}" in tex
    entry = r"\entry{Contoso Robotics}{Sept. 2025 -- Dec. 2025}{Embedded Software Co-op}{Remote}"
    assert entry in tex
    assert r"\href{mailto:alex.doe@example.com}{alex.doe@example.com}" in tex
    assert "+1-555-010-0199" in tex and r"\contactsep" in tex


def test_block_without_lines_has_no_empty_itemize() -> None:
    doc = full_doc()
    doc.sections[2].blocks[1].lines = []
    for name in styles():
        tex = render_resume(doc, load_style(None, name))
        assert re.search(r"\\begin\{(itemize|bullets)\}\s*\\end\{(itemize|bullets)\}", tex) is None


def test_options_change_output() -> None:
    style = load_style(None, "navy")
    base = render_resume(full_doc(), style)
    assert r"\documentclass[10pt" in base and r"\textbf{35\%}" in base
    changed = render_resume(
        full_doc(),
        style,
        {"accent": "#aa0000", "font_size": 12, "margin": 0.75, "bold_highlights": False},
    )
    assert "{HTML}{AA0000}" in changed and "{HTML}{1F3A5F}" not in changed
    assert r"\documentclass[12pt" in changed
    assert "hmargin=0.75in" in changed
    assert r"Cut nightly batch runtime by 35\%" in changed
    assert r"\textbf{35\%}" not in changed
    letter = render_letter(letter_doc(), style, {"bold_highlights": False})
    assert r"\textbf{Software Engineering Intern}" not in letter


@pytest.mark.parametrize(
    "options",
    [{"accent": "navy"}, {"accent": "12345"}, {"font_size": 9}, {"margin": "wide"}],
)
def test_bad_options_rejected(options: dict) -> None:
    with pytest.raises(RenderError):
        render_resume(full_doc(), load_style(None, "navy"), options)


def test_unknown_style() -> None:
    with pytest.raises(RenderError, match="unknown resume style 'fancy'"):
        load_style(None, "fancy")
    with pytest.raises(RenderError):
        load_style(None, "../navy")


def test_user_style_overrides_and_is_listed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / "resume" / "styles"
    shutil.copytree(render.BUILTIN_STYLES / "navy", user / "navy")
    (user / "navy" / "style.yaml").write_text(
        "label: My Navy\ndescription: mine\noptions:\n  accent: 112233\n", encoding="utf-8"
    )
    (user / "navy" / "resume.tex.j2").write_text(
        r"USER \VAR{doc.header.name|tex} \VAR{opt.accent}", encoding="utf-8"
    )
    shutil.copytree(render.BUILTIN_STYLES / "classic", user / "plain")
    (user / "plain" / "style.yaml").write_text("label: Plain\n", encoding="utf-8")

    found = {s.name: s for s in list_styles(home)}
    assert found["navy"].label == "My Navy" and not found["navy"].built_in
    assert found["plain"].label == "Plain" and not found["plain"].built_in
    assert found["classic"].built_in
    navy = load_style(home, "navy")
    assert render_resume(full_doc(), navy) == "USER Alex Doe 112233"
    assert "{HTML}{1F3A5F}" in render_resume(full_doc(), load_style(None, "navy"))


# ----- compiling ----------------------------------------------------------------


@needs_engine
@pytest.mark.parametrize("name", ["navy", "classic"])
def test_full_resume_is_one_page(tmp_path: Path, name: str) -> None:
    tex = render_resume(full_doc(), load_style(None, name))
    result = compile_pdf(tex, tmp_path, f"resume-{name}")
    assert result.pages == 1
    assert result.pdf == tmp_path / f"resume-{name}.pdf" and result.pdf.exists()
    assert result.tex.read_text(encoding="utf-8") == tex
    if shutil.which("pdfinfo"):
        import subprocess

        info = subprocess.run(["pdfinfo", str(result.pdf)], capture_output=True, text=True)
        assert re.search(r"Pages:\s+1\b", info.stdout)


@needs_engine
@pytest.mark.parametrize("name", ["navy", "classic"])
def test_letter_compiles(tmp_path: Path, name: str) -> None:
    tex = render_letter(letter_doc(), load_style(None, name))
    assert "Northwind Trading" in tex and "Dear Hiring Team," in tex
    result = compile_pdf(tex, tmp_path, f"letter-{name}")
    assert result.pages == 1


@needs_engine
def test_compile_error_reports_first_latex_error(tmp_path: Path) -> None:
    bad = "\\documentclass{article}\\begin{document}\\undefinedthing\\end{document}\n"
    with pytest.raises(RenderError, match="Undefined control sequence"):
        compile_pdf(bad, tmp_path, "bad")
    assert (tmp_path / "bad.tex").exists()


def test_engine_missing_still_writes_tex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    assert find_engine() is None
    tex = render_resume(full_doc(), load_style(None, "navy"))
    with pytest.raises(EngineMissing, match="Tectonic.*MiKTeX") as info:
        compile_pdf(tex, tmp_path / "out", "resume")
    assert "still written" in str(info.value)
    assert (tmp_path / "out" / "resume.tex").read_text(encoding="utf-8") == tex


def test_find_engine_prefers_requested(monkeypatch) -> None:
    on_path = {"pdflatex": "/bin/pdflatex", "xelatex": "/bin/xelatex"}
    monkeypatch.setattr(render.shutil, "which", lambda name: on_path.get(name))
    assert find_engine() == "/bin/pdflatex"
    assert find_engine("xelatex") == "/bin/xelatex"
    assert find_engine("tectonic") == "/bin/pdflatex"


# ----- page counting ------------------------------------------------------------


def test_count_pages_plain_and_compressed() -> None:
    plain = b"%PDF-1.5\n1 0 obj\n<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>\nendobj\n"
    assert count_pdf_pages(plain) == 2
    pages_only = b"<< /Type /Page /Parent 1 0 R >> << /Type/Page >> << /Type /Pages >>"
    assert count_pdf_pages(pages_only) == 2
    inner = zlib.compress(b"<< /Type /Pages /Count 3 /Kids [4 0 R 5 0 R 6 0 R] >>")
    packed = b"%PDF-1.5\n7 0 obj\n<< /Type /ObjStm /Filter /FlateDecode >>\nstream\n"
    packed += inner + b"\nendstream\nendobj\n"
    assert count_pdf_pages(packed) == 3
    assert count_pdf_pages(b"%PDF garbage", "Output written on x.pdf (4 pages, 1234 bytes).") == 4
    assert count_pdf_pages(b"%PDF garbage") == 0


@needs_engine
def test_count_pages_of_real_two_page_pdf(tmp_path: Path) -> None:
    tex = "\\documentclass{article}\\begin{document}one\\newpage two\\end{document}\n"
    result = compile_pdf(tex, tmp_path, "two")
    assert result.pages == 2
    assert count_pdf_pages(result.pdf) == 2


# ----- fitting to one page ------------------------------------------------------


def fake_compiler(max_lines: int, calls: list[ResumeDoc]):
    """Pretends the page overflows while the doc has more than ``max_lines`` lines."""

    def compile_fn(doc: ResumeDoc) -> Compiled:
        calls.append(doc.model_copy(deep=True))
        pages = 1 if len(doc.lines()) <= max_lines else 2
        return Compiled(pdf=Path("x.pdf"), tex=Path("x.tex"), pages=pages)

    return compile_fn


def test_fit_removal_order() -> None:
    doc = full_doc()
    before = doc.model_dump()
    total = len(doc.lines())  # 15
    calls: list[ResumeDoc] = []
    trimmed, result, notes = fit_to_one_page(doc, fake_compiler(total - 5, calls))
    assert result.pages == 1
    assert doc.model_dump() == before  # input untouched
    removed = [n for n in notes if n.startswith("Removed from")]
    # priority 3 first: the 4-line Northwind block, then the 3-line blocks (the later one
    # wins a tie), then the 2-line TA block; then priority 2 from the largest block.
    expected = [
        ("Northwind Analytics", "Wrote runbooks"),
        ("Pathfinder -- Warehouse Route Planner", "Visualised routes"),
        ("Contoso Robotics", "Automated hardware-in-the-loop"),
        ("Example State University", "Wrote autograder"),
        ("Northwind Analytics", "Added integration tests"),
    ]
    assert len(removed) == len(expected)
    for note, (title, start) in zip(removed, expected, strict=True):
        assert note.startswith(f'Removed from {title}: "{start}')
    assert len(trimmed.lines()) == total - 5
    assert len(calls) == 6


def test_fit_never_cuts_priority_one_and_drops_empty_projects() -> None:
    doc = full_doc()
    calls: list[ResumeDoc] = []
    trimmed, result, notes = fit_to_one_page(doc, fake_compiler(0, calls), max_rounds=50)
    assert result.pages == 2
    assert notes[-1].startswith("Still 2 pages")
    for section in trimmed.sections:
        for block in section.blocks:
            cuttable = [line for line in block.lines if line.priority > 1]
            # only a non-project block's last remaining line may be above priority 1
            assert not cuttable or (len(block.lines) == 1 and section.key != "projects")
    # the priority-2 project lost its only line and was removed; the TA block kept one line
    project_titles = [b.title for b in trimmed.sections[2].blocks]
    assert project_titles == ["Pathfinder -- Warehouse Route Planner"]
    assert any("Removed the project Tiny Ray Tracer" in n for n in notes)
    ta = trimmed.sections[1].blocks[2]
    assert len(ta.lines) == 1


def test_fit_stops_after_max_rounds() -> None:
    calls: list[ResumeDoc] = []
    trimmed, result, notes = fit_to_one_page(full_doc(), fake_compiler(0, calls), max_rounds=2)
    assert len([n for n in notes if n.startswith("Removed from")]) == 2
    assert len(calls) == 3 and result.pages == 2
    assert "Still 2 pages" in notes[-1]


@needs_engine
def test_fit_real_overlong_document(tmp_path: Path) -> None:
    doc = full_doc()
    long = (
        "Profiled and tuned a hot loop in the order-matching path, removing allocations "
        "and halving tail latency under synthetic load across three services"
    )
    doc.sections[1].blocks[2].lines += [ln(long, 1) for _ in range(6)]
    doc.sections[1].blocks[1].lines += [ln(long, 3) for _ in range(5)]
    style = load_style(None, "navy")
    counter = iter(range(100))

    def compile_fn(d: ResumeDoc) -> Compiled:
        return compile_pdf(render_resume(d, style), tmp_path, f"fit{next(counter)}")

    first = compile_fn(doc)
    assert first.pages == 2
    trimmed, result, notes = fit_to_one_page(doc, compile_fn)
    assert result.pages == 1
    assert notes and all("Still" not in n for n in notes)
    assert len(trimmed.lines()) < len(doc.lines())
