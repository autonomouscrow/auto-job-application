"""Normalising text and URLs so the same job from two repos looks the same."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only record where a click came from.
TRACKING_PARAMS = {
    "ref", "src", "source", "gh_src", "cmpid", "iis", "iisn", "trk", "trackingid",
    "refid", "mobile", "needsredirect", "lever-source", "lever-origin", "jobboardsource",
    "indeed-apply-token", "sid", "fbclid", "gclid", "mode", "codes",
}  # fmt: skip
TRACKING_PREFIXES = ("utm_", "pk_", "mc_")

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|group|plc|gmbh|sa|ag|"
    r"holdings|technologies|technology|the)\b"
)


def canonical_url(url: str) -> str:
    """Strip tracking parameters, fragments, and cosmetic differences."""
    url = url.strip()
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    host = parts.netloc.lower().removeprefix("www.")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def listing_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:24]


def fold(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_company(name: str) -> str:
    folded = _COMPANY_SUFFIXES.sub(" ", fold(name))
    return re.sub(r"\s+", " ", folded).strip() or fold(name)


def norm_title(title: str) -> str:
    folded = fold(title)
    folded = re.sub(r"\b(20\d\d|summer|intern|internship|interns|co op|coop)\b", " ", folded)
    return re.sub(r"\s+", " ", folded).strip() or fold(title)
