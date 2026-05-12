"""
Evaluator agent: reviews code changes and test results using claude -p.

Unlike the coder (which runs interactively in tmux), the evaluator is a
single-shot review — all context is provided upfront in the prompt and
it returns a structured verdict.
"""
from __future__ import annotations

import base64
from typing import List, Optional

from factory.models import EvalResult
from factory.ssh import SSHClient, SSHResult

REVIEW_PROMPT_TEMPLATE = """\
You are a senior engineer in an automated software factory. Your job is to decide \
whether a pull request should be opened, opened as a draft, or rejected.

## Task Description
{task_description}

## Agent Completion Summary
{completion_summary}

## Git Diff (what the coder wrote)
```
{diff}
```

## Test Results
```
{test_output}
```

## Code Review Findings (Crucible)
```
{crucible_findings}
```

## Review Criteria
- Did the agent complete the task as described?
- Are any of the code review findings serious enough to block merging?
- Is the implementation safe to open as a PR for human review, even if imperfect?
{extra_criteria}

## Response Format
Reply in EXACTLY this format, nothing else:

VERDICT: APPROVED
REASON: <one sentence explaining why it's ready to merge>

or:

VERDICT: OPEN_DRAFT
REASON: <one sentence summarising what still needs attention>

or:

VERDICT: REJECTED
REASON: <one sentence explaining the blocking issue>

Guidelines:
- APPROVED: task complete, no serious issues, safe to merge
- OPEN_DRAFT: task mostly complete or has minor issues — worth a human look, not safe to auto-merge
- REJECTED: task not completed, broken functionality, or security issue present
"""


def get_diff(client: SSHClient, worktree_path: str, base_branch: str = "main") -> str:
    """Get all changes in the worktree relative to base_branch.

    Stages untracked files first so new files written by the agent are visible.
    The git add -A is safe here — this is a dedicated per-run worktree.
    """
    client.run(f"git -C {worktree_path} add -A 2>/dev/null || true", timeout=15)
    # Diff everything staged against the remote base branch
    for ref in (f"origin/{base_branch}", base_branch, "HEAD~1"):
        result = client.run(f"git -C {worktree_path} diff --cached {ref} 2>/dev/null")
        if result.stdout.strip():
            return result.stdout
    # Last resort: show HEAD commit
    result = client.run(f"git -C {worktree_path} show HEAD 2>/dev/null")
    return result.stdout or "(no diff available)"


def run_evaluator(
    client: SSHClient,
    worktree_path: str,
    task_description: str,
    eval_results: List[EvalResult],
    extra_criteria: str = "",
    timeout: int = 120,
    base_branch: str = "main",
    model: Optional[str] = None,
    effort: Optional[str] = None,
    crucible_findings: str = "",
    completion_summary: str = "",
) -> SSHResult:
    """Run claude -p with the review prompt and return the raw result."""
    diff = get_diff(client, worktree_path, base_branch)

    test_output = "\n".join(
        f"$ {r.command}\n{r.stdout}{r.stderr}".strip()
        for r in eval_results
    ) or "(no tests run)"

    prompt = REVIEW_PROMPT_TEMPLATE.format(
        task_description=task_description,
        completion_summary=completion_summary or "(no summary provided)",
        diff=diff,
        test_output=test_output,
        crucible_findings=crucible_findings or "(no findings)",
        extra_criteria=f"- {extra_criteria}" if extra_criteria else "",
    )

    encoded = base64.b64encode(prompt.encode()).decode()
    flags = "-p"
    if model:
        flags += f" --model {model}"
    if effort:
        flags += f" --effort {effort}"
    tmp = f"/tmp/factory-evaluator-{worktree_path.replace('/', '-')[-40:]}.b64"
    client.write_file(tmp, encoded)
    cmd = f'claude {flags} "$(base64 -d {tmp})"'
    result = client.run(cmd, timeout=timeout)
    client.run(f"rm -f {tmp}", timeout=5)
    return result


def parse_verdict(output: str) -> tuple[str, str]:
    """
    Parse the evaluator's response.
    Returns (verdict, reason) where verdict is 'approved', 'open_draft', or 'rejected'.
    Falls back to 'open_draft' if the format is unexpected.
    """
    reason = ""
    for rline in output.splitlines():
        if rline.strip().upper().startswith("REASON:"):
            reason = rline.split(":", 1)[1].strip()
            break

    for line in output.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            verdict_raw = line.split(":", 1)[1].strip().upper()
            if "APPROVED" in verdict_raw:
                verdict = "approved"
            elif "OPEN_DRAFT" in verdict_raw or "DRAFT" in verdict_raw:
                verdict = "open_draft"
            else:
                verdict = "rejected"
            return verdict, reason

    return "open_draft", "(evaluator response unparseable — defaulting to draft PR)"
