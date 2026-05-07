# Silverpond Factory

An automated software factory that runs Claude AI agents on a remote worker machine to implement tasks, reviews the code with Crucible, and opens pull requests — all from a single command.

---

## Two ways to use it

### 1. One-off task

```bash
factory run "Add a health check endpoint that returns {\"status\": \"ok\"}" --repo owner/my-repo
```

Claude runs on the worker, implements the task, Crucible reviews the result, and if anything needs fixing it retries automatically. A PR is opened on pass.

### 2. Batch from GitHub issues

Label issues with `factory`, then:

```bash
factory poll owner/my-repo
```

Every labeled issue gets picked up in parallel — each becomes a task for Claude to fix, with a PR opened on success.

---

## Prerequisites

Before you start, ask your admin to set up:

- **SSH access to ares** — your public key needs to be added to the worker machine
- **Claude authenticated on ares** — already done once for the team, nothing to do here

---

## Setup

### 1. Install

```bash
git clone https://github.com/silverpond/silverpond-factory
cd silverpond-factory
pip install -e .
```

### 2. Fill in your `.env`

```bash
cp .env.example .env
```

Then edit `.env` and add:

| Variable | Where to get it |
|---|---|
| `FACTORY_GITHUB_TOKEN` | Create at https://github.com/settings/tokens — scopes: `repo`, `issues`, `pull_requests`, `workflows` |
| `SLACK_BOT_TOKEN` | Ask your admin — from the shared Slack app at api.slack.com/apps |
| `SLACK_APP_TOKEN` | Ask your admin — same Slack app, under "App-Level Tokens" |
| `SLACK_DEFAULT_REVIEWERS` | Comma-separated Slack member IDs to invite to each run's channel (find yours at slack.com/account/profile) |

Slack is optional — leave those blank to skip notifications.

### 3. Run the setup wizard

```bash
factory setup
```

This asks for your username on the worker machine, your SSH key, writes your GitHub token to the worker so clone/push works, and optionally creates the standard factory labels on your repo.

> **Note:** `workers.yaml` is already configured for the Silverpond ares machine. Your personal SSH key and username stay in `.env` (gitignored) — not in the repo.

### Worker machine requirements

- `claude` CLI installed and authenticated
- `tmux`
- `crucible` — `nix profile install github:jonochang/crucible` (for code review)
- `untangle` — `cargo install untangle` (Rust projects only)

---

## Running tasks

### Inline

```bash
# Basic
factory run "Fix the login bug" --repo owner/my-repo

# With eval commands (run after the agent finishes each iteration)
factory run "Add input validation" --repo owner/my-repo --eval "pytest" --eval "ruff check ."

# Override model and effort (haiku + low is cheapest)
factory run "Refactor the auth module" --repo owner/my-repo --model haiku --effort low

# Control how many Crucible review rounds run (0 = skip Crucible)
factory run "Add a loading spinner" --repo owner/my-repo --crucible-rounds 2

# If you're inside a git repo, --repo is inferred automatically
cd ~/projects/my-repo
factory run "Add a health check endpoint"
```

Without `--eval`, Crucible is the only quality gate — it reviews the diff and sends feedback to Claude if it finds critical issues.

### Poll GitHub issues

```bash
# Basic — picks up all issues labeled 'factory'
factory poll owner/my-repo

# With eval commands
factory poll owner/my-repo --eval "bundle exec rails test"

# Override model/effort for all issues in this poll
factory poll owner/my-repo --model haiku --effort low

# Control crucible rounds
factory poll owner/my-repo --crucible-rounds 2

# Cap how many issues run in parallel (default: worker slot count)
factory poll owner/my-repo --max-concurrency 2
```

Individual issues can override model/effort via labels:
- `factory:model:opus` — run that issue with Opus
- `factory:effort:low` — run with low effort

---

## Monitoring

The terminal frees up immediately after launch. Use these to check on runs:

```bash
factory status                  # list all runs (includes PR URL)
factory status <run_id>         # detail for one run
factory attach <run_id>         # attach to the live tmux session on the worker
factory logs <run_id>           # tail agent output
factory kill <run_id>           # stop a run
factory workers                 # show active sessions + CPU/mem on each worker
```

---

## Maintenance

```bash
# Remove worktrees on the worker for finished runs
factory cleanup                 # remove all dead worktrees on ares
factory cleanup --dry-run       # preview first

# Remove local run history
factory runs-clean              # remove finished runs older than 7 days
factory runs-clean --days 1     # keep only last day
factory runs-clean --all        # remove all finished runs
factory runs-clean --dry-run    # preview first
```

---

## Advanced: task YAML files

For repeatable or complex tasks, define them in a YAML file:

```bash
factory run tasks/my-task.yaml                              # run from YAML
factory poll owner/my-repo --template tasks/my-task.yaml   # poll with YAML template
```

YAML files let you configure the full pipeline — eval commands, crucible rounds, untangle, evaluator, Slack reviewers, service ports, and more. See `tasks/todo.yaml` for a complete example.

---

## Pipeline

```
worktree → agent → eval → crucible (N rounds) → evaluator → PR
```

Any stage that fails sends feedback back to the agent for another iteration (up to `max_iterations`, default 3). Stages not configured are skipped.
