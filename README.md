# Managing managers

A Flow Atelier package that runs a queue of written briefs. A brief, not a clock, decides what is
built and when work stops. For each brief an Opus manager plans tasks against the brief's
definition of done, Codex workers build them, fresh Codex reviewers review the whole brief, and
the brief ends as soon as the review loop closes. Each brief gets its own branch, stacked on the
previous one, and its own pull request.

Write the briefs with the `managing-managers` skill from
[flow-atelier-skills](https://github.com/Andesprit/flow-atelier-skills); it also explains the run
in detail.

## Install and run

```bash
git worktree add ../project-briefs -b mm/run
cd ../project-briefs
atelier add Andesprit/managing-managers --project
# write .atelier/briefs/01-....md, 02-....md (the skill does this)
atelier check managing_managers --recursive --probe
CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 atelier run managing_managers
```

Requires Flow Atelier 0.13.0 or newer, Git, Bash, Python 3.11+ as `python3`, and logged-in Claude
Code and Codex. Pull requests need an `origin` remote and a signed-in `gh`.

## Conduits

| Conduit | Role |
| --- | --- |
| `managing_managers` | Reads `.atelier/briefs/` into the queue, runs one brief per pass, prints a summary |
| `mm_queue_step` | Opens the next brief on branch `mm/<id>`, runs it, pushes it and opens its pull request |
| `mm_brief` | One stage of a brief: manager (plan, check, triage, finish), worker, or reviewer |

`mm_brief/scripts/run.py` is the controller: it owns state and Git, validates every agent result
(see `mm_brief/stage-results.md`), runs measured outcomes itself, enforces a brief's allowed
paths, applies the review closing rules and writes each brief's report. `usage.py` is copied
unchanged from [automatic-goal](https://github.com/Andesprit/automatic-goal).

## Review loop

- Rounds 1-3 close when a round has no critical and no high finding.
- Later rounds close when a round has no critical finding; remaining high findings go in the
  pull request.
- Critical findings after `max_review_rounds` (default 6) pause the brief, and the queue stops.
- Medium and low findings never keep the loop open; they are listed in the report.

State lives untracked in `.atelier/mm/`: `queue.json`, `summary.md`, and per brief
`briefs/<id>/state.json` and `report.md`.

## Tests

```bash
python -m unittest tests.test_conduits
```

Run them with the Python that has Atelier's `acp` package (for example
`~/.local/share/uv/tools/flow-atelier/bin/python`) so the end-to-end test drives `atelier run`
with the scripted fake agent in `tests/fake_agent.py`.
