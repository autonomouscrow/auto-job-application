"""Turn a ``ResumeDoc`` or ``LetterDoc`` into LaTeX, and LaTeX into a PDF.

A style is a folder holding ``resume.tex.j2``, ``letter.tex.j2`` and ``style.yaml``.
Built-in styles live next to this module in ``styles/``; a user can add their own in
``<home>/resume/styles/<name>/``, and a user style with a built-in name replaces it.

Templates use LaTeX-friendly Jinja delimiters (``\\VAR{...}``, ``\\BLOCK{...}``, ``%#``
line comments) with autoescape off. Every piece of document text must go through the
``tex`` filter (or ``rich`` for bullets, which also turns ``**bold**`` into ``\\textbf``).
Text can come from a language model, so ``tex_escape`` never lets a command through.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from jinja2 import FileSystemLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from jobtool.resume.model import LetterDoc, ResumeDoc

BUILTIN_STYLES = Path(__file__).resolve().parent / "styles"
ENGINES = ("tectonic", "pdflatex", "xelatex", "lualatex")
DEFAULT_TIMEOUT = 180.0


class RenderError(RuntimeError):
    """A style could not be loaded, a document could not be rendered or compiled."""


class EngineMissing(RenderError):
    """No LaTeX engine was found on PATH."""


# ----- escaping ----------------------------------------------------------------

_ASCII_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
    "|": r"\textbar{}",
}

_UNICODE = {
    " ": "~",  # no-break space
    " ": "~",
    " ": r"\,",  # narrow no-break space
    " ": r"\,",  # thin space
    " ": r"\,",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "‐": "-",
    "‑": "-",
    "‒": "--",
    "–": "--",
    "—": "---",
    "―": "---",
    "−": "$-$",
    "‘": "`",
    "’": "'",
    "‚": ",",
    "‛": "`",
    "“": "``",
    "”": "''",
    "„": ",,",
    "′": "'",
    "″": "''",
    "«": r"\guillemotleft{}",
    "»": r"\guillemotright{}",
    "‹": r"\guilsinglleft{}",
    "›": r"\guilsinglright{}",
    "…": r"\ldots{}",
    "•": r"\textbullet{}",
    "·": r"$\cdot$",
    "⋅": r"$\cdot$",
    "×": r"$\times$",
    "÷": r"$\div$",
    "±": r"$\pm$",
    "≈": r"$\approx$",
    "∼": r"$\sim$",
    "≤": r"$\leq$",
    "≥": r"$\geq$",
    "≠": r"$\neq$",
    "∞": r"$\infty$",
    "→": r"$\rightarrow$",
    "←": r"$\leftarrow$",
    "↔": r"$\leftrightarrow$",
    "⇒": r"$\Rightarrow$",
    "µ": r"$\mu$",
    "μ": r"$\mu$",
    "°": r"\textdegree{}",
    "€": r"\texteuro{}",
    "£": r"\pounds{}",
    "©": r"\textcopyright{}",
    "®": r"\textregistered{}",
    "™": r"\texttrademark{}",
    "§": r"\S{}",
    "¶": r"\P{}",
    "¹": r"\textsuperscript{1}",
    "²": r"\textsuperscript{2}",
    "³": r"\textsuperscript{3}",
    "½": "1/2",
    "¼": "1/4",
    "¾": "3/4",
    "¿": r"?`",
    "¡": r"!`",
    "ß": r"\ss{}",
    "æ": r"\ae{}",
    "Æ": r"\AE{}",
    "œ": r"\oe{}",
    "Œ": r"\OE{}",
    "ø": r"\o{}",
    "Ø": r"\O{}",
    "ł": r"\l{}",
    "Ł": r"\L{}",
    "ı": r"\i{}",
    "þ": r"\th{}",
    "Þ": r"\TH{}",
    "ð": r"\dh{}",
    "Ð": r"\DH{}",
    "đ": r"\dj{}",
    "Đ": r"\DJ{}",
}

# Combining marks that LaTeX text accents can draw on top of an ASCII letter.
_ACCENTS = {
    "̀": "`",
    "́": "'",
    "̂": "^",
    "̃": "~",
    "̄": "=",
    "̆": "u",
    "̇": ".",
    "̈": '"',
    "̊": "r",
    "̋": "H",
    "̌": "v",
    "̧": "c",
    "̨": "k",
}

_WHITESPACE = re.compile(r"[ \t\n\r\f\v\u0085  ]+")
_OPENERS = set(" ([{/-—–")


def _accented(base: str, marks: list[str]) -> str:
    out = r"\i" if base == "i" and marks else r"\j" if base == "j" and marks else base
    for mark in marks:
        out = f"\\{_ACCENTS[mark]}{{{out}}}"
    return out


def _letter(ch: str) -> str:
    """An accented letter as LaTeX accent commands, or '' if it cannot be drawn."""
    parts = unicodedata.normalize("NFD", ch)
    base, marks = parts[0], list(parts[1:])
    if base in _UNICODE and not marks:
        return _UNICODE[base]
    if not (base.isascii() and base.isalpha()):
        return ""
    if not all(m in _ACCENTS for m in marks):
        return base  # keep the letter, lose an accent LaTeX cannot draw
    return _accented(base, marks)


def tex_escape(text: object) -> str:
    """Escape any text so that LaTeX prints it literally and runs nothing.

    The result is pure ASCII built from a fixed set of safe commands. Characters that
    have no safe LaTeX form (emoji, most non-Latin scripts) are dropped.
    """
    if text is None:
        return ""
    source = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", str(text)))
    out: list[str] = []
    for i, ch in enumerate(source):
        if ch == '"':
            j = i - 1
            while j >= 0 and source[j] == "*":
                j -= 1
            opening = j < 0 or source[j] in _OPENERS
            if opening:
                prev = out[-1][-1:] if out else ""
                out.append("{}``" if prev in ("!", "?") else "``")
            else:
                out.append("''")
        elif ch == "~":
            nxt = source[i + 1 : i + 2]
            out.append(r"$\sim$" if nxt.isdigit() or nxt == "." else r"\textasciitilde{}")
        elif ch in _ASCII_SPECIAL:
            out.append(_ASCII_SPECIAL[ch])
        elif " " <= ch <= "~":
            if ch == "`" and out and out[-1][-1:] in ("!", "?"):
                out.append("{}")
            out.append(ch)
        elif ch in _UNICODE:
            out.append(_UNICODE[ch])
        elif unicodedata.category(ch).startswith("L"):
            out.append(_letter(ch))
        # control, format, private-use, combining leftovers, emoji: dropped
    return "".join(out)


_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def tex_rich(text: object, bold: bool = True) -> str:
    """Escape text, then turn balanced ``**bold**`` spans into ``\\textbf{...}``.

    With ``bold=False`` the spans are kept as plain text. Unbalanced asterisks stay
    literal. Escaping happens first, so the bold content is already safe.
    """
    escaped = tex_escape(text)
    return _BOLD.sub(lambda m: rf"\textbf{{{m.group(1)}}}" if bold else m.group(1), escaped)


_URL_SCHEMES = ("http://", "https://", "mailto:", "tel:")
_URL_SAFE = "-._~:/?#@!&'()*+,;=%"


def tex_url(url: object) -> str:
    """A link target that is safe inside ``\\href{}``, or '' when it should not be linked.

    Only http(s), mailto and tel links are allowed. Anything outside a plain URL
    character set is percent-encoded, and ``%`` / ``#`` are escaped for LaTeX.
    """
    raw = "".join(ch for ch in str(url or "").strip() if ch.isprintable() and not ch.isspace())
    if not raw:
        return ""
    lower = raw.lower()
    if not lower.startswith(_URL_SCHEMES):
        if re.match(r"^[a-z][a-z0-9+.-]*:", lower) and not re.match(r"^[^:/]+:\d", lower):
            return ""  # some other scheme, such as javascript: or file:
        raw = "https://" + raw
    encoded = quote(raw, safe=_URL_SAFE)
    encoded = encoded.replace("'", "%27").replace("~", "%7E")
    return encoded.replace("%", r"\%").replace("#", r"\#")


# ----- styles ------------------------------------------------------------------

OPTION_DEFAULTS: dict[str, Any] = {
    "accent": "1F3A5F",
    "font_size": 10,
    "margin": 0.5,
    "bold_highlights": True,
}


@dataclass
class StyleInfo:
    name: str
    label: str
    description: str
    options: dict[str, Any]
    built_in: bool
    path: Path = field(repr=False, default=Path())


_STYLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def user_styles_dir(home: Path) -> Path:
    return Path(home) / "resume" / "styles"


def _read_style(folder: Path, built_in: bool) -> StyleInfo | None:
    if not (folder / "resume.tex.j2").is_file():
        return None
    meta: dict[str, Any] = {}
    meta_path = folder / "style.yaml"
    if meta_path.is_file():
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RenderError(
                f"style '{folder.name}': style.yaml is not valid YAML: {exc}"
            ) from exc
        if not isinstance(meta, dict):
            raise RenderError(f"style '{folder.name}': style.yaml must be a mapping")
    options = {**OPTION_DEFAULTS, **(meta.get("options") or {})}
    return StyleInfo(
        name=folder.name,
        label=str(meta.get("label") or folder.name.title()),
        description=str(meta.get("description") or ""),
        options=options,
        built_in=built_in,
        path=folder,
    )


def _style_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and _STYLE_NAME.match(p.name))


def list_styles(home: Path | None) -> list[StyleInfo]:
    """Every available style, user styles replacing built-ins of the same name."""
    found: dict[str, StyleInfo] = {}
    for folder in _style_folders(BUILTIN_STYLES):
        info = _read_style(folder, built_in=True)
        if info:
            found[info.name] = info
    if home is not None:
        for folder in _style_folders(user_styles_dir(home)):
            info = _read_style(folder, built_in=False)
            if info:
                found[info.name] = info
    return sorted(found.values(), key=lambda s: (not s.built_in, s.name))


def load_style(home: Path | None, name: str) -> StyleInfo:
    """The style called ``name``, looking in the home folder first."""
    if not isinstance(name, str) or not _STYLE_NAME.match(name):
        raise RenderError(f"'{name}' is not a valid style name")
    roots = [(BUILTIN_STYLES, True)]
    if home is not None:
        roots.insert(0, (user_styles_dir(home), False))
    for root, built_in in roots:
        info = _read_style(root / name, built_in)
        if info:
            return info
    known = ", ".join(s.name for s in list_styles(home)) or "none"
    raise RenderError(f"unknown resume style '{name}' (available: {known})")


def resolve_options(style: StyleInfo, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """The style's defaults with ``options`` on top, checked and normalised."""
    merged = {**style.options, **{k: v for k, v in (options or {}).items() if v is not None}}
    accent = str(merged["accent"]).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", accent):
        raise RenderError(f"accent must be a 6-digit hex colour such as 1F3A5F, not {accent!r}")
    merged["accent"] = accent.upper()
    try:
        size = int(merged["font_size"])
    except (TypeError, ValueError):
        size = 0
    if size not in (10, 11, 12):
        raise RenderError(f"font_size must be 10, 11 or 12, not {merged['font_size']!r}")
    merged["font_size"] = size
    try:
        margin = float(merged["margin"])
    except (TypeError, ValueError):
        margin = -1.0
    if not 0.2 <= margin <= 2.0:
        raise RenderError(f"margin must be between 0.2 and 2.0 inches, not {merged['margin']!r}")
    merged["margin"] = f"{margin:.3f}".rstrip("0").rstrip(".")
    bold = merged["bold_highlights"]
    if isinstance(bold, str):
        bold = bold.strip().lower() in ("1", "true", "yes", "on")
    merged["bold_highlights"] = bool(bold)
    return merged


def _environment(style: StyleInfo) -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        loader=FileSystemLoader(str(style.path)),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        line_comment_prefix="%#",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["tex"] = tex_escape
    env.filters["url"] = tex_url
    return env


def _render(template: str, style: StyleInfo, options: dict[str, Any] | None, **ctx: Any) -> str:
    if isinstance(style, str):
        style = load_style(None, style)
    opt = resolve_options(style, options)
    env = _environment(style)
    env.filters["rich"] = lambda text: tex_rich(text, bold=opt["bold_highlights"])
    try:
        return env.get_template(template).render(opt=opt, **ctx)
    except TemplateError as exc:
        raise RenderError(f"style '{style.name}', {template}: {exc}") from exc


def render_resume(doc: ResumeDoc, style: StyleInfo, options: dict[str, Any] | None = None) -> str:
    """LaTeX source for a resume in ``style``."""
    return _render("resume.tex.j2", style, options, doc=doc)


def render_letter(doc: LetterDoc, style: StyleInfo, options: dict[str, Any] | None = None) -> str:
    """LaTeX source for a cover letter in ``style``."""
    return _render("letter.tex.j2", style, options, doc=doc)


# ----- compiling ---------------------------------------------------------------


@dataclass
class Compiled:
    pdf: Path
    tex: Path
    pages: int
    log_tail: str = ""


def find_engine(preferred: str | None = None) -> str | None:
    """The path of a LaTeX engine on PATH, trying ``preferred`` first."""
    candidates = ([preferred] if preferred else []) + list(ENGINES)
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


_PAGES_TREE = re.compile(rb"/Type\s*/Pages\b(?:(?!endobj).)*?/Count\s+(\d+)", re.S)
_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_LOG_PAGES = re.compile(r"Output written on .*?\((\d+) pages?", re.S)


def _count_in(data: bytes) -> int:
    counts = [int(n) for n in _PAGES_TREE.findall(data)]
    if counts:
        return max(counts)
    return len(_PAGE_OBJ.findall(data))


def count_pdf_pages(pdf: Path | bytes, log: str = "") -> int:
    """Pages in a PDF, read from the file itself.

    Looks for the page tree's ``/Count`` (or counts ``/Type /Page`` objects), also
    inside Flate-compressed object streams. Falls back to the engine's log line
    "Output written on x.pdf (N pages" and returns 0 if nothing is found.
    """
    data = pdf if isinstance(pdf, bytes) else Path(pdf).read_bytes()
    pages = _count_in(data)
    if not pages:
        for match in _STREAM.finditer(data):
            try:
                inflated = zlib.decompress(match.group(1))
            except zlib.error:
                continue
            pages = max(pages, _count_in(inflated))
    if not pages and log:
        found = _LOG_PAGES.findall(log.replace("\n", ""))
        if found:
            pages = int(found[-1])
    return pages


def _first_error(log: str) -> str:
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("! ") or line.startswith("error:"):
            detail = line.lstrip("! ").strip()
            for follow in lines[i + 1 : i + 6]:
                if re.match(r"l\.\d+", follow):
                    detail += f" ({follow.strip()})"
                    break
            return detail
    tail = [ln for ln in lines if ln.strip()][-3:]
    return " / ".join(tail) or "no log was written"


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def compile_pdf(
    tex: str,
    out_dir: Path,
    name: str,
    engine: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Compiled:
    """Write ``<out_dir>/<name>.tex`` and compile it to ``<out_dir>/<name>.pdf``.

    The engine runs in a temporary folder with shell escape off and TeX's file access
    limited to that folder. Raises ``EngineMissing`` (after writing the .tex) when no
    engine is installed, and ``RenderError`` with the first LaTeX error on failure.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _SAFE_NAME.sub("_", name).strip("._") or "document"
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    exe = find_engine(engine)
    if exe is None:
        raise EngineMissing(
            "No LaTeX engine found (looked for " + ", ".join(ENGINES) + "). "
            "Install Tectonic (https://tectonic-typesetting.github.io) or MiKTeX "
            "(https://miktex.org), or TeX Live, then try again. "
            f"The LaTeX source was still written to {tex_path}."
        )

    is_tectonic = Path(exe).stem.lower() == "tectonic"
    env = {
        **os.environ,
        "shell_escape": "f",
        "openin_any": "p",
        "openout_any": "p",
    }
    with tempfile.TemporaryDirectory(prefix="jobtool-tex-") as tmp:
        build = Path(tmp)
        source = build / f"{stem}.tex"
        source.write_text(tex, encoding="utf-8")
        if is_tectonic:
            cmd = [exe, "--keep-logs", "--outdir", str(build), source.name]
        else:
            cmd = [
                exe,
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-jobname={stem}",
                source.name,
            ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=build,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(
                f"LaTeX took longer than {timeout:.0f} seconds and was stopped"
            ) from exc
        except OSError as exc:
            raise RenderError(f"could not run {exe}: {exc}") from exc

        log_path = build / f"{stem}.log"
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        output = (proc.stdout or b"").decode("utf-8", "replace")
        output += (proc.stderr or b"").decode("utf-8", "replace")
        log_tail = "\n".join((log or output).splitlines()[-40:])
        pdf_built = build / f"{stem}.pdf"
        if proc.returncode != 0 or not pdf_built.exists():
            raise RenderError(f"LaTeX failed: {_first_error(log or output)}")
        pdf_path = out_dir / f"{stem}.pdf"
        shutil.copyfile(pdf_built, pdf_path)
        pages = count_pdf_pages(pdf_path, log or output)
    return Compiled(pdf=pdf_path, tex=tex_path, pages=pages, log_tail=log_tail)


# ----- fitting -----------------------------------------------------------------


def _quote(text: str, limit: int = 48) -> str:
    plain = _BOLD.sub(lambda m: m.group(1), " ".join(text.split()))
    return f'"{plain}"' if len(plain) <= limit else f'"{plain[:limit].rstrip()}..."'


def _cut_candidate(doc: ResumeDoc) -> tuple[int, int, int] | None:
    """(section, block, line) indices of the next line to cut, or None."""
    best_key: tuple[int, int, int] | None = None
    best: tuple[int, int, int] | None = None
    order = 0
    for s_index, section in enumerate(doc.sections):
        is_project = section.key == "projects"
        for b_index, block in enumerate(section.blocks):
            order += 1
            if not block.lines or (len(block.lines) == 1 and not is_project):
                continue
            cuttable = [i for i, ln in enumerate(block.lines) if ln.priority > 1]
            if not cuttable:
                continue
            top = max(block.lines[i].priority for i in cuttable)
            l_index = max(i for i in cuttable if block.lines[i].priority == top)
            key = (top, len(block.lines), order)
            if best_key is None or key > best_key:
                best_key, best = key, (s_index, b_index, l_index)
    return best


def fit_to_one_page(
    doc: ResumeDoc,
    compile_fn: Callable[[ResumeDoc], Compiled],
    max_rounds: int = 8,
) -> tuple[ResumeDoc, Compiled, list[str]]:
    """Cut lines one at a time until the resume compiles to one page.

    Highest ``priority`` number goes first; among equals, the last line of the block
    with the most lines (later blocks win remaining ties). Priority-1 lines are never
    cut. A project left without lines is dropped; other blocks keep their last line.
    Returns a trimmed copy, the last compile result, and notes on what was cut.
    """
    work = doc.model_copy(deep=True)
    notes: list[str] = []
    result = compile_fn(work)
    rounds = 0
    while result.pages > 1 and rounds < max_rounds:
        pick = _cut_candidate(work)
        if pick is None:
            break
        s_index, b_index, l_index = pick
        section = work.sections[s_index]
        block = section.blocks[b_index]
        line = block.lines.pop(l_index)
        notes.append(f"Removed from {block.title}: {_quote(line.text)}")
        if not block.lines and section.key == "projects":
            section.blocks.pop(b_index)
            notes.append(f"Removed the project {block.title}, which had no bullets left.")
        rounds += 1
        result = compile_fn(work)
    if result.pages > 1:
        notes.append(
            f"Still {result.pages} pages after cutting {rounds} line(s); nothing more could be "
            "cut automatically. Shorten it by hand or pick a tighter style or smaller margins."
        )
    return work, result, notes


__all__ = [
    "BUILTIN_STYLES",
    "Compiled",
    "EngineMissing",
    "RenderError",
    "StyleInfo",
    "compile_pdf",
    "count_pdf_pages",
    "find_engine",
    "fit_to_one_page",
    "list_styles",
    "load_style",
    "render_letter",
    "render_resume",
    "resolve_options",
    "tex_escape",
    "tex_rich",
    "tex_url",
    "user_styles_dir",
]
