# Stage results

Every stage writes one JSON object to `result_path` from `request.json`, with `request_id` copied
from it. Run `python3 <conduit_dir>/scripts/run.py check` from the worktree root before you finish
and repair the file until it prints OK. Fields not listed are ignored.

Severity scale (used by reviewers, the manager and the report):

- critical: data loss, a security hole, or the brief's main path does not work at all.
- high: a definition-of-done outcome fails, behavior the brief says to keep broke, or a required
  check fails.
- medium: a real defect that leaves the definition of done working (an edge case, a rough message).
- low: polish.

## Manager: PLAN (`request.phase` is PLAN)

Read the brief at `brief_path`. Turn its definition of done into a scorecard, one outcome per item,
then break the brief into small tasks. Plan only what the brief asks; out-of-scope ideas are not
tasks. A measured outcome has a read-only command whose last output line is one number; the
controller runs it itself now and at the end, so keep it fast and read-only.

```json
{"request_id": "...", "summary": "the plan in two sentences",
 "scorecard": [
   {"id": "tests-pass", "outcome": "The test suite passes", "kind": "measured",
    "command": "pytest -q >/dev/null 2>&1; echo $?", "direction": "down", "target": 0},
   {"id": "login-error", "outcome": "A wrong password shows a clear message", "kind": "judged",
    "how": "Log in with a wrong password and read the message"}],
 "tasks": [
   {"id": "message", "title": "Show the error message", "scope": ["src/login.py"],
    "outcomes": ["login-error"]}]}
```

Task ids are lowercase letters, digits and dashes. Every task names the scorecard outcomes it
serves.

## Worker: WORK (`request.stage` is WORK)

`request.task` holds the task, its `kind` (build, continue or fix), the manager's `direction` and,
for a fix, the review `findings` to resolve. Build only this task. Commit passing work on the
current branch (several commits are fine); never amend, merge, rebase or reset. Finish with a clean
worktree. Stop early with CHECKPOINT and a question when a decision is not yours to make.

```json
{"request_id": "...", "status": "READY", "head": "<git rev-parse HEAD>",
 "summary": "what changed and how you checked it",
 "checks": [{"command": "pytest -q", "exit_code": 0, "summary": "41 passed"}]}
```

CHECKPOINT needs `question`; BLOCKED needs `reason`. Both need `head` and `summary`.

## Manager: CHECK (`request.phase` is CHECK)

`request.task` holds the task and the worker's `last` result. Judge it against the scorecard
outcomes the task serves, and look at the commits since `request.task.base`.

- ACCEPT: worth keeping. Name in `outcome` the scorecard id it advances; a change that cannot name
  one is rejected.
- REVISE: send it back with a concrete `direction`.
- CONTINUE: answer a CHECKPOINT question in `direction`.
- ABANDON: not worth more turns; its commits are kept under a ref and dropped from the branch.
- PAUSE: only a human can unblock it; the queue stops here.

```json
{"request_id": "...", "verdict": "ACCEPT", "outcome": "login-error",
 "reason": "The message appears and the tests pass",
 "new_tasks": []}
```

`new_tasks` (same shape as in PLAN) adds tasks that the brief needs and the plan missed.

## Reviewer: REVIEW (`request.stage` is REVIEW)

Review the whole brief: the diff `request.base..HEAD` against the brief. Run the checks that
matter. Tie every finding to the brief (`about: "brief"`) or to correctness
(`about: "correctness"`). Anything outside the brief is a suggestion, never a finding.

```json
{"request_id": "...", "head": "<git rev-parse HEAD>", "summary": "one paragraph verdict",
 "checks": [{"command": "pytest -q", "exit_code": 0, "summary": "41 passed"}],
 "findings": [{"severity": "high", "about": "brief", "finding": "A wrong password still shows a stack trace",
               "fix": "The login form shows 'Wrong password' and no trace"}],
 "suggestions": ["Rate-limit login attempts"]}
```

## Manager: TRIAGE (`request.phase` is TRIAGE)

`request.findings` are the findings that keep the review loop open, indexed from 0. Send each one
to the accepted task whose worker built that code, or dismiss it with a reason. Each index exactly
once.

```json
{"request_id": "...",
 "fixes": [{"task": "message", "findings": [0], "direction": "Catch the error in the view"}],
 "dismissed": [{"finding": 1, "reason": "Contradicts the brief's out-of-scope list"}]}
```

## Manager: FINISH (`request.phase` is FINISH)

Use the result as its user would, judge every judged outcome, and write the pull request for
`request.pr_base`. The controller measures the measured outcomes itself and appends its report.

```json
{"request_id": "...", "summary": "what the brief delivered",
 "judged": [{"id": "login-error", "state": "met", "evidence": "Wrong password shows 'Wrong password'"}],
 "questions": ["Should the message be translated?"],
 "pr_title": "Show a clear message on a wrong password",
 "pr_body": "# What\n...\n# Why\n...\n# How\n...\n# Validation\n..."}
```

`state` is unmet, partial or met. At most three questions. `pr_title` and `pr_body` are needed only
when the brief committed something. If `request.blocked` is true, the review loop hit its cap with
critical findings left: write only `{"request_id": "...", "blocking": "what blocks and what the
user must decide"}`.
