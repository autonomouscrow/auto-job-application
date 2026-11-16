"""The check every generated sentence must pass before it can reach a document.

No model is involved. A sentence is accepted only when it cites facts from
the profile and says nothing specific that those facts do not say:

* every cited fact exists (and, on a resume, belongs to the entry the bullet
  sits under);
* every number, written in digits or as a word, appears in the cited facts;
* every name (a word with a capital letter, a digit, or a symbol in it) and
  every known skill or technology appears in the cited facts, in the skills
  the applicant tagged those facts with, or in the entry's own heading;
* enough of the sentence's ordinary words come from the cited facts that it
  is a restatement of them and not a new story with a citation attached.

Rephrasing passes. Adding a tool, a metric, a team size, or an employer does
not. The rules lean toward rejecting: a rejected bullet falls back to the
applicant's own wording of the fact, which is always true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobtool.normalize import fold
from jobtool.profile.models import Education, Experience, Fact, Profile, Project
from jobtool.resume.lexicon import TERMS

MAX_BULLET_CHARS = 320
MAX_PARAGRAPH_CHARS = 1200
# Share of a bullet's longer words that must also be in the facts it cites.
MIN_OVERLAP = 0.4

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER_WORDS = set(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred hundreds thousand thousands million millions billion billions dozen dozens "
    "half double doubled doubling triple tripled tripling twice thrice".split()
)
# Words a number may be spelled with that are harmless on their own.
_HARMLESS_NUMBER_WORDS = {"one"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#']*")
_SENTENCE_START = re.compile(r"(?:^|[.;:!?]\s+|\(\s*)$")
_LINK = re.compile(r"https?://|www\.|\S+@\S+\.\S+", re.IGNORECASE)
_FORBIDDEN = re.compile(r"[\\{}<>$\n\r\t]")
_STOPWORDS = set(
    "about above across after again against along among around because before behind below "
    "between beyond during every from have having into itself more most onto other over same "
    "should such than that their them then there these they this those through under until "
    "upon using very were what when where which while with within without would your also "
    "been being both each used team teams work working worked".split()
)


@dataclass
class Vocabulary:
    """Skill and technology names that must never appear without a fact behind them."""

    terms: set[str] = field(default_factory=set)  # folded

    @classmethod
    def build(cls, profile: Profile, extra: list[str] | None = None) -> Vocabulary:
        terms = {fold(term) for term in TERMS}
        for skill in profile.skills:
            terms.update(fold(name) for name in (skill.name, *skill.aliases))
        terms.update(fold(name) for name in extra or [])
        return cls({term for term in terms if len(term) >= 2})


def strip_markup(text: str) -> str:
    return text.replace("**", "")


def _contains(haystack: str, term: str) -> bool:
    return f" {term} " in f" {haystack} "


def entry_of(profile: Profile, entry_id: str) -> Education | Experience | Project | None:
    return next((entry for entry in profile.entries() if entry.id == entry_id), None)


def _entry_heading(profile: Profile, entry: Education | Experience | Project) -> str:
    """The parts of an entry that are printed as its heading, plus its own skill names."""
    names = {skill.id: skill for skill in profile.skills}
    parts: list[str] = []
    if isinstance(entry, Education):
        parts += [entry.school, entry.degree]
    elif isinstance(entry, Experience):
        parts += [entry.org, entry.role]
    else:
        parts += [entry.name, entry.summary]
        for skill_id in entry.skills:
            if skill_id in names:
                parts += [names[skill_id].name, *names[skill_id].aliases]
    return " ".join(parts)


def support_text(profile: Profile, facts: list[Fact]) -> str:
    """Everything the cited facts say, including the skills the applicant tagged them with."""
    names = {skill.id: skill for skill in profile.skills}
    parts = [fact.text for fact in facts]
    for fact in facts:
        for skill_id in fact.skills:
            if skill_id in names:
                parts += [names[skill_id].name, *names[skill_id].aliases]
    return " ".join(parts)


def _numbers(text: str) -> set[str]:
    return {match.replace(",", "").rstrip(".") for match in _NUMBER.findall(text)}


def _names(text: str) -> list[str]:
    """Words that read as names: an inner capital, a digit, a symbol, or a capital mid-sentence."""
    found = []
    for match in _TOKEN.finditer(text):
        word = match.group().strip("'")
        if len(word) < 2 and not word.isdigit():
            if word not in ("C", "R"):
                continue
        if word.isdigit():
            continue  # numbers are checked on their own
        at_start = bool(_SENTENCE_START.search(text[: match.start()]))
        inner_capital = any(ch.isupper() for ch in word[1:])
        odd = any(ch.isdigit() or ch in "+#" for ch in word)
        capital = word[0].isupper()
        if inner_capital or odd or (capital and not at_start):
            found.append(word)
    return found


def _overlap(text: str, support: str) -> float:
    words = [w for w in fold(text).split() if len(w) >= 4 and w not in _STOPWORDS]
    if not words:
        return 1.0
    stems = {w[:5] for w in fold(support).split()}
    return sum(1 for w in words if w[:5] in stems) / len(words)


def check_text(
    text: str,
    *,
    profile: Profile,
    facts: list[Fact],
    vocabulary: Vocabulary,
    heading: str = "",
    context: str = "",
    min_overlap: float = MIN_OVERLAP,
    max_chars: int = MAX_BULLET_CHARS,
) -> list[str]:
    """Problems with one sentence or paragraph, given the facts it cites. Empty means it passes.

    ``heading`` is text printed right beside the sentence (the entry's heading).
    ``context`` is text about the employer (company, role, posting): names from it may be
    mentioned, but it never backs up a skill, a technology, or a number.
    """
    problems: list[str] = []
    plain = strip_markup(text).strip()
    if not plain:
        return ["it is empty"]
    if len(plain) > max_chars:
        problems.append(f"it is {len(plain)} characters long (the limit is {max_chars})")
    if _FORBIDDEN.search(plain):
        problems.append("it contains characters that do not belong in a sentence")
    if text.count("**") % 2:
        problems.append("its bold markers are not paired")

    backing = support_text(profile, facts) + " " + heading
    backing_folded = fold(backing)
    context_folded = fold(context)

    if _LINK.search(plain) and not any(
        link in backing for link in re.findall(r"\S+", plain) if _LINK.search(link)
    ):
        problems.append("it contains a link or an address that is not in the cited facts")

    known_numbers = _numbers(backing) | _numbers(heading)
    for number in sorted(_numbers(plain) - known_numbers):
        problems.append(f"the number {number} is not in the cited facts")
    spelled = {w for w in fold(plain).split() if w in _NUMBER_WORDS} - _HARMLESS_NUMBER_WORDS
    for word in sorted(spelled - set(backing_folded.split())):
        problems.append(f"the amount '{word}' is not in the cited facts")

    reported: set[str] = set()
    for term in sorted(vocabulary.terms):
        if _contains(fold(plain), term) and not _contains(backing_folded, term):
            reported.add(term)
            problems.append(f"'{term}' is a skill or technology the cited facts do not mention")
    for name in _names(plain):
        folded = fold(name)
        if not folded or folded in reported or any(folded in term.split() for term in reported):
            continue
        if _contains(backing_folded, folded) or _contains(context_folded, folded):
            continue
        reported.add(folded)
        problems.append(f"'{name}' does not appear in the cited facts")

    if facts and _overlap(plain, backing + " " + context) < min_overlap:
        problems.append("most of its wording does not come from the cited facts")
    return problems


def check_bullet(
    text: str,
    fact_ids: list[str],
    *,
    profile: Profile,
    entry_id: str,
    vocabulary: Vocabulary,
) -> list[str]:
    """Problems with one resume bullet placed under the given entry."""
    entry = entry_of(profile, entry_id)
    if entry is None:
        return [f"there is no entry '{entry_id}' in the profile"]
    if not fact_ids:
        return ["it cites no facts"]
    own = {fact.id: fact for fact in entry.facts}
    problems = [
        f"fact '{fact_id}' is not one of this entry's facts"
        for fact_id in fact_ids
        if fact_id not in own
    ]
    facts = [own[fact_id] for fact_id in fact_ids if fact_id in own]
    if not facts:
        return problems
    return problems + check_text(
        text, profile=profile, facts=facts, vocabulary=vocabulary,
        heading=_entry_heading(profile, entry),
    )  # fmt: skip


def check_paragraph(
    text: str,
    fact_ids: list[str],
    *,
    profile: Profile,
    vocabulary: Vocabulary,
    context: str,
    own_words: str = "",
) -> list[str]:
    """Problems with one cover-letter paragraph. ``context`` describes the employer.

    ``own_words`` is text the applicant wrote themselves (saved answers, stories).
    It backs a claim the same way a cited fact does.
    """
    known = profile.facts()
    problems = [
        f"fact '{fact_id}' is not in the profile" for fact_id in fact_ids if fact_id not in known
    ]
    facts = [known[fact_id] for fact_id in fact_ids if fact_id in known]
    # A letter names where the cited work was done, so those headings count as backing.
    headings = " ".join(
        _entry_heading(profile, entry)
        for entry in profile.entries()
        if any(fact.id in fact_ids for fact in entry.facts)
    )
    headings = f"{headings} {own_words}"
    return problems + check_text(
        text, profile=profile, facts=facts, vocabulary=vocabulary, heading=headings,
        context=context, min_overlap=0.0, max_chars=MAX_PARAGRAPH_CHARS,
    )  # fmt: skip
