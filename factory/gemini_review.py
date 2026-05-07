"""
Gemini review: produces a plain-language summary of what a PR does.

Runs gemini on the git diff to generate a human-readable description of
the changes. This step is informational only — it does NOT block the pipeline.
"""
from __future__ import annotations

import base64
from typing import Optional

from factory.evaluator_agent import get_diff
from factory.ssh import SSHClient

SUMMARY_PROMPT_TEMPLATE = """\
You are a code reviewer summarizing a pull request for a human reader.

Below is the git diff for this PR. Write a concise summary (3-5 sentences) that:
1. Describes what the PR does in plain language
2. Highlights the most important changes
3. Notes any potential concerns (informational only)

## Task Description
{task_description}

## Git Diff
```
{diff}
```

Write your summary in plain prose. Do not use bullet points or headers. Keep it under 150 words.
"""


def run_gemini_review(
    client: SSHClient,
    worktree_path: str,
    task_description: str,
    timeout: int = 120,
    base_branch: str = "main",
    model: Optional[str] = None,
    gemini_cmd: str = "gemini",
) -> str:
    """
    Run gemini to summarize the PR diff.

    Returns the summary text, or an empty string if gemini is unavailable or fails.
    This is non-blocking — errors are swallowed and logged by the caller.
    """
    diff = get_diff(client, worktree_path, base_branch)

    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        task_description=task_description,
        diff=diff[:8000],  # truncate very large diffs to stay within context
    )

    encoded = base64.b64encode(prompt.encode()).decode()
    flags = "-p"
    if model:
        flags += f" --model {model}"
    cmd = f'{gemini_cmd} {flags} "$(echo \'{encoded}\' | base64 -d)"'
    result = client.run(cmd, timeout=timeout)
    return result.stdout.strip()
