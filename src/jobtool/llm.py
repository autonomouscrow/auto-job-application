"""The Claude API: the key, one structured call, and plain-language problems.

Nothing here ever prints or stores the API key. When a call fails, the cause
is turned into an :class:`LlmProblem` with a ``kind`` the rest of the app can
act on, and the latest problem is kept in the database so the web app can
show it as a banner until a call succeeds again.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel
from sqlmodel import Session

from jobtool import db

KEY_NAME = "ANTHROPIC_API_KEY"
CONSOLE = "https://console.anthropic.com"

MISSING_KEY, BAD_KEY, BILLING, PERMISSION = "missing_key", "bad_key", "billing", "permission"
MODEL, RATE_LIMIT, UNAVAILABLE, OTHER = "model", "rate_limit", "unavailable", "other"
# Problems that will not go away by waiting: stop calling until the user acts.
NEEDS_ACTION = {MISSING_KEY, BAD_KEY, BILLING, PERMISSION, MODEL}

TITLES = {
    MISSING_KEY: "No Claude API key",
    BAD_KEY: "Your Claude API key was rejected",
    BILLING: "Your Claude account is out of credit",
    PERMISSION: "Your Claude API key is not allowed to do this",
    MODEL: "The configured Claude model was not found",
    RATE_LIMIT: "Claude is rate limiting this key",
    UNAVAILABLE: "Claude could not be reached",
    OTHER: "A Claude request failed",
}
ADVICE = {
    MISSING_KEY: (
        f"Create a key at {CONSOLE} (API keys), then put it in the .env file in your home "
        f"folder as {KEY_NAME}=your-key and choose Check again."
    ),
    BAD_KEY: (
        "It has expired, been revoked, or was mistyped. Create a new key at "
        f"{CONSOLE} (API keys), replace the one in the .env file in your home folder, "
        "and choose Check again."
    ),
    BILLING: f"Add credit at {CONSOLE} (Billing), then choose Check again.",
    PERMISSION: (
        f"Check the key's workspace and permissions at {CONSOLE}, or create a new key."
    ),
    MODEL: "Check the model names under 'llm' in config.yaml.",
    RATE_LIMIT: "Nothing to do. Reading continues on its own in a little while.",
    UNAVAILABLE: "Check your internet connection. Reading is retried automatically.",
    OTHER: "Reading is retried automatically. Run 'jobs doctor' if this keeps happening.",
}
# US dollars per million tokens (input, output), for the spending estimate only.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0)}

_STATUS_KEY = "llm_status"
T = TypeVar("T", bound=BaseModel)


class LlmProblem(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{TITLES[kind]}. {detail}".strip())
        self.kind = kind
        self.detail = detail

    @property
    def needs_action(self) -> bool:
        return self.kind in NEEDS_ACTION


@dataclass
class Reply[T]:
    parsed: T
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class Reader(Protocol):
    """What the rest of the app needs from a model. Tests supply a fake."""

    model: str

    def check(self) -> None: ...

    def parse(self, *, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> Reply[T]:
        ...


def load_key(home: Path) -> str | None:
    """The key from ``<home>/.env``, or else from the environment."""
    path = home / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.strip().removeprefix("export ").partition("=")
            if name.strip() == KEY_NAME:
                value = value.strip().strip("\"'")
                if value:
                    return value
    return os.environ.get(KEY_NAME) or None


def translate(exc: Exception) -> LlmProblem:
    """Turn an SDK error into a problem the user can act on. Never includes the key."""
    import anthropic

    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error = body.get("error", {}) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    kind_name = str(error.get("type") or getattr(exc, "type", "") or "")
    said = str(error.get("message") or "")[:300]
    lowered = said.lower()

    if isinstance(exc, anthropic.AuthenticationError) or status == 401:
        return LlmProblem(BAD_KEY)
    if status == 402 or kind_name == "billing_error" or "credit balance" in lowered:
        return LlmProblem(BILLING)
    if isinstance(exc, anthropic.PermissionDeniedError) or status == 403:
        return LlmProblem(PERMISSION, said)
    if isinstance(exc, anthropic.NotFoundError) or status == 404:
        return LlmProblem(MODEL, said)
    if isinstance(exc, anthropic.RateLimitError) or status == 429:
        return LlmProblem(RATE_LIMIT)
    if isinstance(exc, anthropic.APIConnectionError):
        return LlmProblem(UNAVAILABLE)
    if isinstance(status, int) and status >= 500:
        return LlmProblem(UNAVAILABLE, "Anthropic's servers reported a problem.")
    return LlmProblem(OTHER, said or type(exc).__name__)


class ClaudeReader:
    """Structured calls to one Claude model."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=90.0)

    def check(self) -> None:
        """Confirm the key works and the model exists, without spending tokens."""
        import anthropic

        try:
            self._client.models.retrieve(self.model)
        except anthropic.AnthropicError as exc:
            raise translate(exc) from None

    def parse(self, *, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> Reply[T]:
        import anthropic

        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=[
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.AnthropicError as exc:
            raise translate(exc) from None
        if response.stop_reason == "refusal":
            raise LlmProblem(OTHER, "The model declined to read this text.")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise LlmProblem(OTHER, "The model's answer was cut off or unreadable.")
        usage = response.usage
        # Cached input is billed apart from fresh input; count all of it as input here.
        read = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        return Reply(response.parsed_output, self.model, read, usage.output_tokens or 0)


def reader_for(home: Path, model: str) -> ClaudeReader:
    key = load_key(home)
    if not key:
        raise LlmProblem(MISSING_KEY)
    return ClaudeReader(key, model)


# ----- the alert shown in the app -------------------------------------------


def record_problem(session: Session, problem: LlmProblem) -> None:
    payload = {
        "kind": problem.kind,
        "detail": problem.detail,
        "at": datetime.now().isoformat(timespec="minutes"),
    }
    db.set_meta(session, _STATUS_KEY, json.dumps(payload))


def record_ok(session: Session) -> None:
    if db.get_meta(session, _STATUS_KEY):
        db.set_meta(session, _STATUS_KEY, "")


def current_alert(session: Session, home: Path) -> dict | None:
    """The banner to show, or None when Claude is working as far as we know."""
    if not load_key(home):
        kind, detail, at = MISSING_KEY, "", ""
    else:
        raw = db.get_meta(session, _STATUS_KEY)
        if not raw:
            return None
        try:
            saved = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(saved, dict):
            return None
        kind, detail, at = saved.get("kind", OTHER), saved.get("detail", ""), saved.get("at", "")
        if kind == MISSING_KEY:
            return None  # a key has been added since
    return {
        "kind": kind,
        "title": TITLES.get(kind, TITLES[OTHER]),
        "advice": ADVICE.get(kind, ADVICE[OTHER]),
        "detail": detail,
        "at": at,
        "needs_action": kind in NEEDS_ACTION,
    }


def blocked(session: Session, home: Path) -> bool:
    """True when calling Claude is pointless until the user fixes something."""
    alert = current_alert(session, home)
    return bool(alert and alert["needs_action"])


def check_now(session: Session, home: Path, model: str) -> LlmProblem | None:
    """Test the key against the API and update the alert. Returns the problem, if any."""
    try:
        reader_for(home, model).check()
    except LlmProblem as problem:
        record_problem(session, problem)
        session.commit()
        return problem
    record_ok(session)
    session.commit()
    return None


def log_usage(session: Session, purpose: str, reply: Reply) -> None:
    session.add(
        db.Usage(
            at=datetime.now(),
            purpose=purpose,
            model=reply.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
    )


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
