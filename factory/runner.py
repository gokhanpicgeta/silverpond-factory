"""
Run orchestrator.

Pipeline:
  launch_task()  — setup worktree + slot + launch agent session → returns immediately
  watch_task()   — poll for completion + eval + evaluator → called by background watcher
  run_task()     — convenience wrapper: launch + watch (blocking, for tests)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml as _yaml

from factory import evaluator, session as sess, store
from factory.evaluator_agent import parse_verdict, run_evaluator
from factory.gemini_review import run_gemini_review
from factory.config import WorkerConfig, load_workers
from factory.models import Run, RunState, TaskDefinition, UntangleConfig, CrucibleConfig, GeminiReviewConfig
from factory.slots import teardown_port
from factory.slack import SlackClient, get_token as get_slack_token, get_cached_channel_id
from factory.ssh import SSHClient


# ── Public API ────────────────────────────────────────────────────────────────

def launch_task(task: TaskDefinition, workers_path: Path = Path("workers.yaml")) -> Run:
    """
    Set up worktree + slot, launch the first agent session, print commands panel.
    Returns immediately after the session starts — does NOT wait for completion.
    Call spawn_watcher() to run the poll/eval pipeline in a background process.
    """
    config = load_workers(workers_path)
    worker: WorkerConfig | None = config.workers.get(task.worker)
    if worker is None:
        raise ValueError(f"Worker '{task.worker}' not found in {workers_path}")

    run = Run(task_id=task.id, task_name=task.name, worker=task.worker)
    store.save_run(run)

    # Persist task definition so the background watcher can reload it
    _task_yaml_path = store.RUNS_DIR / run.run_id / "task.yaml"
    _task_yaml_path.write_text(_yaml.dump(task.model_dump(), allow_unicode=True, sort_keys=False))
    run.task_file = str(_task_yaml_path.resolve())
    store.save_run(run)
    _log(run.run_id, f"created  task={task.id!r} worker={task.worker}")

    client = SSHClient(host=worker.host, user=worker.user, port=worker.port,
                       identity_file=worker.identity_file, shell_init=worker.shell_init)

    coder_model = (task.coder.model or worker.model) if task.coder else worker.model
    coder_effort = (task.coder.effort or worker.effort) if task.coder else worker.effort
    agent_names = (task.coder.agents or ["claude"]) if task.coder else ["claude"]

    # ── Slack setup ──────────────────────────────────────────────────────────
    slack_client = None
    slack_token = get_slack_token()
    if task.slack is not None and slack_token:
        try:
            slack_client = SlackClient(slack_token)
            channel_id = slack_client.find_or_create_channel(f"factory-{worker.user}", cached_id=get_cached_channel_id())
            run.slack_channel_id = channel_id
            if task.slack.reviewers:
                slack_client.invite(channel_id, task.slack.reviewers)
            prompt_preview = task.coder.prompt[:300] if task.coder else ""
            result = slack_client.post(
                channel_id,
                f":rocket: *{task.name}* — run `{run.run_id}`\n"
                f"Agent: `{agent_names[0]}` · Model: `{coder_model}` · Effort: `{coder_effort}`\n"
                + (f"> {prompt_preview}" if prompt_preview else ""),
            )
            run.slack_thread_ts = result.get("ts", "")
            store.save_run(run)
            _log(run.run_id, f"  slack #factory thread={run.slack_thread_ts}")
        except Exception as exc:
            _log(run.run_id, f"  WARNING: Slack setup failed: {exc}")
            slack_client = None

    run.state = RunState.running
    store.save_run(run)

    # ── Worktree setup ───────────────────────────────────────────────────────
    if task.repo is not None:
        _log(run.run_id, "state=running  (worktree setup)")
        try:
            worktree_path = _create_run_worktree(client, task, run.run_id, worker)
            run.worktree_path = worktree_path
            store.save_run(run)
            _log(run.run_id, f"  worktree={worktree_path}")
        except RuntimeError as exc:
            run.state = RunState.failed
            run.notes = str(exc)
            store.save_run(run)
            _log(run.run_id, f"FAILED during worktree setup: {exc}")
            return run

    working_dir = run.worktree_path or task.eval.working_dir

    # ── Slot acquisition ─────────────────────────────────────────────────────
    if task.service is not None:
        factory_port = _allocate_port(worker)
        run.service_port = factory_port
        store.save_run(run)
        _log(run.run_id, f"  slot=1  port={factory_port}")

    if task.coder is None:
        # Eval-only task — nothing to launch, watcher will run eval immediately
        store.save_run(run)
        return run

    # ── Launch first agent session ───────────────────────────────────────────
    session_name = sess.session_name_for_run(run.run_id)
    prompt = _prepend_constitution(task.coder.prompt)
    agent_commands = [worker.agents.get(name, name) for name in agent_names]
    agent_cmd = agent_commands[0]
    agent_name = agent_names[0]

    _log(run.run_id, f"state=running  (coder iteration 1/{task.coder.max_iterations}, agent={agent_name}, model={coder_model}, effort={coder_effort})")
    _slack_post(slack_client, run, f":hammer: Iteration 1/{task.coder.max_iterations} — agent: `{agent_name}` · model: `{coder_model}` · effort: `{coder_effort}`")

    sess.setup_factory_dir(client, working_dir)
    sess.write_task(client, working_dir, prompt)
    if slack_client and run.slack_channel_id and slack_token:
        sess.write_agent_hooks(client, working_dir, slack_token, run.slack_channel_id, thread_ts=run.slack_thread_ts)
    if run.service_port and task.service:
        sess.append_service_context(client, working_dir, run.service_port, task.service.port)
    sess.write_runner_script(
        client, working_dir,
        shell_init=worker.shell_init,
        agent_cmd=agent_cmd,
        factory_port=run.service_port or None,
        model=coder_model if agent_cmd == "claude" else None,
        effort=coder_effort if agent_cmd == "claude" else None,
    )

    if not sess.start_session(client, session_name, working_dir):
        run.state = RunState.failed
        run.notes = "Failed to start tmux session"
        store.save_run(run)
        _log(run.run_id, "  ERROR: could not start tmux session")
        _slack_post(slack_client, run, ":x: Failed to start tmux session on worker")
        return run

    _print_commands_panel(run.run_id, run.worktree_path, run.service_port, label=task.name)

    if slack_client and run.slack_channel_id and slack_token and run.slack_thread_ts:
        slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if slack_app_token:
            sess.register_run(client, run.run_id, session_name, run.slack_thread_ts)
            ok = sess.ensure_bridge_daemon(client, slack_app_token, slack_token, run.slack_channel_id)
            _log(run.run_id, f"  slack bridge daemon {'started' if ok else 'failed to start'}")

    store.save_run(run)
    return run


def watch_task(
    run_id: str,
    task: TaskDefinition,
    workers_path: Path = Path("workers.yaml"),
    *,
    repo: str | None = None,
    issue_number: int | None = None,
) -> Run:
    """
    Poll the running agent session, run eval, evaluator, handle retries.
    Called by the background watcher process after launch_task() has returned.
    If repo + issue_number are given, also opens a PR on pass and comments on fail.
    """
    run = store.load_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id!r} not found")

    config = load_workers(workers_path)
    worker = config.workers[task.worker]
    client = SSHClient(host=worker.host, user=worker.user, port=worker.port,
                       identity_file=worker.identity_file, shell_init=worker.shell_init)

    working_dir = run.worktree_path or task.eval.working_dir
    session_name = sess.session_name_for_run(run_id)

    # Reconnect Slack if needed
    slack_client = None
    slack_token = get_slack_token()
    if task.slack is not None and slack_token and run.slack_channel_id:
        try:
            slack_client = SlackClient(slack_token)
        except Exception:
            pass

    try:
        # ── Coder → eval loop ────────────────────────────────────────────────
        if task.coder is not None:
            original_prompt = _prepend_constitution(task.coder.prompt)
            coder_model = task.coder.model or worker.model
            coder_effort = task.coder.effort or worker.effort
            agent_names = task.coder.agents or ["claude"]
            agent_commands = [worker.agents.get(name, name) for name in agent_names]
            current_agent_idx = 0
            prompt = original_prompt

            needs_new_session = False  # True only when switching agents (rate limit)
            feedback_message = ""
            crucible_rounds_used = 0  # tracks how many crucible feedback cycles have been used

            for iteration in range(1, task.coder.max_iterations + 1):
                agent_name = agent_names[current_agent_idx]
                agent_cmd = agent_commands[current_agent_idx]

                if iteration > 1:
                    _log(run_id, f"state=running  (coder iteration {iteration}/{task.coder.max_iterations}, agent={agent_name}, model={coder_model}, effort={coder_effort})")
                    _slack_post(slack_client, run, f":hammer: Iteration {iteration}/{task.coder.max_iterations} — agent: `{agent_name}` · model: `{coder_model}` · effort: `{coder_effort}`")
                    if needs_new_session:
                        # Agent switched due to rate limit — kill old session, start fresh
                        sess.kill_session(client, session_name)
                        if run.slack_thread_ts:
                            sess.unregister_run(client, run.slack_thread_ts)
                        sess.setup_factory_dir(client, working_dir)
                        sess.write_task(client, working_dir, original_prompt)
                        if slack_client and run.slack_channel_id and slack_token:
                            sess.write_agent_hooks(client, working_dir, slack_token, run.slack_channel_id, thread_ts=run.slack_thread_ts)
                        if run.service_port and task.service:
                            sess.append_service_context(client, working_dir, run.service_port, task.service.port)
                        sess.write_runner_script(
                            client, working_dir,
                            shell_init=worker.shell_init,
                            agent_cmd=agent_cmd,
                            factory_port=run.service_port or None,
                            model=coder_model if agent_cmd == "claude" else None,
                            effort=coder_effort if agent_cmd == "claude" else None,
                        )
                        if not sess.start_session(client, session_name, working_dir):
                            run.state = RunState.failed
                            run.notes = "Failed to start tmux session"
                            store.save_run(run)
                            return run
                        needs_new_session = False
                    else:
                        # Same agent — paste feedback directly into the live session
                        sess.send_feedback_to_session(client, session_name, working_dir, feedback_message)

                _log(run_id, f"  session={session_name} running, polling for completion...")
                agent_status = sess.wait_for_status(
                    client, working_dir,
                    timeout=task.coder.session_timeout,
                    poll_interval=5,
                    session_name=session_name,
                )

                captured = client.run(
                    f"tmux capture-pane -t {session_name} -p -S -200 2>/dev/null || "
                    f"cat {working_dir}/.factory/output.log 2>/dev/null || true",
                    timeout=10,
                ).stdout
                store.save_log(run_id, f"agent_output_iter{iteration}.txt", captured)
                _log(run_id, f"  agent status={agent_status}")

                rate_limited = any(
                    marker.lower() in captured.lower()
                    for marker in task.coder.rate_limit_markers
                )
                if rate_limited:
                    next_idx = current_agent_idx + 1
                    if next_idx < len(agent_commands):
                        next_name = agent_names[next_idx]
                        _log(run_id, f"  rate limit detected on {agent_name} → switching to {next_name}")
                        _slack_post(slack_client, run, f":warning: Rate limit hit on `{agent_name}` — switching to `{next_name}`")
                        current_agent_idx = next_idx
                        needs_new_session = True
                        continue
                    else:
                        run.state = RunState.failed
                        run.notes = f"All agents rate limited: {', '.join(agent_names)}"
                        store.save_run(run)
                        _log(run_id, "  all agents exhausted — giving up")
                        _slack_post(slack_client, run, f":x: All agents rate limited — giving up")
                        sess.kill_session(client, session_name)
                        if run.slack_thread_ts:
                            sess.unregister_run(client, run.slack_thread_ts)
                        return run

                if agent_status == "timeout":
                    run.state = RunState.failed
                    run.notes = f"Coder timed out after {task.coder.session_timeout}s"
                    store.save_run(run)
                    _slack_post(slack_client, run, f":warning: Agent timed out after {task.coder.session_timeout}s without completing")
                    sess.kill_session(client, session_name)
                    if run.slack_thread_ts:
                        sess.unregister_run(client, run.slack_thread_ts)
                    return run

                if agent_status == "failed":
                    _slack_post(slack_client, run, ":warning: Agent exited with an error — checking results")

                run.state = RunState.evaluating
                store.save_run(run)
                _log(run_id, f"state=evaluating  (iteration {iteration})")
                _slack_post(slack_client, run, ":mag: Running eval...")

                results = evaluator.run_eval(client, task.eval, working_dir=working_dir)
                run.eval_results = results
                _save_eval_logs(run_id, results, suffix=f"_iter{iteration}")
                _print_eval_results(run_id, results)

                if evaluator.eval_passed(results):
                    repo_base_path = task.repo.path if task.repo else working_dir
                    base_branch = _detect_base_branch(client, repo_base_path, fallback=task.repo.branch if task.repo else "master")

                    if task.untangle is not None and run.worktree_path:
                        _log(run_id, "  running untangle diff...")
                        branch = f"factory/{task.id}-{run_id}"
                        untangle_feedback = _run_untangle(client, working_dir, base_branch, branch, task.untangle)
                        if untangle_feedback:
                            store.save_log(run_id, f"untangle_iter{iteration}.json", untangle_feedback)
                            _log(run_id, f"  untangle blocked: {untangle_feedback[:120]}...")
                            if iteration < task.coder.max_iterations:
                                feedback_message = f"Structural analysis (untangle) found issues:\n{untangle_feedback}"
                                _log(run_id, "  sending untangle feedback to coder")
                                store.save_run(run)
                                continue
                            run.state = RunState.failed
                            run.notes = "Untangle structural check failed after max iterations"
                            store.save_run(run)
                            sess.kill_session(client, session_name)
                            if run.slack_thread_ts:
                                sess.unregister_run(client, run.slack_thread_ts)
                            return run
                        _log(run_id, "  untangle passed")

                    if task.crucible is not None and run.worktree_path:
                        _log(run_id, f"  running crucible review...")
                        _slack_post(slack_client, run, f":magnifying_glass_tilted_right: Running crucible review...")
                        crucible_feedback, crucible_errors, crucible_debug = _run_crucible(client, working_dir, base_branch, task.crucible)
                        store.save_log(run_id, f"crucible_iter{iteration}.json", crucible_feedback or "")
                        if crucible_errors:
                            store.save_log(run_id, f"crucible_iter{iteration}.stderr.txt", crucible_errors)
                        if crucible_debug:
                            store.save_log(run_id, f"crucible_iter{iteration}.debug.log", crucible_debug)
                        if crucible_feedback:
                            crucible_rounds_used += 1
                            _log(run_id, f"  crucible blocked ({crucible_rounds_used}/{task.crucible.rounds} rounds used): {crucible_feedback[:120]}...")
                            _slack_post(slack_client, run, f":x: Crucible found critical issues ({crucible_rounds_used}/{task.crucible.rounds} rounds used):\n```{crucible_feedback[:1000]}```")
                            if crucible_rounds_used < task.crucible.rounds:
                                feedback_message = f"Code review (crucible) found critical issues:\n{crucible_feedback}"
                                _log(run_id, "  sending crucible feedback to coder")
                                store.save_run(run)
                                continue
                            run.state = RunState.failed
                            run.notes = f"Crucible review blocked after {task.crucible.rounds} rounds"
                            store.save_run(run)
                            sess.kill_session(client, session_name)
                            if run.slack_thread_ts:
                                sess.unregister_run(client, run.slack_thread_ts)
                            return run
                        _log(run_id, "  crucible passed")
                        _slack_post(slack_client, run, ":white_check_mark: Crucible passed")

                    if task.evaluator is not None and run.worktree_path:
                        _log(run_id, "  running evaluator agent...")
                        eval_model = task.evaluator.model or worker.model
                        eval_effort = task.evaluator.effort or "low"
                        ev_result = run_evaluator(
                            client,
                            worktree_path=working_dir,
                            task_description=task.coder.prompt,
                            eval_results=results,
                            extra_criteria=task.evaluator.criteria or "",
                            timeout=task.evaluator.timeout,
                            base_branch=base_branch,
                            model=eval_model,
                            effort=eval_effort,
                        )
                        store.save_log(run_id, f"evaluator_iter{iteration}.stdout", ev_result.stdout)
                        verdict, reason = parse_verdict(ev_result.stdout)
                        run.evaluator_verdict = verdict
                        run.evaluator_reason = reason
                        _log(run_id, f"  evaluator verdict={verdict}: {reason}")

                        if verdict == "needs_changes" and iteration < task.coder.max_iterations:
                            feedback_message = reason
                            _log(run_id, "  evaluator requested changes — sending feedback to coder")
                            store.save_run(run)
                            continue

                    if task.gemini_review is not None and run.worktree_path:
                        _log(run_id, "  running gemini PR summary...")
                        _slack_post(slack_client, run, ":sparkles: Generating Gemini PR summary...")
                        try:
                            summary = run_gemini_review(
                                client,
                                worktree_path=working_dir,
                                task_description=task.coder.prompt,
                                timeout=task.gemini_review.timeout,
                                base_branch=task.repo.branch if task.repo else "main",
                                model=task.gemini_review.model,
                                gemini_cmd=task.gemini_review.gemini_cmd,
                            )
                            if summary:
                                run.gemini_summary = summary
                                store.save_log(run_id, f"gemini_review_iter{iteration}.txt", summary)
                                _log(run_id, f"  gemini summary: {summary[:200]}")
                                _slack_post(slack_client, run, f":memo: *PR Summary (Gemini):*\n{summary[:2000]}")
                            else:
                                _log(run_id, "  gemini summary: (empty response)")
                        except Exception as exc:
                            _log(run_id, f"  WARNING: Gemini review failed: {exc}")
                        store.save_run(run)

                    run.state = RunState.passed
                    store.save_run(run)
                    _log(run_id, f"state=passed  (iteration {iteration})")
                    _post_results(slack_client, run, results, passed=True)
                    if slack_client and run.slack_channel_id:
                        summary = client.run(
                            f"cat {working_dir}/.factory/completion.md 2>/dev/null || true",
                            timeout=10,
                        ).stdout.strip()
                        if summary:
                            _slack_post(slack_client, run, f":robot_face: {summary[:2000]}")
                    sess.kill_session(client, session_name)
                    if run.slack_thread_ts:
                        sess.unregister_run(client, run.slack_thread_ts)
                    _maybe_open_pr(run, task, worker, repo, issue_number, workers_path, slack_client)
                    return run

                if iteration < task.coder.max_iterations:
                    failed_output = "\n".join(r.stdout + r.stderr for r in results if r.exit_code != 0)
                    feedback_message = f"Tests/eval failed:\n{failed_output[:2000]}" if failed_output else "The eval checks did not pass. Please review your changes."
                    _log(run_id, "  eval failed — sending feedback to coder")

            run.state = RunState.failed
            store.save_run(run)
            _log(run_id, f"state=failed  (exhausted {task.coder.max_iterations} iterations)")
            _post_results(slack_client, run, run.eval_results or [], passed=False)
            _maybe_comment_failure(run, repo, issue_number)
            sess.kill_session(client, session_name)
            if run.slack_thread_ts:
                sess.unregister_run(client, run.slack_thread_ts)
            return run

        # ── Eval-only (no coder) ─────────────────────────────────────────────
        run.state = RunState.evaluating
        store.save_run(run)
        _log(run_id, "state=evaluating")

        results = evaluator.run_eval(client, task.eval, working_dir=working_dir)
        run.eval_results = results
        _save_eval_logs(run_id, results)
        _print_eval_results(run_id, results)

        passed = evaluator.eval_passed(results)
        run.state = RunState.passed if passed else RunState.failed
        store.save_run(run)
        _log(run_id, f"state={run.state}")
        _post_results(slack_client, run, results, passed=passed)
        return run

    finally:
        if run.service_port:
            teardown_port(client, run.service_port)


def spawn_watcher(
    run: Run,
    workers_path: Path,
    *,
    repo: str | None = None,
    issue_number: int | None = None,
) -> None:
    """Spawn a detached background process to run watch_task() for this run."""
    if not run.task_file:
        raise ValueError(f"Run {run.run_id!r} has no task_file — call launch_task first")

    log_path = store.RUNS_DIR / run.run_id / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "factory._watcher", run.run_id,
           "--task", run.task_file, "--workers", str(workers_path.resolve())]
    if repo:
        cmd += ["--repo", repo]
    if issue_number is not None:
        cmd += ["--issue", str(issue_number)]

    with open(log_path, "w") as log_fh:
        subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)


def run_task(task: TaskDefinition, workers_path: Path = Path("workers.yaml")) -> Run:
    """Synchronous convenience: launch + watch in the same process. Used by tests."""
    run = launch_task(task, workers_path)
    if run.state == RunState.failed:
        return run
    return watch_task(run.run_id, task, workers_path)


# ── Private helpers ───────────────────────────────────────────────────────────

def _detect_base_branch(client: SSHClient, repo_path: str, fallback: str = "master") -> str:
    """Return the remote default branch (e.g. main or master), falling back if undetectable."""
    result = client.run(
        f"git -C {repo_path} symbolic-ref refs/remotes/origin/HEAD 2>/dev/null"
        f" | sed 's|refs/remotes/origin/||'",
        timeout=10,
    )
    return result.stdout.strip() or fallback


def _github_https_url(url: str) -> str:
    """Convert a GitHub SSH or HTTPS URL to plain HTTPS (no embedded token)."""
    import re as _re
    m = _re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return f"https://github.com/{m.group(1)}.git"
    m = _re.match(r"https://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return f"https://github.com/{m.group(1)}.git"
    return url


def _create_run_worktree(client: SSHClient, task: TaskDefinition, run_id: str, worker: "WorkerConfig") -> str:
    base_path = task.repo.path
    worktree_base = worker.default_worktree_base
    worktree_path = f"{worktree_base}/{task.id}-{run_id}"
    branch = f"factory/{task.id}-{run_id}"

    if task.repo.url:
        check = client.run(f"test -d {base_path}/.git && echo exists || echo missing")
        if "missing" in check.stdout:
            clone_url = _github_https_url(task.repo.url)
            _log(run_id, f"  cloning {clone_url} -> {base_path}")
            result = client.run(f"git clone {clone_url} {base_path}", timeout=300)
            if not result.ok:
                raise RuntimeError(f"git clone failed:\n{result.stderr}")

    # Detect actual default branch from remote (handles main vs master vs custom)
    detected = client.run(
        f"git -C {base_path} symbolic-ref refs/remotes/origin/HEAD 2>/dev/null"
        f" | sed 's|refs/remotes/origin/||'",
        timeout=10,
    ).stdout.strip()
    base_branch = detected or task.repo.branch

    client.run(f"mkdir -p {worktree_base}")
    _log(run_id, f"  branch={branch}  base={base_branch}")
    result = client.run(
        f"git -C {base_path} worktree add {worktree_path} -b {branch} {base_branch}",
        timeout=30,
    )
    if not result.ok:
        raise RuntimeError(f"git worktree add failed:\n{result.stderr}")
    # Prevent factory internals (including secrets) from ever being committed
    client.run(
        f"echo '.factory/' >> {worktree_path}/.gitignore && "
        f"echo '.claude/' >> {worktree_path}/.gitignore && "
        f"echo '.crucible.toml' >> {worktree_path}/.gitignore",
        timeout=10,
    )
    return worktree_path


def _save_eval_logs(run_id: str, results: list, suffix: str = "") -> None:
    for r in results:
        safe = r.command.replace(" ", "_").replace("/", "_")
        store.save_log(run_id, f"eval_{safe}{suffix}.stdout", r.stdout)
        store.save_log(run_id, f"eval_{safe}{suffix}.stderr", r.stderr)


def _print_eval_results(run_id: str, results: list) -> None:
    for r in results:
        status = "PASS" if r.exit_code == 0 else "FAIL"
        _log(run_id, f"  [{status}] {r.command!r}  ({r.duration:.1f}s)")


def _run_untangle(client: SSHClient, working_dir: str, base_branch: str, head_branch: str, config: "UntangleConfig") -> str:
    cmd = (f"cd {working_dir} && untangle diff"
           f" --base {base_branch} --head {head_branch}"
           f" --lang {config.lang} --fail-on {config.fail_on} --format json")
    result = client.run(cmd, timeout=config.timeout)
    return "" if result.exit_code == 0 else (result.stdout or result.stderr)


def _run_crucible(client: SSHClient, working_dir: str, base_branch: str, config: "CrucibleConfig") -> tuple:
    import json as _json
    if config.model:
        import shlex as _shlex
        patch_script = (
            "c = open('.crucible.toml').read(); "
            "c = c.replace("
            "'agents = [\\n    \"claude-code\",\\n    \"codex\",\\n    \"gemini\",\\n    \"open-code\",\\n]', "
            "'agents = [\"claude-code\"]'"
            "); "
            f"c = c.replace('max_rounds = 2', 'max_rounds = {config.rounds}'); "
            "c = c.replace('\\n[gate]\\nenabled = true', '\\n[gate]\\nenabled = false'); "
            "old = '    \"-p\",\\n    \"--output-format\",\\n    \"json\",\\n]\\npersona = \"Security Auditor\"'; "
            f"new = '    \"-p\",\\n    \"--output-format\",\\n    \"json\",\\n    \"--model\",\\n    \"{config.model}\",\\n]\\npersona = \"Security Auditor\"'; "
            "open('.crucible.toml', 'w').write(c.replace(old, new))"
        )
        client.run(
            f"cd {working_dir} && rm -f .crucible.toml && crucible config init && "
            f"python3 -c {_shlex.quote(patch_script)}",
            timeout=15,
        )
    cmd = f"cd {working_dir} && crucible review --branch {base_branch} --json --debug"
    result = client.run(cmd, timeout=config.timeout)
    errors = result.stderr or ""
    debug_log = client.run(
        f"ls -t {working_dir}/.crucible/runs/*/debug.log 2>/dev/null | head -1 | xargs cat 2>/dev/null || true",
        timeout=10,
    ).stdout
    if "timed out" in errors.lower():
        return "[Error] Crucible review timed out — could not complete analysis", errors, debug_log
    if not result.stdout.strip():
        return "", errors, debug_log
    try:
        data = _json.loads(result.stdout)
    except Exception:
        return "", errors, debug_log
    if data.get("verdict", "Pass") in ("Pass", "Warn"):
        return "", errors, debug_log
    critical = [
        f"[{f['severity']}] {f['file']}:{f.get('line_start','')} {f['title']}: {f['description']}"
        for f in data.get("findings", [])
        if f.get("severity") == "Critical"
    ]
    return ("\n".join(critical) if critical else ""), errors, debug_log


def _slack_post(slack_client, run: "Run", text: str) -> None:
    if not slack_client or not run.slack_channel_id:
        return
    try:
        slack_client.post(run.slack_channel_id, text, thread_ts=run.slack_thread_ts or None)
    except Exception:
        pass


def _post_results(slack_client, run: "Run", results: list, passed: bool) -> None:
    icon = ":white_check_mark:" if passed else ":x:"
    status = "passed" if passed else "failed"
    lines = [f"{icon} *{status}*"]
    for r in results:
        mark = ":white_check_mark:" if r.exit_code == 0 else ":x:"
        lines.append(f"  {mark} `{r.command}`  ({r.duration:.1f}s)")
        if r.exit_code != 0 and (r.stdout or r.stderr):
            output = (r.stdout + r.stderr).strip()[:500]
            lines.append(f"```\n{output}\n```")
    _slack_post(slack_client, run, "\n".join(lines))


def _maybe_open_pr(
    run: "Run",
    task: TaskDefinition,
    worker: "WorkerConfig",
    repo: str | None,
    issue_number: int | None,
    workers_path: Path = Path("workers.yaml"),
    slack_client=None,
) -> None:
    if not repo:
        return
    from factory.github import GitHubClient, get_token
    from factory.poller import _push_and_pr, _branch_name
    try:
        gh = GitHubClient(get_token())
        if issue_number:
            issue = {"number": issue_number, "title": task.name.replace(f"Issue #{issue_number}: ", "")}
        else:
            issue = {"number": 0, "title": task.name}
        pr_url = _push_and_pr(gh, repo, run, task, issue, workers_path)
        if issue_number:
            verdict_line = f"\n\n**Evaluator:** {run.evaluator_reason}" if run.evaluator_verdict else ""
            comment = (f"✅ Factory run **passed** (run `{run.run_id}`){verdict_line}\n\n"
                       f"Branch: `{_branch_name(task.id, run.run_id)}`")
            gh.comment_on_issue(repo, issue_number, comment)
            gh.close_issue(repo, issue_number)
            gh.remove_label(repo, issue_number, "factory:running")
            gh.remove_label(repo, issue_number, "factory")
        if pr_url:
            run.pr_url = pr_url
            store.save_run(run)
            _log(run.run_id, f"  PR opened: {pr_url}")
            _slack_post(slack_client, run, f":arrow_heading_up: PR opened: {pr_url}")
        elif repo:
            _log(run.run_id, "  WARNING: push succeeded but PR URL was empty")
            _slack_post(slack_client, run, ":warning: Run passed but PR creation failed (check watch.log)")
    except Exception as exc:
        _log(run.run_id, f"  WARNING: PR/issue update failed: {exc}")
        _slack_post(slack_client, run, f":warning: PR creation failed: {exc}")


def _maybe_comment_failure(run: "Run", repo: str | None, issue_number: int | None) -> None:
    if not repo or not issue_number:
        return
    from factory.github import GitHubClient, get_token
    try:
        gh = GitHubClient(get_token())
        gh.comment_on_issue(repo, issue_number,
                            f"❌ Factory run **failed** (run `{run.run_id}`)\n\nNotes: {run.notes or 'see run logs'}")
        gh.remove_label(repo, issue_number, "factory:running")
        gh.remove_label(repo, issue_number, "factory")
    except Exception as exc:
        _log(run.run_id, f"  WARNING: issue comment failed: {exc}")


def _prepend_constitution(prompt: str) -> str:
    constitution_path = Path(__file__).parent / "constitution.md"
    if not constitution_path.exists():
        return prompt
    return f"{constitution_path.read_text()}\n\n---\n\n{prompt}"


def _allocate_port(worker: WorkerConfig) -> int:
    """Pick a free port from the worker's pool, avoiding ports held by active runs."""
    active_ports = {
        r.service_port
        for r in store.list_runs()
        if r.service_port and r.state == RunState.running
    }
    for slot in range(1, worker.slots + 1):
        port = worker.slot_port_base + slot
        if port not in active_ports:
            return port
    raise RuntimeError(f"All {worker.slots} service port(s) are currently in use")


def _print_commands_panel(run_id: str, worktree_path: str | None, service_port: int | None, label: str = "") -> None:
    from rich.console import Console
    _con = Console(highlight=False)
    _con.print()
    if label:
        _con.print(f"  [bold]{label}[/bold]  [dim](run {run_id})[/dim]")
    if worktree_path:
        _con.print(f"  [green]✓[/green] Worktree  [dim]{worktree_path}[/dim]")
    if service_port:
        _con.print(f"  [green]✓[/green] Port      [cyan]{service_port}[/cyan]")
    _con.print()
    _con.print("  [dim]Commands:[/dim]")
    _con.print(f"    [bold cyan]factory attach {run_id}[/bold cyan]    live tmux session")
    _con.print(f"    [bold cyan]factory logs   {run_id}[/bold cyan]    tail agent output")
    _con.print(f"    [bold cyan]factory status {run_id}[/bold cyan]    check state")
    _con.print(f"    [bold cyan]factory kill   {run_id}[/bold cyan]    stop run")
    _con.print()


def _log(run_id: str, msg: str) -> None:
    from rich.console import Console
    from rich.text import Text
    _con = Console(highlight=False)
    prefix = Text(f"[{run_id[:8]}] ", style="bold blue")
    _con.print(prefix + Text(msg))
