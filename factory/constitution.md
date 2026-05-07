# Factory Constitution

These rules apply to every factory run. Read them before starting work.

## Git discipline
- Work only in the current directory (your assigned worktree). Never touch the base repo directly.
- Make focused, minimal commits. Do not commit unrelated changes, generated artifacts, or debug code.
- If the worktree or branch layout looks wrong, stop and report it rather than proceeding silently.

## How to work
- Implement one change at a time. Do not attempt multiple unrelated fixes in a single run.
- Do not add new dependencies unless the task explicitly requires it.
- Do not install packages, run the project, or set up the environment. Assume dependencies are already installed.
- Do not run tests or eval commands — the pipeline handles that after you finish.

## Before finishing
Write a short completion summary to `.factory/completion.md`:
1. What files you changed and why
2. Any assumptions you made or open questions

Then signal completion by running:
```
echo done > .factory/status
```
The pipeline cannot continue until this file exists.
