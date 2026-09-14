"""Thin client around the official YouTube Data API v3."""

from __future__ import annotations

import re

import httpx

from app.config import get_settings

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class YouTubeAPIError(RuntimeError):
    """Raised when the YouTube Data API cannot be reached or rejects the request."""


def parse_iso8601_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = {key: int(val) if val else 0 for key, val in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


class YouTubeClient:
    """Synchronous wrapper for the search/videos/channels endpoints we need."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.youtube_api_key
        self.region = settings.youtube_region
        self.max_results = settings.youtube_max_results
        self.timeout = 15.0

    def _get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            raise YouTubeAPIError(
                "YOUTUBE_API_KEY не задан. Получите бесплатный ключ в Google Cloud Console "
                "(YouTube Data API v3) и добавьте его в .env."
            )
        request_params = {**params, "key": self.api_key}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{YOUTUBE_API_BASE}/{path}", params=request_params)
        except httpx.HTTPError as exc:
            raise YouTubeAPIError(f"Не удалось обратиться к YouTube API: {exc}") from exc
        if response.status_code != 200:
            raise YouTubeAPIError(
                f"YouTube API вернул {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    def search_video_ids(self, query_text: str, language: str) -> list[str]:
        data = self._get(
            "search",
            {
                "part": "snippet",
                "q": query_text,
                "type": "video",
                "maxResults": self.max_results,
                "relevanceLanguage": language,
                "regionCode": self.region,
                "safeSearch": "none",
                "order": "date",
            },
        )
        return [
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    def get_videos(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        items: list[dict] = []
        for offset in range(0, len(video_ids), 50):
            chunk = video_ids[offset : offset + 50]
            data = self._get(
                "videos",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            )
            items.extend(data.get("items", []))
        return items

    def get_channels(self, channel_ids: list[str]) -> list[dict]:
        unique_ids = list(dict.fromkeys(channel_ids))
        if not unique_ids:
            return []
        items: list[dict] = []
        for offset in range(0, len(unique_ids), 50):
            chunk = unique_ids[offset : offset + 50]
            data = self._get(
                "channels",
                {"part": "snippet,statistics", "id": ",".join(chunk)},
            )
            items.extend(data.get("items", []))
        return items

    def resolve_channel(self, external_id: str | None, handle: str | None, username: str | None) -> dict | None:
        """Resolves a channel to its full record (with the uploads playlist id)
        by id, @handle or legacy username."""
        params = {"part": "snippet,contentDetails,statistics"}
        if external_id:
            params["id"] = external_id
        elif handle:
            params["forHandle"] = handle
        elif username:
            params["forUsername"] = username
        else:
            return None
        items = self._get("channels", params).get("items", [])
        return items[0] if items else None

    def search_channel(self, query: str) -> dict | None:
        """Fallback for custom /c/ URLs: search for the channel by name."""
        data = self._get(
            "search",
            {"part": "snippet", "q": query, "type": "channel", "maxResults": 1},
        )
        items = data.get("items", [])
        if not items:
            return None
        channel_id = items[0].get("id", {}).get("channelId")
        return self.resolve_channel(channel_id, None, None) if channel_id else None

    def get_recent_video_ids(self, uploads_playlist_id: str, max_results: int = 15) -> list[str]:
        data = self._get(
            "playlistItems",
            {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": max_results},
        )
        return [
            item["contentDetails"]["videoId"]
            for item in data.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]
