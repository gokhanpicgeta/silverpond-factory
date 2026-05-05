"""
Remote tmux session management for factory runs.

Execution model:
  1. Write task prompt to .factory/task.md on the remote machine
  2. Write .factory/run.sh — launches claude and writes done/failed to .factory/status
  3. Create a tmux session and start run.sh inside it (SSH returns immediately)
  4. Poll .factory/status via short SSH calls until done/failed/timeout
  5. Kill the session when finished

This decouples the SSH connection lifetime from how long Claude runs.
"""
from __future__ import annotations

import base64
import re
import shlex
import time
from typing import Optional

from rich.console import Console
from rich.text import Text

from factory.ssh import SSHClient

_console = Console(highlight=False)


def setup_factory_dir(client: SSHClient, working_dir: str) -> None:
    """Create .factory/ directory and ensure it is gitignored."""
    client.run(f"mkdir -p {working_dir}/.factory")
    client.run(
        f"grep -qxF '.factory/' {working_dir}/.gitignore 2>/dev/null || "
        f"echo '.factory/' >> {working_dir}/.gitignore"
    )


def write_task(client: SSHClient, working_dir: str, prompt: str) -> None:
    """Write the task prompt to .factory/task.md."""
    encoded = base64.b64encode(prompt.encode()).decode()
    client.run(f"echo '{encoded}' | base64 -d > {working_dir}/.factory/task.md")


def write_runner_script(
    client: SSHClient,
    working_dir: str,
    shell_init: Optional[str] = None,
    agent_cmd: str = "claude",
    factory_port: Optional[int] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> None:
    """
    Write .factory/run.sh — the script that runs inside the tmux session.

    Launches the agent, then writes 'done' or 'failed' to .factory/status.
    agent_cmd is the full command name/path for the agent (e.g. 'claude', 'codex').
    """
    lines = ["#!/bin/bash"]
    if shell_init:
        lines.append(shell_init)
    if factory_port is not None:
        lines.append(f"export FACTORY_PORT={factory_port}")

    if agent_cmd == "claude":
        # Run interactively so engineers can send mid-task messages via Slack.
        # Logging is handled via tmux pipe-pane (set up by start_session).
        # Claude writes .factory/status itself when done (told via initial prompt).
        claude_flags = "--dangerously-skip-permissions"
        if model:
            claude_flags += f" --model {model}"
        if effort:
            claude_flags += f" --effort {effort}"
        lines += [
            f"cd {working_dir}",
            "rm -f .factory/status",
            f"claude {claude_flags}",
        ]
    else:
        if agent_cmd == "codex":
            agent_invocation = 'codex --full-auto -q "$(cat .factory/task.md)"'
        else:
            agent_invocation = f'{agent_cmd} "$(cat .factory/task.md)"'
        lines += [
            f"cd {working_dir}",
            "rm -f .factory/status",
            f"{agent_invocation} 2>&1 | tee .factory/output.log",
            "if [ ${PIPESTATUS[0]} -eq 0 ]; then",
            "    echo done > .factory/status",
            "else",
            "    echo failed > .factory/status",
            "fi",
        ]
    script = "\n".join(lines) + "\n"
    encoded = base64.b64encode(script.encode()).decode()
    client.run(
        f"echo '{encoded}' | base64 -d > {working_dir}/.factory/run.sh && "
        f"chmod +x {working_dir}/.factory/run.sh"
    )


def append_service_context(client: SSHClient, working_dir: str, port: int, native_port: int) -> None:
    """Append port context to .factory/task.md so the agent knows which port to use."""
    context = (
        f"\n\n---\n"
        f"## Service Port\n\n"
        f"A port has been allocated for your use: `$FACTORY_PORT` (= `{port}`).\n\n"
        f"Start your app on this port (e.g. instead of the default `{native_port}`, use `{port}`). "
        f"The `FACTORY_PORT` environment variable is set in your shell.\n"
    )
    encoded = base64.b64encode(context.encode()).decode()
    client.run(f"echo '{encoded}' | base64 -d >> {working_dir}/.factory/task.md")


def start_session(client: SSHClient, session_name: str, working_dir: str) -> bool:
    """
    Create a tmux session, launch run.sh, enable output logging, and send the
    initial task instruction to the interactive agent.
    """
    client.run(f"tmux kill-session -t {session_name} 2>/dev/null; true")
    result = client.run(f"tmux new-session -d -s {session_name} -c {working_dir}")
    if not result.ok:
        return False

    result = client.run(
        f"tmux send-keys -t {session_name} "
        f"'bash {working_dir}/.factory/run.sh' Enter"
    )
    if not result.ok:
        return False

    # Give the session a moment to start before polling for readiness
    time.sleep(2)

    # Poll until Claude's TUI shows its input prompt (❯), then send the task.
    # Use -l (literal) so tmux doesn't misinterpret special characters.
    instruction = (
        "Please read and follow all instructions in .factory/task.md. "
        "When you have completely finished, run this shell command: "
        "echo done > .factory/status"
    )
    ready = False
    for _ in range(60):  # up to 60s
        time.sleep(1)
        pane = client.run(
            f"tmux capture-pane -t {session_name} -p 2>/dev/null", timeout=5
        ).stdout
        # The claude input prompt is a bare ❯ at the end of a line.
        # Dialog prompts show ❯ followed by text (e.g. "❯ 1. Yes") — ignore those.
        # Strip all ANSI/VT escape sequences including DEC private modes (\x1b[?...)
        clean = re.sub(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07]*\x07|[()][AB012])", "", pane)
        clean = re.sub(r"\r", "", clean)
        if any("❯" in line for line in clean.splitlines()):
            ready = True
            break

    if ready:
        # Give Claude a moment to finish rendering before sending
        time.sleep(3)

    # Write instruction to a temp file and paste via tmux buffer —
    # more reliable than send-keys -l for long text (avoids dropped keystrokes on TUI redraws).
    tmp = f"/tmp/factory-instr-{session_name}"
    encoded = base64.b64encode(instruction.encode()).decode()
    client.run(f"echo '{encoded}' | base64 -d > {tmp}", timeout=5)

    for attempt in range(3):
        if attempt > 0:
            time.sleep(3)
        client.run(f"tmux load-buffer {tmp} 2>/dev/null || true", timeout=5)
        client.run(f"tmux paste-buffer -t {session_name} -p 2>/dev/null || true", timeout=5)
        client.run(f"tmux send-keys -t {session_name} Enter", timeout=5)
        time.sleep(3)

        # Check if Claude received the instruction — welcome screen should be gone
        pane = client.run(
            f"tmux capture-pane -t {session_name} -p 2>/dev/null", timeout=5
        ).stdout
        clean = re.sub(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07]*\x07|[()][AB012])", "", pane)
        still_idle = "Welcome back" in clean or "Recent activity" in clean
        if not still_idle:
            break

    client.run(f"rm -f {tmp}", timeout=5)
    return True


def wait_for_status(
    client: SSHClient,
    working_dir: str,
    timeout: int = 600,
    poll_interval: int = 5,
    session_name: Optional[str] = None,
) -> str:
    """
    Poll .factory/status until it contains 'done' or 'failed'.
    Returns 'done', 'failed', or 'timeout'.
    """
    elapsed = 0
    last_heartbeat = 0

    while elapsed < timeout:
        result = client.run(f"cat {working_dir}/.factory/status 2>/dev/null")
        status = result.stdout.strip()
        if status in ("done", "failed"):
            return status

        # Fallback: if Claude wrote completion.md but missed writing status
        completion = client.run(
            f"test -s {working_dir}/.factory/completion.md && echo yes || true",
            timeout=5,
        ).stdout.strip()
        if completion == "yes":
            client.run(f"echo done > {working_dir}/.factory/status")
            return "done"

        if elapsed - last_heartbeat >= 60:
            m, s = divmod(elapsed, 60)
            _console.print(Text(f"  still running... {m}m{s:02d}s", style="dim"))
            last_heartbeat = elapsed

        time.sleep(poll_interval)
        elapsed += poll_interval
    return "timeout"


def write_slack_bridge(
    client: SSHClient,
    working_dir: str,
    session_name: str,
    slack_token: str,
    channel_id: str,
    run_id: str,
    thread_ts: str = "",
) -> None:
    """Write the Slack bridge script to .factory/ on the remote.
    slack-env.sh is written by write_agent_hooks before the session starts."""
    # Append session-specific vars to the existing slack-env.sh
    extra_env = (
        f"export FACTORY_RUN_ID={run_id}\n"
        f"export FACTORY_TMUX_SESSION={session_name}\n"
    )
    encoded_extra = base64.b64encode(extra_env.encode()).decode()
    client.run(
        f"echo '{encoded_extra}' | base64 -d >> {working_dir}/.factory/slack-env.sh"
    )

    bridge_script = r'''#!/usr/bin/env python3
"""Slack -> agent bridge. Forwards thread replies from engineers to the running agent.
Agent -> Slack direction is handled by Claude's Stop hook (cleaner, no pane scraping)."""
import json, os, subprocess, sys, time, urllib.request

SLACK_API = "https://slack.com/api"
POLL_INTERVAL = 5
SESSION = os.environ["FACTORY_TMUX_SESSION"]
CHANNEL = os.environ["SLACK_CHANNEL_ID"]
THREAD_TS = os.environ.get("FACTORY_THREAD_TS", "")
TOKEN = os.environ["SLACK_BOT_TOKEN"]
RUN_ID = os.environ.get("FACTORY_RUN_ID", "?")
AGENT_WINDOW = "0"


def api(method, data):
    req = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("ok"):
                print(f"[bridge] Slack {method} not ok: {result.get('error')}", file=sys.stderr)
            return result
    except Exception as e:
        print(f"[bridge] Slack error {method}: {e}", file=sys.stderr)
        return {"ok": False}


def bot_user_id():
    return api("auth.test", {}).get("user_id")


def thread_replies(oldest):
    if not THREAD_TS:
        return []
    import urllib.parse
    params = urllib.parse.urlencode({
        "channel": CHANNEL, "ts": THREAD_TS,
        "oldest": oldest, "limit": 20,
    })
    req = urllib.request.Request(
        f"{SLACK_API}/conversations.replies?{params}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("ok"):
                print(f"[bridge] replies error: {result.get('error')}", file=sys.stderr)
            return result.get("messages", [])
    except Exception as e:
        print(f"[bridge] replies exception: {e}", file=sys.stderr)
        return []


def send_to_agent(text):
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{SESSION}:{AGENT_WINDOW}", text],
        timeout=5,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{SESSION}:{AGENT_WINDOW}", "Enter"],
        timeout=5,
    )


print(f"[bridge] starting for run {RUN_ID}, thread={THREAD_TS or 'none'}")
MY_ID = bot_user_id()
print(f"[bridge] bot_user_id={MY_ID}")
last_ts = f"{time.time():.6f}"
print(f"[bridge] last_ts={last_ts}")
errors = 0

while True:
    try:
        msgs = thread_replies(last_ts)
        if msgs:
            print(f"[bridge] poll: {len(msgs)} messages")
        for msg in msgs:  # oldest-first (default Slack order)
            ts = msg.get("ts", "0")
            if float(ts) <= float(last_ts):
                continue
            last_ts = ts
            # Skip bot messages and the root thread message
            if msg.get("user") == MY_ID or msg.get("bot_id"):
                continue
            if msg.get("ts") == THREAD_TS:
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue
            print(f"[bridge] -> agent: {text[:80]}")
            send_to_agent(text)
        errors = 0
    except KeyboardInterrupt:
        break
    except Exception as e:
        errors += 1
        print(f"[bridge] error ({errors}): {e}", file=sys.stderr)
        if errors > 10:
            time.sleep(30)
    time.sleep(POLL_INTERVAL)
'''
    encoded_bridge = base64.b64encode(bridge_script.encode()).decode()
    client.run(
        f"echo '{encoded_bridge}' | base64 -d > {working_dir}/.factory/slack-bridge.py && "
        f"chmod +x {working_dir}/.factory/slack-bridge.py"
    )


def write_agent_hooks(
    client: SSHClient,
    working_dir: str,
    slack_token: str,
    channel_id: str,
    thread_ts: str = "",
) -> None:
    """
    Write Claude Stop/SessionStart hooks into the worktree.

    SessionStart hook: records the top-level Claude session ID as a marker so
    nested invocations (e.g. crucible's inner claude) stay silent.

    Stop hook: posts CLAUDE_LAST_ASSISTANT_MESSAGE to Slack, but only from the
    top-level session (nested session guard via the marker file).
    """
    import json as _json

    # ── SessionStart hook ────────────────────────────────────────────────────
    session_start = r'''#!/usr/bin/env bash
# Record the first (top-level) session ID so nested claude calls stay silent.
MARKER="$(cd "$(dirname "$0")" && pwd)/session-marker"
if [[ ! -f "$MARKER" ]]; then
    printf '%s' "$CLAUDE_SESSION_ID" > "$MARKER"
fi
'''
    encoded_ss = base64.b64encode(session_start.encode()).decode()
    client.run(
        f"echo '{encoded_ss}' | base64 -d > {working_dir}/.factory/session-start-hook.sh && "
        f"chmod +x {working_dir}/.factory/session-start-hook.sh"
    )

    # ── Stop hook ────────────────────────────────────────────────────────────
    stop_hook = r'''#!/usr/bin/env bash
# Nested session guard: only the top-level claude posts to Slack.
FACTORY_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="$FACTORY_DIR/session-marker"
[[ -f "$MARKER" ]] || exit 0
[[ "$CLAUDE_SESSION_ID" == "$(cat "$MARKER")" ]] || exit 0

MSG="$CLAUDE_LAST_ASSISTANT_MESSAGE"
[[ -z "$MSG" ]] && exit 0

source "$FACTORY_DIR/slack-env.sh"

# Write message to a temp file to avoid shell quoting issues.
TMPFILE=$(mktemp)
printf '%s' "$MSG" > "$TMPFILE"
python3 - "$TMPFILE" <<'PYEOF'
import json, os, sys, urllib.request
token = os.environ["SLACK_BOT_TOKEN"]
channel = os.environ["SLACK_CHANNEL_ID"]
with open(sys.argv[1]) as f:
    msg = f.read()
os.unlink(sys.argv[1])
thread_ts = os.environ.get("FACTORY_THREAD_TS", "")
payload = {"channel": channel, "text": msg[:3000], "unfurl_links": False}
if thread_ts:
    payload["thread_ts"] = thread_ts
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=json.dumps(payload).encode(),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
)
try:
    urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print(f"[stop-hook] Slack error: {e}", file=__import__("sys").stderr)
PYEOF
'''
    encoded_stop = base64.b64encode(stop_hook.encode()).decode()
    client.run(
        f"echo '{encoded_stop}' | base64 -d > {working_dir}/.factory/stop-hook.sh && "
        f"chmod +x {working_dir}/.factory/stop-hook.sh"
    )

    # Resolve ~ to absolute path so Claude's hook runner can find the scripts
    abs_dir = client.run(f"realpath {working_dir}", timeout=5).stdout.strip() or working_dir

    # ── slack-env.sh (sourced by stop hook and bridge) ───────────────────────
    env_content = (
        f"export SLACK_BOT_TOKEN={slack_token}\n"
        f"export SLACK_CHANNEL_ID={channel_id}\n"
        f"export FACTORY_THREAD_TS={thread_ts}\n"
    )
    encoded_env = base64.b64encode(env_content.encode()).decode()
    client.run(
        f"echo '{encoded_env}' | base64 -d > {working_dir}/.factory/slack-env.sh && "
        f"chmod 600 {working_dir}/.factory/slack-env.sh"
    )

    # ── .claude/settings.local.json ─────────────────────────────────────────
    factory_dir = f"{abs_dir}/.factory"
    settings = {
        "enabledMcpServers": {},    # suppress extension/LSP recommendation dialogs
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": f"{factory_dir}/session-start-hook.sh"}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": f"{factory_dir}/stop-hook.sh"}]}
            ],
        },
    }
    settings_json = _json.dumps(settings, indent=2)
    encoded_cfg = base64.b64encode(settings_json.encode()).decode()
    client.run(
        f"mkdir -p {working_dir}/.claude && "
        f"echo '{encoded_cfg}' | base64 -d > {working_dir}/.claude/settings.local.json"
    )


DAEMON_REMOTE_PATH = "~/factory/slack-bridge-daemon.py"
ACTIVE_RUNS_FILE = "~/factory/active-runs.json"


def deploy_bridge_daemon(client: SSHClient) -> None:
    """Copy the bridge daemon script to the worker if not already current."""
    import hashlib
    from pathlib import Path
    local = Path(__file__).parent.parent / "factory" / "slack-bridge-daemon.py"
    content = local.read_bytes()
    local_hash = hashlib.md5(content).hexdigest()
    remote_hash = client.run(
        f"md5sum {DAEMON_REMOTE_PATH} 2>/dev/null | cut -d' ' -f1 || echo ''",
        timeout=5,
    ).stdout.strip()
    if local_hash == remote_hash:
        return
    encoded = base64.b64encode(content).decode()
    client.run(
        f"mkdir -p ~/factory && "
        f"echo '{encoded}' | base64 -d > {DAEMON_REMOTE_PATH} && "
        f"chmod +x {DAEMON_REMOTE_PATH}"
    )


def register_run(client: SSHClient, run_id: str, session_name: str, thread_ts: str) -> None:
    """Add a run to the active-runs.json on the worker."""
    abs_path = client.run("echo ~/factory/active-runs.json", timeout=5).stdout.strip()
    client.run(
        f"python3 -c \"import json,os; "
        f"f='{abs_path}'; "
        f"d=json.load(open(f)) if os.path.exists(f) else {{}}; "
        f"d['{thread_ts}']={{'run_id':'{run_id}','session':'{session_name}'}}; "
        f"json.dump(d,open(f,'w'))\""
    )


def unregister_run(client: SSHClient, thread_ts: str) -> None:
    """Remove a run from the active-runs.json on the worker."""
    abs_path = client.run("echo ~/factory/active-runs.json", timeout=5).stdout.strip()
    client.run(
        f"python3 -c \"import json,os; "
        f"f='{abs_path}'; "
        f"d=json.load(open(f)) if os.path.exists(f) else {{}}; "
        f"d.pop('{thread_ts}', None); "
        f"json.dump(d,open(f,'w'))\""
    )


def ensure_bridge_daemon(client: SSHClient, slack_app_token: str, slack_bot_token: str, channel_id: str) -> bool:
    """Deploy and start the bridge daemon on the worker if not already running."""
    deploy_bridge_daemon(client)
    status = client.run(
        f"SLACK_APP_TOKEN={slack_app_token} SLACK_BOT_TOKEN={slack_bot_token} "
        f"SLACK_CHANNEL_ID={channel_id} "
        f"python3 {DAEMON_REMOTE_PATH} status 2>/dev/null || echo 'not running'",
        timeout=10,
    ).stdout.strip()
    if "running" in status and "not running" not in status:
        return True
    result = client.run(
        f"SLACK_APP_TOKEN={slack_app_token} SLACK_BOT_TOKEN={slack_bot_token} "
        f"SLACK_CHANNEL_ID={channel_id} "
        f"python3 {DAEMON_REMOTE_PATH} start 2>&1",
        timeout=15,
    )
    return result.ok


def kill_session(client: SSHClient, session_name: str) -> None:
    """Kill the tmux session."""
    client.run(f"tmux kill-session -t {session_name} 2>/dev/null; true")


def session_name_for_run(run_id: str) -> str:
    return f"factory-{run_id}"
