"""
GitHub issue poller and parallel run dispatcher.

Usage:
  factory poll --repo owner/repo --template tasks/todo.yaml

For each open issue labeled "factory":
  - Converts the issue into a TaskDefinition using the template YAML
  - Runs coder + eval + evaluator in parallel (one thread per issue)
  - Comments on the issue with the result
  - Opens a PR if the run passed (requires ares to have GitHub SSH access)
"""
from __future__ import annotations

import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import typer
from rich.console import Console
from rich.text import Text

from factory.config import load_task, load_workers
from factory.github import GitHubClient
from factory.models import CoderConfig, EvalConfig, Run, RunState, TaskDefinition
from factory.runner import launch_task, spawn_watcher
from factory.slack import SlackClient, get_token as get_slack_token
from factory.ssh import SSHClient
from factory import store

_console = Console(highlight=False)


def _plog(issue_number: int, msg: str, style: str = "") -> None:
    prefix = Text(f"[#{issue_number}] ", style="bold magenta")
    _console.print(prefix + Text(msg, style=style))


def issue_to_task(issue: Dict, template: TaskDefinition) -> TaskDefinition:
    """
    Build a TaskDefinition from a GitHub issue + a template task.

    The template provides worker, repo, eval, and evaluator config.
    The issue title + body become the coder prompt.
    """
    number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""

    # Use the template's coder prompt as a prefix if set, otherwise generic
    template_prompt = (template.coder.prompt or "") if template.coder else ""
    preamble = f"{template_prompt}\n\n" if template_prompt.strip() else ""

    prompt = (
        f"{preamble}"
        f"GitHub Issue #{number}: {title}\n\n"
        f"{body}\n\n"
        f"Fix this issue in the codebase. Make sure all existing tests still pass."
    )

    coder = CoderConfig(
        prompt=prompt,
        max_iterations=template.coder.max_iterations if template.coder else 3,
        session_timeout=template.coder.session_timeout if template.coder else 600,
        agents=template.coder.agents if template.coder else ["claude"],
        rate_limit_markers=template.coder.rate_limit_markers if template.coder else [],
    )

    return TaskDefinition(
        id=f"issue-{number}",
        name=f"Issue #{number}: {title}",
        worker=template.worker,
        repo=template.repo,
        coder=coder,
        evaluator=template.evaluator,
        untangle=template.untangle,
        crucible=template.crucible,
        slack=template.slack,
        service=template.service,
        eval=template.eval,
    )


def _push_and_pr(
    gh: GitHubClient,
    repo: str,
    run: Run,
    task: TaskDefinition,
    issue: Dict,
    workers_path: Path,
) -> None:
    """Push the worktree branch to GitHub and open a PR."""
    branch = _branch_name(task.id, run.run_id)
    number = issue["number"]

    config = load_workers(workers_path)
    worker = config.workers[task.worker]
    client = SSHClient(
        host=worker.host,
        user=worker.user,
        port=worker.port,
        identity_file=worker.identity_file,
        shell_init=worker.shell_init,
    )

    worktree = run.worktree_path

    commit_msg = f"factory: fix for issue #{number}" if number else f"factory: {task.name[:60]}"
    # Commit any uncommitted changes Claude left behind
    client.run(
        f"git -C {worktree} add -A && "
        f"git -C {worktree} diff --cached --quiet || "
        f"git -C {worktree} commit -m {shlex.quote(commit_msg)}",
        timeout=30,
    )

    push_url = f"https://x-access-token:{gh.token}@github.com/{repo}.git"
    result = client.run(
        f"git -c credential.helper= -C {worktree} push {shlex.quote(push_url)} HEAD:{branch}",
        timeout=60,
    )
    if not result.ok:
        raise RuntimeError(f"git push failed: {result.stderr.strip()}")

    base_branch = gh.get_default_branch(repo)
    verdict_line = f"\n\n**Evaluator:** {run.evaluator_reason}" if run.evaluator_verdict else ""
    service_line = f"\n\n**Preview:** http://{worker.host}:{run.service_port}" if run.service_port else ""
    pr_body = (
        f"{'Fixes #' + str(number) + chr(10) + chr(10) if number else ''}"
        f"Automated implementation by Silverpond Factory (run `{run.run_id}`).{verdict_line}{service_line}"
    )
    try:
        pr = gh.create_pr(
            repo=repo,
            title=issue["title"],
            body=pr_body,
            head=branch,
            base=base_branch,
        )
        if number:
            _plog(number, f"PR opened: {pr['html_url']}", style="green")
        return pr["html_url"]
    except RuntimeError as exc:
        typer.echo(f"WARNING: could not open PR: {exc}")
        return None
    return None


def _issue_overrides(issue: Dict) -> Tuple[Optional[str], Optional[str]]:
    """Extract factory:model:<name> and factory:effort:<level> from issue labels."""
    model = None
    effort = None
    for label in issue.get("labels", []):
        name = label["name"]
        if name.startswith("factory:model:"):
            model = name[len("factory:model:"):]
        elif name.startswith("factory:effort:"):
            effort = name[len("factory:effort:"):]
    return model, effort


def _run_one_issue(
    issue: Dict,
    template: TaskDefinition,
    gh: GitHubClient,
    repo: str,
    workers_path: Path,
) -> Tuple[Dict, Run]:
    """Launch the factory pipeline for a single issue (non-blocking)."""
    number = issue["number"]
    task = issue_to_task(issue, template)

    model, effort = _issue_overrides(issue)
    if model and task.coder:
        task.coder.model = model
        _plog(number, f"  model override: {model}")
    if effort and task.coder:
        task.coder.effort = effort
        _plog(number, f"  effort override: {effort}")

    _plog(number, issue["title"])
    gh.add_label(repo, number, "factory:running")

    try:
        run = launch_task(task, workers_path)
    except Exception as exc:
        _plog(number, f"ERROR: {exc}", style="red")
        gh.comment_on_issue(repo, number, f"❌ Factory launch crashed: {exc}")
        gh.remove_label(repo, number, "factory:running")
        raise

    if run.state == RunState.failed:
        _plog(number, f"launch failed: {run.notes}", style="red")
        gh.comment_on_issue(repo, number, f"❌ Factory launch failed: {run.notes or 'unknown error'}")
        gh.remove_label(repo, number, "factory:running")
        return issue, run

    spawn_watcher(run, workers_path, repo=repo, issue_number=number)
    _plog(number, f"launched  run={run.run_id}", style="green")
    return issue, run


def _slack_post_result(run: Run, passed: bool, pr_url: Optional[str] = None) -> None:
    token = get_slack_token()
    if not token or not run.slack_channel_id:
        return
    try:
        sl = SlackClient(token)
        if passed:
            msg = ":white_check_mark: Run passed"
            if run.evaluator_reason:
                msg += f"\n> {run.evaluator_reason}"
            if pr_url:
                msg += f"\n:arrow_right: PR opened: {pr_url}"
        else:
            msg = f":x: Run failed\n> {run.notes or 'see run logs'}"
        sl.post(run.slack_channel_id, msg, thread_ts=run.slack_thread_ts or None)
    except Exception:
        pass


def poll(
    repo: str,
    template: TaskDefinition,
    gh: GitHubClient,
    workers_path: Path = Path("workers.yaml"),
    max_concurrency: int = 4,
) -> List[Tuple[Dict, Run]]:
    """
    Fetch all 'factory'-labeled issues and run them in parallel.
    Concurrency is capped at max_concurrency (default: worker slot count).
    Returns a list of (issue, run) pairs.
    """
    issues = gh.get_issues(repo, "factory")

    # Skip issues already marked as running
    pending = [i for i in issues if "factory:running" not in
               [l["name"] for l in i.get("labels", [])]]

    if not pending:
        _console.print("  [dim]No pending factory issues found.[/dim]")
        return []

    cap = min(len(pending), max_concurrency)
    parallel_note = f"up to {cap} at a time" if len(pending) > cap else "all in parallel"
    _console.print(f"  Found [bold]{len(pending)}[/bold] issue(s) — {parallel_note}")
    for i in pending:
        _console.print(f"    [dim]#{i['number']}[/dim]  {i['title']}")
    _console.print()

    results = []
    with ThreadPoolExecutor(max_workers=cap) as executor:
        futures = {
            executor.submit(
                _run_one_issue, issue, template, gh, repo, workers_path
            ): issue
            for issue in pending
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                issue = futures[future]
                _plog(issue["number"], f"unhandled error: {exc}", style="red")

    return results


def _branch_name(task_id: str, run_id: str) -> str:
    return f"factory/{task_id}-{run_id}"
