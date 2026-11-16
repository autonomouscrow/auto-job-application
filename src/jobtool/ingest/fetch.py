"""Downloading source files from GitHub without authentication."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass

import httpx

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
TARBALL = "https://codeload.github.com/{repo}/tar.gz/{branch}"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
HEADERS = {"User-Agent": "jobtool"}


class FetchError(Exception):
    pass


@dataclass
class Fetched:
    """``content`` is None when the server says nothing changed since ``etag``."""

    content: str | dict[str, str] | None
    etag: str | None


def _get(url: str, etag: str | None) -> httpx.Response | None:
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    try:
        response = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise FetchError(f"could not reach {url}: {exc}") from exc
    if response.status_code == 304:
        return None
    if response.status_code == 404:
        raise FetchError(f"not found: {url}")
    if response.status_code >= 400:
        raise FetchError(f"{url} returned HTTP {response.status_code}")
    return response


def fetch_file(repo: str, branch: str, path: str, etag: str | None = None) -> Fetched:
    response = _get(RAW.format(repo=repo, branch=branch, path=path), etag)
    if response is None:
        return Fetched(None, etag)
    return Fetched(response.text, response.headers.get("etag"))


def extract_folder(archive: bytes, folder: str) -> dict[str, str]:
    """Read the text files directly inside ``folder`` of a GitHub tarball."""
    folder = folder.strip("/")
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            # Paths look like "<repo>-<sha>/<folder>/<file>".
            inner = member.name.split("/", 1)[1] if "/" in member.name else member.name
            parent, _, name = inner.rpartition("/")
            if parent != folder:
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                files[name] = handle.read().decode("utf-8", errors="replace")
    return files


def fetch_folder(repo: str, branch: str, path: str, etag: str | None = None) -> Fetched:
    response = _get(TARBALL.format(repo=repo, branch=branch), etag)
    if response is None:
        return Fetched(None, etag)
    try:
        files = extract_folder(response.content, path)
    except tarfile.TarError as exc:
        raise FetchError(f"could not read the archive for {repo}: {exc}") from exc
    if not files:
        raise FetchError(f"folder '{path}' is empty or missing in {repo}")
    return Fetched(files, response.headers.get("etag"))
