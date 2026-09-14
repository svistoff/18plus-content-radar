"""Parsers for YouTube video and channel URLs (and bare ids/handles)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if _VIDEO_ID_RE.match(value):
        return value

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path

    if host == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
            return candidate if _VIDEO_ID_RE.match(candidate) else None
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if path.startswith(prefix):
                candidate = path[len(prefix) :].split("/")[0]
                return candidate if _VIDEO_ID_RE.match(candidate) else None
    return None


def parse_channel_ref(value: str) -> dict | None:
    """Returns one of {external_id|handle|username|search} for a channel URL,
    @handle, or channel id."""
    value = (value or "").strip()
    if not value:
        return None

    if value.startswith("@"):
        return {"handle": value}
    if re.match(r"^UC[A-Za-z0-9_-]{22}$", value):
        return {"external_id": value}

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in ("youtube.com", "m.youtube.com"):
        # bare handle without @, e.g. "somechannel"
        return {"handle": f"@{value}"} if re.match(r"^[A-Za-z0-9_.-]+$", value) else None

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@"):
        return {"handle": first}
    if first == "channel" and len(parts) > 1:
        return {"external_id": parts[1]}
    if first == "user" and len(parts) > 1:
        return {"username": parts[1]}
    if first == "c" and len(parts) > 1:
        return {"search": parts[1]}
    return {"search": first}
