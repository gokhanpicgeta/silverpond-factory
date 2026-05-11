"""
GitHub API client for the factory poller.

Uses urllib (no extra dependencies) to:
- Fetch issues labeled "factory"
- Comment on issues with run results
- Add/remove labels to track state
- Open PRs when runs pass
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def get_token() -> str:
    token = os.environ.get("FACTORY_GITHUB_TOKEN")
    if not token:
        raise RuntimeError("FACTORY_GITHUB_TOKEN environment variable not set")
    return token


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Any:
        url = f"{self.BASE}{path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"

        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "silverpond-factory",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub API error {e.code}: {e.read().decode()}") from e

    # ── Repo ──────────────────────────────────────────────────────────────────

    def get_default_branch(self, repo: str) -> str:
        """Return the default branch name for a repo (e.g. 'main' or 'master')."""
        data = self._request("GET", f"/repos/{repo}")
        return data.get("default_branch", "master")

    # ── Issues ────────────────────────────────────────────────────────────────

    def get_issues(self, repo: str, label: str) -> List[Dict]:
        """Return open issues with the given label."""
        return self._request("GET", f"/repos/{repo}/issues", params={
            "labels": label,
            "state": "open",
            "per_page": "100",
        })

    def comment_on_issue(self, repo: str, issue_number: int, body: str) -> Dict:
        return self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            data={"body": body},
        )

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/labels",
            data={"labels": [label]},
        )

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        try:
            self._request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{label}")
        except RuntimeError:
            pass  # label may not exist, ignore

    def close_issue(self, repo: str, issue_number: int) -> None:
        self._request("PATCH", f"/repos/{repo}/issues/{issue_number}", data={"state": "closed"})

    # ── Pull Requests ─────────────────────────────────────────────────────────

    def comment_on_pr(self, repo: str, pr_number: int, body: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{pr_number}/comments", data={"body": body})

    def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> Dict:
        return self._request("POST", f"/repos/{repo}/pulls", data={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        })
