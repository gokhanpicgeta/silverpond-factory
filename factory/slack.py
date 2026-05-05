"""
Slack API client for the factory controller.

Handles channel lifecycle: create, post, invite, archive.
Requires SLACK_BOT_TOKEN environment variable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from typing import Optional


SLACK_API = "https://slack.com/api"


def get_token() -> Optional[str]:
    return os.environ.get("SLACK_BOT_TOKEN")


def get_cached_channel_id() -> Optional[str]:
    return os.environ.get("SLACK_FACTORY_CHANNEL_ID")


def _cache_channel_id(channel_id: str) -> None:
    """Persist the factory channel ID to .env so future runs skip channel lookup."""
    os.environ["SLACK_FACTORY_CHANNEL_ID"] = channel_id
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if "SLACK_FACTORY_CHANNEL_ID" not in content:
            env_path.write_text(content.rstrip() + f"\nSLACK_FACTORY_CHANNEL_ID={channel_id}\n")


class SlackClient:
    def __init__(self, token: str):
        self.token = token

    def _call(self, method: str, data: dict) -> dict:
        req = urllib.request.Request(
            f"{SLACK_API}/{method}",
            data=json.dumps(data).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"Slack API {method} failed: {e}")

    def create_channel(self, name: str) -> str:
        """Create a channel and return its ID. Returns existing channel ID if name is taken."""
        safe = name.lower().replace(" ", "-").replace("/", "-")[:80]
        result = self._call("conversations.create", {"name": safe, "is_private": False})
        if result.get("ok"):
            return result["channel"]["id"]
        if result.get("error") == "name_taken":
            return self._get_channel_id(safe)
        raise RuntimeError(f"Could not create channel {safe!r}: {result.get('error')}")

    def _get_channel_id(self, name: str) -> str:
        cursor = None
        while True:
            params: dict = {"limit": 200, "exclude_archived": True, "types": "public_channel,private_channel"}
            if cursor:
                params["cursor"] = cursor
            result = self._call("conversations.list", params)
            for ch in result.get("channels", []):
                if ch["name"] == name:
                    return ch["id"]
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        raise RuntimeError(f"Channel {name!r} not found")

    def find_or_create_channel(self, name: str, cached_id: Optional[str] = None) -> str:
        """Return the channel ID, using cached_id if available to avoid expensive lookups."""
        if cached_id:
            return cached_id
        channel_id = self.create_channel(name)
        _cache_channel_id(channel_id)
        return channel_id

    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: str) -> list:
        """Fetch replies in a thread newer than oldest."""
        r = self._call("conversations.replies", {
            "channel": channel_id,
            "ts": thread_ts,
            "oldest": oldest,
            "limit": 20,
        })
        return r.get("messages", [])

    def post(self, channel_id: str, text: str, thread_ts: Optional[str] = None) -> dict:
        data: dict = {"channel": channel_id, "text": text, "unfurl_links": False}
        if thread_ts:
            data["thread_ts"] = thread_ts
        return self._call("chat.postMessage", data)

    def invite(self, channel_id: str, user_ids: list[str]) -> None:
        if not user_ids:
            return
        self._call("conversations.invite", {"channel": channel_id, "users": ",".join(user_ids)})

    def archive(self, channel_id: str) -> None:
        self._call("conversations.archive", {"channel": channel_id})
