"""
Silverpond Factory CLI.

Commands:
  ping   [worker]           — verify SSH connectivity
  run    <task.yaml>        — execute a task end-to-end
  status [run_id]           — show run(s) status
  eval   <run_id>           — re-run eval commands for an existing run
  attach <run_id>           — SSH into the remote tmux session for a run
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

import questionary
import typer
from rich.console import Console
from rich.table import Table

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from factory import store
from factory.config import load_task, load_workers
from factory.models import RunState, TaskDefinition
from factory.runner import launch_task, run_task, spawn_watcher
from factory.ssh import SSHClient

app = typer.Typer(
    name="factory",
    help="Silverpond Factory — remote worker orchestrator",
    no_args_is_help=True,
)
console = Console()

_WORKERS_OPT = typer.Option(Path("workers.yaml"), "--workers", "-w", help="Path to workers.yaml")

_STATE_STYLE: dict[RunState, str] = {
    RunState.queued:        "white",
    RunState.running:       "yellow",
    RunState.waiting_input: "magenta",
    RunState.evaluating:    "cyan",
    RunState.passed:        "green",
    RunState.failed:        "red",
    RunState.human_review:  "bright_red",
}


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")



def _client(worker_name: str, workers_path: Path) -> SSHClient:
    config = load_workers(workers_path)
    if worker_name not in config.workers:
        typer.echo(f"Worker '{worker_name}' not found in {workers_path}", err=True)
        raise typer.Exit(1)
    w = config.workers[worker_name]
    return SSHClient(host=w.host, user=w.user, port=w.port, identity_file=w.identity_file, shell_init=w.shell_init)


# ── ping ─────────────────────────────────────────────────────────────────────

@app.command()
def ping(
    worker: str = typer.Argument("ares", help="Worker name from workers.yaml"),
    workers: Path = _WORKERS_OPT,
) -> None:
    """Verify SSH connectivity to a worker machine."""
    c = _client(worker, workers)
    typer.echo(f"Pinging {worker} ({c.user}@{c.host}:{c.port})...")
    if c.ping():
        console.print(f"  [green]OK[/green] — {worker} is reachable")
    else:
        console.print(f"  [red]UNREACHABLE[/red] — {worker} did not respond")
        raise typer.Exit(1)


# ── inline task helpers ───────────────────────────────────────────────────────

def _infer_gh_repo_from_cwd() -> Optional[str]:
    """Parse owner/repo from the current directory's git remote origin."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return _parse_gh_repo(result.stdout.strip())
    return None


def _build_inline_task(
    prompt: str,
    repo: Optional[str],
    eval_commands: List[str],
    workers_path: Path,
) -> "TaskDefinition":
    from factory.models import CoderConfig, CrucibleConfig, EvalConfig, GeminiReviewConfig, RepoConfig, SlackConfig, TaskDefinition

    config = load_workers(workers_path)
    worker_name = next(iter(config.workers))

    gh_repo = repo or _infer_gh_repo_from_cwd()
    if not gh_repo:
        typer.echo(
            "Could not determine repo — use --repo owner/repo or run from inside a git repo.",
            err=True,
        )
        raise typer.Exit(1)

    repo_name = gh_repo.split("/")[-1]
    repo_path = f"~/factory/projects/{repo_name}"
    task_id = _slugify(prompt[:50]) or "inline"

    return TaskDefinition(
        id=task_id,
        name=prompt[:80],
        worker=worker_name,
        repo=RepoConfig(path=repo_path, branch="main", url=f"git@github.com:{gh_repo}.git"),
        coder=CoderConfig(prompt=prompt, max_iterations=3, session_timeout=600, agents=["claude"]),
        crucible=CrucibleConfig(block_on="Critical", timeout=300),
        gemini_review=GeminiReviewConfig(),
        slack=SlackConfig(reviewers=[r for r in os.environ.get("SLACK_DEFAULT_REVIEWERS", "").split(",") if r.strip()]),
        eval=EvalConfig(commands=eval_commands, working_dir=repo_path, timeout=120),
    )


# ── run ──────────────────────────────────────────────────────────────────────

@app.command(name="run")
def run_cmd(
    task_or_file: str = typer.Argument(..., help="Task YAML file, or inline task description"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Override agent (e.g. claude, codex)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model (e.g. sonnet, opus)"),
    effort: Optional[str] = typer.Option(None, "--effort", "-e", help="Override effort (low, medium, high, max)"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="GitHub repo (owner/repo) for inline tasks"),
    eval_cmd: Optional[List[str]] = typer.Option(None, "--eval", help="Eval command(s) for inline tasks"),
    crucible_rounds: Optional[int] = typer.Option(None, "--crucible-rounds", help="Number of crucible review rounds"),
    workers: Path = _WORKERS_OPT,
) -> None:
    """Run a task — pass a YAML file or an inline task description."""
    task_path = Path(task_or_file)
    if task_path.suffix in (".yaml", ".yml") or task_path.exists():
        if not task_path.exists():
            typer.echo(f"Task file not found: {task_path}", err=True)
            raise typer.Exit(1)
        task = load_task(task_path)
    else:
        task = _build_inline_task(task_or_file, repo=repo, eval_commands=list(eval_cmd or []), workers_path=workers)

    if agent and task.coder:
        task.coder.agents = [agent]
    if model and task.coder:
        task.coder.model = model
    if effort and task.coder:
        task.coder.effort = effort
    if crucible_rounds is not None and task.crucible:
        task.crucible.rounds = crucible_rounds

    agents_display = ", ".join(task.coder.agents) if task.coder else "—"
    console.print()
    console.print(f"  [bold]{task.name}[/bold]  [dim]({task.id})[/dim]")
    console.print(f"  Worker [cyan]{task.worker}[/cyan]  ·  Agent [cyan]{agents_display}[/cyan]")
    console.print()

    run = launch_task(task, workers)
    if run.state == RunState.failed:
        color = _STATE_STYLE.get(run.state, "white")
        console.print(f"  [{color}]{run.state.value.upper()}[/{color}]  run [bold]{run.run_id}[/bold]")
        if run.notes:
            console.print(f"  [dim]{run.notes}[/dim]")
        console.print()
        raise typer.Exit(1)

    gh_repo = _parse_gh_repo(task.repo.url) if task.repo and task.repo.url else None
    spawn_watcher(run, workers.resolve(), repo=gh_repo)


# ── status ───────────────────────────────────────────────────────────────────

@app.command()
def status(
    run_id: Optional[str] = typer.Argument(None, help="Run ID, or omit to list all runs"),
) -> None:
    """Show run status. Lists all runs if no run_id is given."""
    runs = [store.load_run(run_id)] if run_id else store.list_runs()
    runs = [r for r in runs if r is not None]

    if not runs:
        typer.echo("No runs found.")
        return

    table = Table(title="Factory Runs", show_lines=False)
    table.add_column("Run ID",  style="cyan",  no_wrap=True)
    table.add_column("Task")
    table.add_column("Worker",  style="dim")
    table.add_column("State",   no_wrap=True)
    table.add_column("PR",      style="blue",  no_wrap=True)
    table.add_column("Created", style="dim",   no_wrap=True)

    for r in runs:
        color = _STATE_STYLE.get(r.state, "white")
        table.add_row(
            r.run_id,
            r.task_name,
            r.worker,
            f"[{color}]{r.state}[/{color}]",
            r.pr_url or "",
            r.created_at[:19],
        )

    console.print(table)

    # If a single run was requested, show eval detail too
    if run_id and runs:
        r = runs[0]
        if r.pr_url:
            console.print(f"\n  PR: [blue]{r.pr_url}[/blue]")
        if r.eval_results:
            typer.echo("\nEval results:")
            for er in r.eval_results:
                mark = "✓" if er.exit_code == 0 else "✗"
                typer.echo(f"  {mark} [{er.exit_code}] {er.command!r}  ({er.duration:.1f}s)")


# ── eval ─────────────────────────────────────────────────────────────────────

@app.command(name="eval")
def eval_cmd(
    run_id: str = typer.Argument(..., help="Run ID to re-evaluate"),
    task_file: Path = typer.Option(..., "--task", "-t", help="Task YAML (needed for eval config)"),
    workers: Path = _WORKERS_OPT,
) -> None:
    """Re-run evaluation commands for an existing run."""
    from factory.evaluator import eval_passed, run_eval

    r = store.load_run(run_id)
    if r is None:
        typer.echo(f"Run {run_id!r} not found", err=True)
        raise typer.Exit(1)

    task = load_task(task_file)
    c = _client(r.worker, workers)

    typer.echo(f"Re-evaluating run {run_id}...")
    store.update_state(run_id, RunState.evaluating)

    results = run_eval(c, task.eval)
    r = store.load_run(run_id)
    assert r is not None
    r.eval_results = results

    for res in results:
        safe = res.command.replace(" ", "_").replace("/", "_")
        store.save_log(run_id, f"eval_{safe}.stdout", res.stdout)
        store.save_log(run_id, f"eval_{safe}.stderr", res.stderr)
        mark = "PASS" if res.exit_code == 0 else "FAIL"
        typer.echo(f"  [{mark}] {res.command!r}  ({res.duration:.1f}s)")

    r.state = RunState.passed if eval_passed(results) else RunState.failed
    store.save_run(r)

    color = _STATE_STYLE.get(r.state, "white")
    console.print(f"Eval complete: [{color}]{r.state}[/{color}]")


# ── attach ───────────────────────────────────────────────────────────────────

@app.command()
def attach(
    run_id: str = typer.Argument(..., help="Run ID whose tmux session to attach"),
    workers: Path = _WORKERS_OPT,
) -> None:
    """
    SSH into the remote machine and attach to the tmux session for a run.

    Creates a new session if one doesn't already exist.
    """
    r = store.load_run(run_id)
    if r is None:
        typer.echo(f"Run {run_id!r} not found", err=True)
        raise typer.Exit(1)

    config = load_workers(workers)
    if r.worker not in config.workers:
        typer.echo(f"Worker '{r.worker}' not in {workers}", err=True)
        raise typer.Exit(1)

    w = config.workers[r.worker]
    session = f"factory-{run_id}"

    ssh_cmd = ["ssh", "-t", "-p", str(w.port)]
    if w.identity_file:
        import os
        ssh_cmd += ["-i", os.path.expanduser(w.identity_file)]
    ssh_cmd += [
        f"{w.user}@{w.host}",
        f"tmux attach-session -t {session} 2>/dev/null || tmux new-session -s {session}",
    ]
    typer.echo(f"Attaching to {w.host} → tmux session '{session}'")
    typer.echo("(Ctrl-b d to detach)")
    subprocess.run(ssh_cmd)


# ── connect ───────────────────────────────────────────────────────────────────

@app.command()
def connect(
    worker: str = typer.Argument("ares", help="Worker name from workers.yaml"),
    workers_path: Path = _WORKERS_OPT,
) -> None:
    """Attach to the active Claude tmux session on a worker (auto-picks if only one)."""
    import os
    config = load_workers(workers_path)
    if worker not in config.workers:
        typer.echo(f"Worker '{worker}' not in {workers_path}", err=True)
        raise typer.Exit(1)

    w = config.workers[worker]
    client = SSHClient(host=w.host, user=w.user, port=w.port,
                       identity_file=w.identity_file, shell_init=w.shell_init)

    sessions_result = client.run(
        "tmux ls -F '#{session_name}' 2>/dev/null || true", timeout=10
    )
    sessions = [s for s in sessions_result.stdout.splitlines() if s.startswith("factory-")]

    if not sessions:
        typer.echo("No active factory sessions on this worker.", err=True)
        raise typer.Exit(1)

    if len(sessions) == 1:
        session = sessions[0]
    else:
        typer.echo("Active sessions:")
        for i, s in enumerate(sessions):
            run_id = s.removeprefix("factory-")
            run = store.load_run(run_id)
            label = run.task_name if run else "unknown"
            typer.echo(f"  [{i}] {s}  ({label})")
        idx = typer.prompt("Select session", default="0")
        session = sessions[int(idx)]

    ssh_cmd = ["ssh", "-t", "-p", str(w.port)]
    if w.identity_file:
        ssh_cmd += ["-i", os.path.expanduser(w.identity_file)]
    ssh_cmd += [f"{w.user}@{w.host}", f"tmux attach-session -t {session}"]

    typer.echo(f"Attaching to {session} on {w.host}  (Ctrl-b d to detach)")
    subprocess.run(ssh_cmd)


# ── kill ─────────────────────────────────────────────────────────────────────

@app.command()
def kill(
    run_id: str = typer.Argument(..., help="Run ID to stop"),
    workers_path: Path = _WORKERS_OPT,
) -> None:
    """Kill a running session and mark the run as failed."""
    from factory.models import RunState
    from factory.session import session_name_for_run

    r = store.load_run(run_id)
    if r is None:
        typer.echo(f"Run {run_id!r} not found", err=True)
        raise typer.Exit(1)

    if r.state != RunState.running:
        typer.echo(f"Run {run_id[:8]} is not running (state={r.state.value})", err=True)
        raise typer.Exit(1)

    config = load_workers(workers_path)
    w = config.workers.get(r.worker)
    if w is None:
        typer.echo(f"Worker '{r.worker}' not in {workers_path}", err=True)
        raise typer.Exit(1)

    client = SSHClient(host=w.host, user=w.user, port=w.port,
                       identity_file=w.identity_file, shell_init=w.shell_init)
    session = session_name_for_run(run_id)
    client.run(f"tmux kill-session -t {session} 2>/dev/null || true", timeout=10)

    if r.service_port:
        from factory.slots import teardown_port
        teardown_port(client, r.service_port)

    r.state = RunState.failed
    r.notes = "killed by user"
    store.save_run(r)
    console.print(f"[red]killed[/red] {run_id[:8]} ({r.task_name})")


# ── cleanup ───────────────────────────────────────────────────────────────────

@app.command()
def cleanup(
    worker: str = typer.Argument("ares", help="Worker name from workers.yaml"),
    workers_path: Path = _WORKERS_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be removed without removing"),
) -> None:
    """Remove all factory worktrees on the worker that have no active tmux session."""
    config = load_workers(workers_path)
    w = config.workers.get(worker)
    if w is None:
        typer.echo(f"Worker '{worker}' not in {workers_path}", err=True)
        raise typer.Exit(1)

    client = SSHClient(host=w.host, user=w.user, port=w.port,
                       identity_file=w.identity_file, shell_init=w.shell_init)

    # List all worktree directories on the worker
    worktree_base = w.default_worktree_base
    listing = client.run(f"ls {worktree_base} 2>/dev/null", timeout=10)
    dirs = [d.strip() for d in listing.stdout.splitlines() if d.strip()]

    if not dirs:
        console.print("  [dim]No worktrees found.[/dim]")
        return

    # Find active tmux sessions so we don't remove live worktrees
    sessions_out = client.run("tmux ls -F '#{session_name}' 2>/dev/null || true", timeout=10)
    active_sessions = set(sessions_out.stdout.splitlines())

    removed = 0
    for d in dirs:
        # Extract run_id from the last segment (e.g. task-name-abc12345 → abc12345)
        run_id = d.rsplit("-", 1)[-1] if "-" in d else d
        session = f"factory-{run_id}"
        if session in active_sessions:
            console.print(f"  [dim]skipping[/dim] {d}  [yellow](active)[/yellow]")
            continue

        worktree = f"{worktree_base}/{d}"
        if dry_run:
            console.print(f"  [dim]would remove[/dim] {worktree}")
            removed += 1
            continue

        # Find the base repo for this worktree so we can prune via git
        base_path = client.run(
            f"git -C {worktree} rev-parse --path-format=absolute --git-common-dir 2>/dev/null | sed 's|/\\.git.*||'",
            timeout=10,
        ).stdout.strip()

        if base_path:
            client.run(f"git -C {base_path} worktree remove --force {worktree} 2>/dev/null || rm -rf {worktree}", timeout=30)
            client.run(f"git -C {base_path} branch -D factory/{d} 2>/dev/null || true", timeout=10)
        else:
            client.run(f"rm -rf {worktree}", timeout=30)

        console.print(f"  [green]removed[/green] {worktree}")
        removed += 1

    noun = "worktree(s) would be removed" if dry_run else "worktree(s) removed"
    console.print(f"\n  {removed} {noun}.")


# ── runs-clean ────────────────────────────────────────────────────────────────

@app.command(name="runs-clean")
def runs_clean(
    days: int = typer.Option(7, "--days", "-d", help="Remove finished runs older than this many days"),
    all_finished: bool = typer.Option(False, "--all", help="Remove all finished runs regardless of age"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be removed without removing"),
) -> None:
    """Remove local run directories for finished runs older than N days (default 7). Use --all for everything."""
    import shutil
    from datetime import datetime as dt, timedelta, timezone

    runs = store.list_runs()

    if all_finished:
        to_remove = [r for r in runs if r.state in (RunState.passed, RunState.failed)]
    else:
        cutoff = dt.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        to_remove = [
            r for r in runs
            if r.state in (RunState.passed, RunState.failed)
            and dt.fromisoformat(r.created_at) < cutoff
        ]

    if not to_remove:
        console.print(f"  [dim]No finished runs older than {days} day(s).[/dim]")
        return

    removed = 0
    for r in to_remove:
        run_dir = store.RUNS_DIR / r.run_id
        age_days = (dt.now(timezone.utc).replace(tzinfo=None) - dt.fromisoformat(r.created_at)).days
        if dry_run:
            console.print(f"  [dim]would remove[/dim] {r.run_id}  [{r.state}]  {r.task_name[:50]}  [dim]({age_days}d old)[/dim]")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
            console.print(f"  [green]removed[/green] {r.run_id}  [dim]{r.task_name[:50]}  ({age_days}d old)[/dim]")
        removed += 1

    noun = "run(s) would be removed" if dry_run else "run(s) removed"
    console.print(f"\n  {removed} {noun}.")


# ── logs ──────────────────────────────────────────────────────────────────────

@app.command()
def logs(
    run_id: str = typer.Argument(..., help="Run ID to show logs for"),
    workers_path: Path = _WORKERS_OPT,
    lines: int = typer.Option(100, "--lines", "-n", help="Number of lines of scrollback"),
) -> None:
    """Dump the tmux scrollback for a running session, or output.log for finished runs."""
    from factory.models import RunState
    from factory.session import session_name_for_run

    r = store.load_run(run_id)
    if r is None:
        typer.echo(f"Run {run_id!r} not found", err=True)
        raise typer.Exit(1)

    config = load_workers(workers_path)
    w = config.workers.get(r.worker)
    if w is None:
        typer.echo(f"Worker '{r.worker}' not in {workers_path}", err=True)
        raise typer.Exit(1)

    client = SSHClient(host=w.host, user=w.user, port=w.port,
                       identity_file=w.identity_file, shell_init=w.shell_init)

    # For running sessions: dump tmux scrollback
    if r.state == RunState.running:
        session = session_name_for_run(run_id)
        result = client.run(
            f"tmux capture-pane -t {session} -p -S -{lines} 2>/dev/null", timeout=15
        )
        if result.stdout.strip():
            typer.echo(result.stdout)
        else:
            typer.echo(f"No tmux session found for {run_id[:8]}", err=True)
        return

    # For finished runs: try output.log
    if not r.worktree_path:
        typer.echo(f"Run {run_id[:8]} has no worktree path", err=True)
        raise typer.Exit(1)
    log_path = f"{r.worktree_path}/.factory/output.log"
    result = client.run(f"tail -n {lines} {log_path} 2>/dev/null", timeout=15)
    if result.stdout.strip():
        typer.echo(result.stdout)
    else:
        typer.echo(f"No log found for {run_id[:8]} (session already cleaned up)", err=True)


# ── init ─────────────────────────────────────────────────────────────────────

@app.command(name="setup")
def setup_cmd(
    workers_path: Path = _WORKERS_OPT,
) -> None:
    """One-time engineer setup: SSH access, GitHub token, and optional label creation."""
    try:
        config = load_workers(workers_path)
    except FileNotFoundError:
        typer.echo(f"workers.yaml not found at {workers_path}", err=True)
        raise typer.Exit(1)

    worker_names = list(config.workers.keys())
    if not worker_names:
        typer.echo("No workers defined in workers.yaml", err=True)
        raise typer.Exit(1)

    console.print("\n[bold]factory setup[/bold] — engineer onboarding\n")

    def ask(q):
        result = q.ask()
        if result is None:
            raise typer.Exit(0)
        return result

    # Worker
    worker_name = ask(questionary.select("Worker:", choices=worker_names, default=worker_names[0]))
    worker_cfg = config.workers[worker_name]

    # Username
    current_user = os.environ.get("FACTORY_WORKER_USER", "")
    username = ask(questionary.text(
        f"Your username on {worker_name} ({worker_cfg.host}):",
        default=current_user,
        validate=lambda v: True if v.strip() else "Cannot be empty",
    )).strip()
    _write_env_var("FACTORY_WORKER_USER", username)
    os.environ["FACTORY_WORKER_USER"] = username
    console.print(f"  [green]✓[/green] Username saved to [dim].env[/dim]")

    # SSH key
    default_key = os.environ.get("FACTORY_SSH_IDENTITY") or str(Path.home() / ".ssh" / "id_ed25519")
    ssh_key = ask(questionary.text(
        "Path to your SSH private key for the worker:",
        default=default_key,
    )).strip()
    if ssh_key:
        _write_env_var("FACTORY_SSH_IDENTITY", ssh_key)
        os.environ["FACTORY_SSH_IDENTITY"] = ssh_key
        console.print(f"  [green]✓[/green] SSH key saved to [dim].env[/dim]")

    # GitHub token → worker
    github_token = os.environ.get("FACTORY_GITHUB_TOKEN")
    if github_token:
        if ask(questionary.confirm(
            f"Write GITHUB_TOKEN to {worker_name} (~/.factory-secrets) for git clone/push?",
            default=True,
        )):
            try:
                _write_worker_secrets(_client(worker_name, workers_path), github_token)
                console.print(f"  [green]✓[/green] GITHUB_TOKEN written to {worker_name}:~/.factory-secrets")
            except Exception as exc:
                console.print(f"  [yellow]⚠[/yellow] Could not write secrets: {exc}")
    else:
        console.print("  [yellow]⚠[/yellow] FACTORY_GITHUB_TOKEN not set in .env — add it and re-run setup")

    # Slack
    if not os.environ.get("SLACK_BOT_TOKEN"):
        console.print("\n  [bold]Slack[/bold] — paste your bot token from api.slack.com/apps → OAuth & Permissions")
        slack_token = ask(questionary.text(
            "SLACK_BOT_TOKEN (leave blank to skip Slack):",
            default="",
        )).strip()
        if slack_token:
            _write_env_var("SLACK_BOT_TOKEN", slack_token)
            os.environ["SLACK_BOT_TOKEN"] = slack_token
            console.print(f"  [green]✓[/green] SLACK_BOT_TOKEN saved to [dim].env[/dim]")

    if os.environ.get("SLACK_BOT_TOKEN"):
        console.print(
            "\n  [dim]To find your Slack user ID: open Slack → click your profile picture"
            " → Profile → ⋯ menu → Copy member ID[/dim]"
        )
        current_reviewers = os.environ.get("SLACK_DEFAULT_REVIEWERS", "")
        reviewers_str = ask(questionary.text(
            "Slack user IDs to notify on your runs (comma-separated, leave blank to skip):",
            default=current_reviewers,
        )).strip()
        if reviewers_str != current_reviewers:
            _write_env_var("SLACK_DEFAULT_REVIEWERS", reviewers_str)
            os.environ["SLACK_DEFAULT_REVIEWERS"] = reviewers_str
            console.print(f"  [green]✓[/green] Slack reviewers saved to [dim].env[/dim]")

    # Optional: set up labels on a repo
    gh_repo_str = ask(questionary.text(
        "GitHub repo to set up factory labels on (owner/repo, leave blank to skip):",
        default="",
    )).strip()
    if gh_repo_str:
        _setup_gh_labels(gh_repo_str)

    console.print("\n[green]✓ Setup complete.[/green]")
    console.print("  Run a task:    [bold]factory run \"your task\" --repo owner/repo[/bold]")
    console.print("  Poll issues:   [bold]factory poll owner/repo[/bold]\n")


def _write_worker_secrets(client: SSHClient, github_token: str) -> None:
    """Write GITHUB_TOKEN to ~/.factory-secrets on the worker and configure git credential helper."""
    write_cmd = (
        "touch ~/.factory-secrets && chmod 600 ~/.factory-secrets && "
        "grep -v '^export GITHUB_TOKEN=' ~/.factory-secrets > /tmp/.fs.tmp 2>/dev/null || true && "
        f"echo 'export GITHUB_TOKEN={github_token}' >> /tmp/.fs.tmp && "
        "mv /tmp/.fs.tmp ~/.factory-secrets"
    )
    client.run(write_cmd, timeout=10)
    client.run(
        "git config --global credential.helper "
        "'!f() { echo username=x-access-token; echo \"password=$GITHUB_TOKEN\"; }; f'",
        timeout=10,
    )


def _write_env_var(key: str, value: str) -> None:
    """Upsert a KEY=value line in the local .env file."""
    env_path = Path(__file__).parent.parent / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")


def _parse_gh_repo(repo_url: str) -> Optional[str]:
    """Extract owner/repo from a GitHub URL or SSH remote."""
    if not repo_url:
        return None
    # git@github.com:owner/repo.git  or  https://github.com/owner/repo
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    return m.group(1) if m else None


_FACTORY_LABELS = [
    ("factory",              "0075ca", "Trigger a factory run"),
    ("factory:running",      "e11d48", "Factory run in progress"),
    ("factory:model:sonnet", "7c3aed", "Use Claude Sonnet"),
    ("factory:model:opus",   "4f46e5", "Use Claude Opus"),
    ("factory:model:haiku",  "0891b2", "Use Claude Haiku"),
    ("factory:effort:low",   "e4e669", "Low effort level"),
    ("factory:effort:medium","f97316", "Medium effort level"),
    ("factory:effort:high",  "ef4444", "High effort level"),
    ("factory:effort:max",   "dc2626", "Max effort level"),
]


def _setup_gh_labels(gh_repo: str) -> None:
    """Create all standard factory labels on a GitHub repo, skipping existing ones."""
    import subprocess as _sp
    created = 0
    skipped = 0
    for name, color, description in _FACTORY_LABELS:
        result = _sp.run(
            ["gh", "label", "create", name, "--repo", gh_repo,
             "--color", color, "--description", description],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            created += 1
        else:
            skipped += 1  # already exists
    console.print(f"  [green]✓[/green] Labels: {created} created, {skipped} already existed on [cyan]{gh_repo}[/cyan]")


# ── labels ───────────────────────────────────────────────────────────────────

@app.command()
def labels(
    repo: str = typer.Argument(..., help="GitHub repo in owner/repo format"),
) -> None:
    """Create all standard factory labels on a GitHub repo."""
    _setup_gh_labels(repo)


# ── workers ──────────────────────────────────────────────────────────────────

@app.command()
def workers(
    workers_path: Path = _WORKERS_OPT,
) -> None:
    """Show all workers and their active agent sessions with resource usage."""
    from datetime import datetime, timezone

    config = load_workers(workers_path)

    # Build a lookup of run_id -> Run for active/recent runs
    all_runs = {r.run_id: r for r in store.list_runs()}

    for worker_name, w in config.workers.items():
        client = SSHClient(
            host=w.host, user=w.user, port=w.port,
            identity_file=w.identity_file, shell_init=w.shell_init,
        )

        console.rule(f"[bold]{worker_name}[/bold]  {w.user}@{w.host}")

        # Check connectivity
        if not client.ping():
            console.print("  [red]UNREACHABLE[/red]")
            continue

        # Get active factory tmux sessions
        sessions_result = client.run(
            "tmux ls -F '#{session_name}' 2>/dev/null || true", timeout=10
        )
        sessions = [
            s for s in sessions_result.stdout.splitlines()
            if s.startswith("factory-")
        ]

        if not sessions:
            console.print("  [dim]No active sessions[/dim]")
            continue

        # Get CPU/mem for all claude processes in one call
        ps_result = client.run(
            "ps aux --no-header 2>/dev/null | grep '[c]laude' || true", timeout=10
        )
        # Sum CPU and mem across all claude processes
        total_cpu = 0.0
        total_mem_mb = 0
        for line in ps_result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 6:
                try:
                    total_cpu += float(parts[2])
                    total_mem_mb += int(parts[5]) // 1024
                except ValueError:
                    pass

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("RUN ID",  style="cyan", no_wrap=True)
        table.add_column("TASK")
        table.add_column("STATE",   no_wrap=True)
        table.add_column("RUNTIME", style="dim", no_wrap=True)
        table.add_column("CPU",     style="dim", no_wrap=True)
        table.add_column("MEM",     style="dim", no_wrap=True)

        for i, session in enumerate(sessions):
            run_id = session.removeprefix("factory-")
            run = all_runs.get(run_id)

            task_name = run.task_name if run else "unknown"
            if len(task_name) > 35:
                task_name = task_name[:33] + "…"

            state_str = ""
            if run:
                color = _STATE_STYLE.get(run.state, "white")
                state_str = f"[{color}]{run.state.value}[/{color}]"

            runtime = ""
            if run:
                try:
                    created = datetime.fromisoformat(run.created_at.replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    delta = int((datetime.now(timezone.utc) - created).total_seconds())
                    m, s = divmod(delta, 60)
                    h, m = divmod(m, 60)
                    runtime = f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
                except Exception:
                    pass

            # Show resource totals on the first row only
            cpu_str = f"{total_cpu:.1f}%" if i == 0 and total_cpu else ("" if i > 0 else "0%")
            mem_str = f"{total_mem_mb}MB" if i == 0 and total_mem_mb else ("" if i > 0 else "0MB")

            table.add_row(run_id[:8], task_name, state_str, runtime, cpu_str, mem_str)

        console.print(table)

    console.print()


# ── gc ───────────────────────────────────────────────────────────────────────

@app.command()
def gc(
    workers_path: Path = _WORKERS_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be cleaned up without doing it"),
    keep_days: int = typer.Option(7, "--keep-days", help="Keep completed runs for this many days"),
) -> None:
    """Garbage collect: mark stuck runs as failed, clean up old run data."""
    from datetime import datetime, timezone, timedelta
    from factory.models import RunState
    from factory.session import session_name_for_run

    config = load_workers(workers_path)
    all_runs = store.list_runs()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)

    stuck = 0
    cleaned = 0

    for run in all_runs:
        # ── 1. Stuck running runs ──────────────────────────────────────────
        if run.state == RunState.running:
            w = config.workers.get(run.worker)
            if w is None:
                continue
            client = SSHClient(host=w.host, user=w.user, port=w.port,
                               identity_file=w.identity_file, shell_init=w.shell_init)
            session = session_name_for_run(run.run_id)
            alive = client.run(
                f"tmux has-session -t {session} 2>/dev/null && echo yes || echo no",
                timeout=10,
            ).stdout.strip() == "yes"
            if not alive:
                stuck += 1
                if dry_run:
                    console.print(f"  [yellow]would mark failed:[/yellow] {run.run_id[:8]} ({run.task_name}) — session dead")
                else:
                    if run.service_port:
                        from factory.slots import teardown_port
                        teardown_port(client, run.service_port)
                    run.state = RunState.failed
                    run.notes = "gc: tmux session not found"
                    store.save_run(run)
                    console.print(f"  [red]marked failed:[/red] {run.run_id[:8]} ({run.task_name})")

        # ── 2. Old completed runs ──────────────────────────────────────────
        if run.state in (RunState.passed, RunState.failed):
            try:
                created = datetime.fromisoformat(run.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if created < cutoff:
                cleaned += 1
                run_dir = store.RUNS_DIR / run.run_id
                if dry_run:
                    worktree_note = f" + worktree" if run.worktree_path else ""
                    console.print(f"  [dim]would delete:[/dim] {run.run_id[:8]} ({run.task_name}, {created.date()}){worktree_note}")
                else:
                    import shutil
                    shutil.rmtree(run_dir, ignore_errors=True)
                    if run.worktree_path:
                        w = config.workers.get(run.worker)
                        if w:
                            wt_client = SSHClient(host=w.host, user=w.user, port=w.port,
                                                  identity_file=w.identity_file, shell_init=w.shell_init)
                            wt_client.run(f"rm -rf {run.worktree_path}", timeout=30)
                    console.print(f"  [dim]deleted:[/dim] {run.run_id[:8]} ({run.task_name})")

    label = "[dim](dry run)[/dim] " if dry_run else ""
    console.print(f"\n{label}gc complete: {stuck} stuck run(s) marked failed, {cleaned} old run(s) cleaned up")


# ── poll ─────────────────────────────────────────────────────────────────────

@app.command()
def poll(
    repo: str = typer.Argument(..., help="GitHub repo in owner/repo format"),
    template: Optional[Path] = typer.Option(None, "--template", "-t", help="Task YAML template (optional — inferred from repo if omitted)"),
    workers: Path = _WORKERS_OPT,
    max_concurrency: Optional[int] = typer.Option(None, "--max-concurrency", "-c", help="Max parallel runs (default: worker slot count)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model (e.g. sonnet, opus)"),
    effort: Optional[str] = typer.Option(None, "--effort", "-e", help="Override effort (low, medium, high, max)"),
    eval_cmd: Optional[List[str]] = typer.Option(None, "--eval", help="Eval command(s) run after each agent iteration"),
) -> None:
    """
    Fetch open GitHub issues labeled 'factory' and run them in parallel.

    Requires FACTORY_GITHUB_TOKEN environment variable to be set.
    """
    from factory.github import GitHubClient, get_token
    from factory.poller import poll as run_poll

    try:
        token = get_token()
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    if template is not None:
        if not template.exists():
            typer.echo(f"Template not found: {template}", err=True)
            raise typer.Exit(1)
        task_template = load_task(template)
    else:
        task_template = _build_inline_task("", repo=repo, eval_commands=list(eval_cmd or []), workers_path=workers)

    if model and task_template.coder:
        task_template.coder.model = model
    if effort and task_template.coder:
        task_template.coder.effort = effort
    gh = GitHubClient(token)

    # Default concurrency to the worker's slot count
    workers_config = load_workers(workers)
    worker_cfg = workers_config.workers.get(task_template.worker)
    default_cap = worker_cfg.slots if worker_cfg else 4
    cap = max_concurrency if max_concurrency is not None else default_cap

    agents_display = ", ".join(task_template.coder.agents) if task_template.coder else "—"
    console.print()
    console.print(f"  [bold]factory poll[/bold]  [dim]{repo}[/dim]")
    console.print(f"  Template [cyan]{task_template.id}[/cyan]  ·  Worker [cyan]{task_template.worker}[/cyan]  ·  Agent [cyan]{agents_display}[/cyan]  ·  Max concurrency [cyan]{cap}[/cyan]")
    console.print()

    results = run_poll(repo, task_template, gh, workers, max_concurrency=cap)

    if not results:
        return

    console.print()
    table = Table(title=f"Launched — {repo}", show_lines=False)
    table.add_column("Issue",  style="dim",  no_wrap=True)
    table.add_column("Title")
    table.add_column("Run",    style="cyan", no_wrap=True)
    table.add_column("State",  no_wrap=True)

    for issue, run in results:
        color = _STATE_STYLE.get(run.state, "white")
        table.add_row(
            f"#{issue['number']}",
            issue["title"],
            run.run_id,
            f"[{color}]{run.state.value}[/{color}]",
        )
    console.print(table)
    console.print()
    console.print(f"  [dim]Watchers running in background. Use [bold]factory status[/bold] to check progress.[/dim]")
    console.print()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
