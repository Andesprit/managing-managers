"""Brief queue, per-brief state and Git guards for managing-managers (standard library only).

A brief, not a clock, decides what is built: a manager plans tasks against the brief's definition
of done, workers build them, fresh reviewers check the whole brief, and the brief ends as soon as
the review loop closes. Time is only a safety cap.
"""

import argparse
import copy
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(".atelier/mm")
BRIEFS = Path(".atelier/briefs")
BRIEF_ID = re.compile(r"[0-9a-z][0-9a-z-]{0,59}")
SEVERITIES = ("critical", "high", "medium", "low")
JUDGED = {"unmet", "partial", "met"}
# One scorecard command may take this long; the controller runs them, so they must stay cheap.
MEASURE_SECONDS = 120
# After this many rounds a high finding no longer keeps the review loop open.
STRICT_ROUNDS = 3
PR_BODY_LIMIT = 65536
# Set by `check`: validate a result without changing state or Git.
DRY = False


class Checked(Exception):
    """A dry check reached the first change to state or Git, so the result is valid."""


def effect():
    if DRY:
        raise Checked


def git(*args):
    proc = subprocess.run(["git", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
    return proc.stdout.strip()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(value)
    os.replace(name, path)


def write_json(path, value):
    atomic(path, json.dumps(value, indent=2) + "\n")


def read_json(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def dirty():
    return [line for line in git("status", "--porcelain").splitlines()
            if not line.startswith("?? .atelier/")]


def guard(clean=True):
    if not DRY and os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") != "1":
        raise RuntimeError("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS must be exactly 1")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != Path.cwd().resolve():
        raise RuntimeError("run from the worktree root")
    if Path(git("rev-parse", "--absolute-git-dir")) == Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")):
        raise RuntimeError("use a dedicated linked worktree (git worktree add ../<dir> -b <branch>)")
    branch = git("symbolic-ref", "--short", "-q", "HEAD")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise RuntimeError("unsupported branch name")
    if git("ls-files", ".atelier"):
        raise RuntimeError("tracked files under .atelier are not allowed")
    if clean and dirty():
        raise RuntimeError("worktree is not clean (only untracked .atelier/ is allowed)")
    return branch


# ---------- briefs and queue ----------

def parse_brief(path):
    """Split a brief into its `## ` sections; only the title, goal, done and allowed paths are read."""
    text = path.read_text(encoding="utf-8")
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
    sections, name = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip().lower()
            sections[name] = []
        elif name:
            sections[name].append(line)
    bullets = lambda key: [l.strip()[2:].strip().strip("`") for l in sections.get(key, [])
                           if l.strip().startswith("- ") and l.strip()[2:].strip()]
    if not title:
        raise ValueError(f"{path}: needs a '# Title' line")
    for key in ("goal", "in scope", "definition of done"):
        if not "\n".join(sections.get(key, [])).strip():
            raise ValueError(f"{path}: needs a nonempty '## {key.capitalize()}' section")
    done = bullets("definition of done")
    if not 1 <= len(done) <= 6:
        raise ValueError(f"{path}: definition of done needs one to six '- ' outcomes")
    return {"title": title, "allowed_paths": bullets("allowed paths"), "done": done}


def brief_files():
    files = sorted(BRIEFS.glob("*.md"))
    for path in files:
        if not BRIEF_ID.fullmatch(path.stem):
            raise ValueError(f"{path.name}: brief file names must be lowercase letters, digits and dashes")
    return files


def load_queue():
    return read_json(ROOT / "queue.json")


def save_queue(queue):
    write_json(ROOT / "queue.json", queue)


def init(hours, max_rounds, max_turns, pull_request, base):
    """Create the queue from .atelier/briefs, or continue an unfinished one; prints the deadline."""
    if not 0 < hours <= 720:
        raise ValueError("hours must be more than 0 and at most 720")
    if not 1 <= max_rounds <= 10 or not 1 <= max_turns <= 20:
        raise ValueError("max_rounds must be 1-10 and max_task_turns 1-20")
    if pull_request not in {"true", "false"}:
        raise ValueError("pull_request must be true or false")
    if base and not re.fullmatch(r"[A-Za-z0-9._/-]+", base):
        raise ValueError("invalid pull_request_base")
    branch = guard()
    files = brief_files()
    if not files:
        raise ValueError(f"no briefs found in {BRIEFS}/; write them with the managing-managers skill")
    for path in files:
        parse_brief(path)
    deadline = time.time() + hours * 3600
    settings = {"deadline": deadline, "max_rounds": max_rounds, "max_turns": max_turns,
                "pull_request": pull_request == "true", "base": base}
    if (ROOT / "queue.json").exists():
        queue = load_queue()
        # Deleting a paused brief's folder is how the user asks to start that brief over.
        queue["briefs"] = [b if b["status"] != "PAUSED" or brief_dir(b["id"]).exists()
                           else {"id": b["id"], "status": "PENDING"} for b in queue["briefs"]]
        paused = [b["id"] for b in queue["briefs"] if b["status"] == "PAUSED"]
        if paused:
            raise RuntimeError(f"brief {paused[0]} is paused; read .atelier/mm/briefs/{paused[0]}/report.md, "
                               f"then delete that folder and branch mm/{paused[0]} to start it over")
        known = {b["id"] for b in queue["briefs"]}
        # Briefs added since the last run join the end of the queue.
        queue["briefs"] += [{"id": p.stem, "status": "PENDING"} for p in files if p.stem not in known]
        queue.update(settings)
    else:
        queue = {"version": 1, "started": time.time(), "start_branch": branch,
                 "start_head": git("rev-parse", "HEAD"),
                 "briefs": [{"id": p.stem, "status": "PENDING"} for p in files], **settings}
    save_queue(queue)
    return str(int(deadline))


def start():
    """Open the next brief on its own branch, stacked on the previous brief; prints its ID or QUEUE_END."""
    queue = load_queue()
    entry = next((b for b in queue["briefs"] if b["status"] != "DONE"), None)
    if entry is None or entry["status"] == "PAUSED" or time.time() >= queue["deadline"]:
        return "QUEUE_END"
    if entry["status"] == "ACTIVE":
        # Resumed run: the brief's branch must still be checked out.
        if guard(clean=False) != entry["branch"]:
            raise RuntimeError(f"brief {entry['id']} runs on {entry['branch']}; check it out to resume")
        return entry["id"]
    guard()
    path = BRIEFS / f"{entry['id']}.md"
    brief = parse_brief(path)
    branch = f"mm/{entry['id']}"
    if git("branch", "--list", branch):
        raise RuntimeError(f"branch {branch} already exists; delete it or rename the brief")
    previous = [b for b in queue["briefs"] if b["status"] == "DONE" and b.get("committed")]
    # The pull request targets the last brief that changed something, so each PR shows only its own work.
    pr_base = previous[-1]["branch"] if previous else queue["base"]
    git("switch", "-q", "-c", branch)
    head = git("rev-parse", "HEAD")
    entry.update(status="ACTIVE", branch=branch, pr_base=pr_base, base=head)
    state = {"version": 1, "id": entry["id"], "title": brief["title"], "brief_path": str(path),
             "allowed_paths": brief["allowed_paths"], "branch": branch, "base": head,
             "started": time.time(), "status": "ACTIVE", "phase": "PLAN", "scorecard": None,
             "readings": {}, "tasks": [], "active": None, "round": 0, "reviews": [], "fixes": [],
             "remaining_high": [], "minor": [], "suggestions": [], "dismissed": [], "final": None,
             "request": None, "protocol_failures": 0, "events": [], "reason": "Planning pending."}
    save(state)
    save_queue(queue)
    return entry["id"]


# ---------- brief state ----------

def brief_dir(identifier):
    return ROOT / "briefs" / identifier


def current():
    queue = load_queue()
    entry = next((b for b in queue["briefs"] if b["status"] == "ACTIVE"), None)
    if entry is None:
        raise RuntimeError("no active brief")
    return read_json(brief_dir(entry["id"]) / "state.json")


def save(state):
    write_json(brief_dir(state["id"]) / "state.json", state)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def texts(value, name, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be a {'nonempty ' if nonempty else ''}list")
    for item in value:
        text(item, name)


def number(value, name):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number")


def checks(value, passing=False):
    if not isinstance(value, list) or not value:
        raise ValueError("checks must include reproducible validation")
    for item in value:
        text(item["command"], "check command")
        text(item["summary"], "check summary")
        if type(item["exit_code"]) is not int or (passing and item["exit_code"] != 0):
            raise ValueError("passing checks with integer exit codes required")


def validate_scorecard(card):
    if not isinstance(card, list) or not 1 <= len(card) <= 6:
        raise ValueError("scorecard must list one to six outcomes, one per definition-of-done item")
    for item in card:
        text(item["id"], "scorecard id")
        text(item["outcome"], "scorecard outcome")
        if item["kind"] == "measured":
            text(item["command"], "scorecard command")
            if item["direction"] not in {"up", "down"}:
                raise ValueError("a measured outcome needs direction up or down")
            number(item["target"], "scorecard target")
        elif item["kind"] == "judged":
            text(item["how"], "scorecard how")
        else:
            raise ValueError("scorecard kind must be measured or judged")
    if len({item["id"] for item in card}) != len(card):
        raise ValueError("scorecard ids must be unique")


def measure(card):
    """Run every measured outcome; the controller reads the numbers, never an agent's claim."""
    values, errors = {}, {}
    for item in card:
        if item["kind"] != "measured":
            continue
        try:
            proc = subprocess.run([shutil.which("bash") or "bash", "-c", item["command"]],
                                  capture_output=True, text=True, timeout=MEASURE_SECONDS,
                                  stdin=subprocess.DEVNULL)
            if proc.returncode:
                raise ValueError(f"exit code {proc.returncode}")
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            if not lines:
                raise ValueError("no output")
            values[item["id"]] = float(lines[-1])
            number(values[item["id"]], "the last line of output")
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            values[item["id"]] = None
            errors[item["id"]] = f"{type(error).__name__}: {error}"[:300]
    left = dirty()
    if left:
        git("checkout", "--", ".")
        git("clean", "-fdq", "-e", ".atelier")
        errors["worktree"] = ("a command changed the worktree (restored): " + "; ".join(left))[:300]
    return {"at": time.time(), "head": git("rev-parse", "HEAD"), "values": values, "errors": errors}


def reached(outcome, judged, reading):
    if outcome["kind"] == "judged":
        return judged.get(outcome["id"], {}).get("state") == "met"
    value = reading["values"].get(outcome["id"])
    if value is None:
        return False
    return value >= outcome["target"] if outcome["direction"] == "up" else value <= outcome["target"]


def validate_tasks(tasks, state, known):
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")
    outcomes = {o["id"] for o in state["scorecard"]}
    for task in tasks:
        text(task["id"], "task id")
        if not BRIEF_ID.fullmatch(task["id"]):
            raise ValueError("task ids must be lowercase letters, digits and dashes")
        text(task["title"], "task title")
        texts(task["scope"], "task scope")
        texts(task["outcomes"], "task outcomes")
        # Every change must name the brief outcome it advances.
        if not set(task["outcomes"]) <= outcomes:
            raise ValueError("task outcomes must name scorecard ids")
        if task["id"] in known:
            raise ValueError(f"task id {task['id']} is already used")
        known.add(task["id"])
    return [{**{k: t[k] for k in ("id", "title", "scope", "outcomes")}, "status": "PENDING",
             "turns": 0, "notes": []} for t in tasks]


def task(state, identifier=None):
    return next(t for t in state["tasks"] if t["id"] == (identifier or state["active"]))


def next_task(state):
    """Start the next pending task, or move on to review once every task is closed."""
    pending = next((t for t in state["tasks"] if t["status"] == "PENDING"), None)
    if pending:
        pending.update(status="BUILDING", base=git("rev-parse", "HEAD"), turns=0)
        state.update(active=pending["id"], phase="WORK")
        return
    state["active"] = None
    # Nothing committed means nothing to review: go straight to the finish.
    state["phase"] = "REVIEW" if git("rev-parse", "HEAD") != state["base"] else "FINISH"


def verify_commits(state, base):
    head = git("rev-parse", "HEAD")
    if head == base:
        return head
    git("merge-base", "--is-ancestor", base, head)
    if git("rev-list", "--merges", f"{base}..{head}"):
        raise ValueError("merge commits are not allowed; add a normal commit instead")
    if git("log", "--format=%H", f"{base}..{head}", "--", ".atelier"):
        raise ValueError(".atelier was committed; never stage .atelier files")
    allowed = state["allowed_paths"]
    if allowed:
        # Net change against the brief base, so a later commit can take an out-of-scope change back.
        outside = [p for p in git("diff", "--name-only", state["base"], head).splitlines()
                   if not any(fnmatch.fnmatch(p, pattern) for pattern in allowed)]
        if outside:
            raise ValueError("files outside the brief's allowed paths changed: " + ", ".join(outside[:20]) +
                             "; commit a change that restores them")
    return head


def plan(state, result):
    validate_scorecard(result["scorecard"])
    text(result["summary"], "summary")
    state["scorecard"] = result["scorecard"]
    reading = measure(state["scorecard"])
    if reading["errors"]:
        raise ValueError("scorecard commands must print one number on their last line, exit 0 and leave "
                         "the worktree alone: " + "; ".join(f"{k}: {v}" for k, v in reading["errors"].items()))
    tasks = validate_tasks(result["tasks"], state, set())
    if not tasks:
        raise ValueError("plan at least one task")
    effect()
    state.update(tasks=tasks, readings={"baseline": reading}, reason=result["summary"])
    next_task(state)


def worked(state, result):
    item = task(state)
    status = result["status"]
    if status not in {"READY", "CHECKPOINT", "BLOCKED"}:
        raise ValueError("status must be READY, CHECKPOINT or BLOCKED")
    text(result["summary"], "summary")
    if dirty():
        raise ValueError("the worktree is not clean: commit your work or discard it before you finish")
    head = verify_commits(state, item["base"])
    if result["head"] != head:
        raise ValueError("head does not match git rev-parse HEAD")
    if status == "READY":
        checks(result["checks"], passing=True)
    if status == "CHECKPOINT":
        text(result["question"], "question")
    if status == "BLOCKED":
        text(result["reason"], "reason")
    effect()
    item.update(head=head, turns=item["turns"] + 1, last=result)
    fix = state["fixes"][0] if state["fixes"] else None
    if fix:
        # A repair is judged by the next review round, not by the manager.
        fix["result"] = {k: result.get(k) for k in ("status", "summary", "reason")}
        state["fixes"].pop(0)
        if state["fixes"]:
            state["active"] = state["fixes"][0]["task"]
        else:
            state.update(active=None, phase="REVIEW")
        return
    item["status"] = "WAITING"
    state["phase"] = "CHECK"


def retire(state, item):
    """Keep an abandoned task's commits under a ref, then drop them from the branch."""
    effect()
    if item.get("head", item["base"]) != item["base"]:
        ref = f"refs/managing-managers/{state['id']}/{item['id']}"
        git("update-ref", ref, item["head"])
        git("reset", "-q", "--hard", item["base"])
        item["kept_at"] = ref


def checked(state, result, max_turns):
    item = task(state)
    verdict = result["verdict"]
    if verdict not in {"ACCEPT", "REVISE", "CONTINUE", "ABANDON", "PAUSE"}:
        raise ValueError("verdict must be ACCEPT, REVISE, CONTINUE, ABANDON or PAUSE")
    text(result["reason"], "reason")
    new = validate_tasks(result.get("new_tasks", []), state, {t["id"] for t in state["tasks"]})
    if verdict == "ACCEPT":
        if item.get("head", item["base"]) == item["base"]:
            raise ValueError("nothing was committed, so there is nothing to ACCEPT; ABANDON instead")
        text(result["outcome"], "outcome")
        if result["outcome"] not in item["outcomes"]:
            raise ValueError("ACCEPT must name the scorecard outcome this task advances; "
                             "a change that cannot name one is rejected")
    if verdict in {"REVISE", "CONTINUE"}:
        text(result["direction"], "direction")
        if item["turns"] >= max_turns:
            raise ValueError(f"this task used its {max_turns} worker turns: ACCEPT what is worth keeping, "
                             "ABANDON it, or PAUSE for the user")
    effect()
    state["tasks"] += new
    item["notes"].append({"verdict": verdict, "reason": result["reason"],
                          "direction": result.get("direction", "")})
    state["reason"] = result["reason"]
    if verdict == "ACCEPT":
        item["status"] = "ACCEPT"
        next_task(state)
    elif verdict in {"REVISE", "CONTINUE"}:
        item.update(status="BUILDING", direction=result["direction"])
        state["phase"] = "WORK"
    elif verdict == "ABANDON":
        retire(state, item)
        item["status"] = "ABANDON"
        next_task(state)
    else:
        pause(state, result["reason"])


def pause(state, reason):
    state.update(status="PAUSED", reason=reason, finished=time.time(), request=None, active=None)


def reviewed(state, result, max_rounds):
    if result["head"] != git("rev-parse", "HEAD"):
        raise ValueError("review must name the reviewed HEAD")
    text(result["summary"], "summary")
    checks(result["checks"])
    findings = result["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    for item in findings:
        if item["severity"] not in SEVERITIES:
            raise ValueError("severity must be critical, high, medium or low")
        if item["about"] not in {"brief", "correctness"}:
            raise ValueError("about must be brief or correctness; anything outside the brief is a suggestion")
        text(item["finding"], "finding")
        text(item["fix"], "fix")
    texts(result.get("suggestions", []), "suggestions", False)
    effect()
    state["round"] += 1
    rounds = state["round"]
    strict = rounds <= STRICT_ROUNDS
    blocking = [f for f in findings if f["severity"] == "critical" or (strict and f["severity"] == "high")]
    state["reviews"].append({"round": rounds, "head": result["head"], "summary": result["summary"],
                             "findings": findings, "blocking": blocking})
    state["minor"] += [f for f in findings if f["severity"] in {"medium", "low"}]
    state["suggestions"] += result.get("suggestions", [])
    if not blocking:
        state["remaining_high"] = [f for f in findings if f["severity"] == "high"]
        state.update(phase="FINISH", reason=f"Review loop closed after round {rounds}.")
    elif rounds >= max_rounds:
        state.update(phase="FINISH", blocked=True,
                     reason=f"Critical findings remain after {rounds} review rounds.")
    else:
        state.update(phase="TRIAGE", reason=f"Round {rounds}: {len(blocking)} findings to fix.")


def triaged(state, result):
    blocking = state["reviews"][-1]["blocking"]
    wanted = set(range(len(blocking)))
    fixes, dismissed = result.get("fixes", []), result.get("dismissed", [])
    seen = []
    accepted = {t["id"] for t in state["tasks"] if t["status"] == "ACCEPT"}
    for fix in fixes:
        if fix["task"] not in accepted:
            raise ValueError("a fix must go to an accepted task, whose worker conversation built that code")
        text(fix["direction"], "direction")
        seen += fix["findings"]
    for item in dismissed:
        text(item["reason"], "dismissal reason")
        seen.append(item["finding"])
    if sorted(seen) != sorted(wanted):
        raise ValueError(f"assign or dismiss each blocking finding exactly once: indexes 0-{len(blocking) - 1}")
    effect()
    queue = []
    for fix in fixes:
        queue.append({"task": fix["task"], "direction": fix["direction"],
                      "findings": [blocking[i] for i in fix["findings"]]})
    state["dismissed"] += [{**blocking[d["finding"]], "reason": d["reason"]} for d in dismissed]
    state["fixes"] = queue
    if queue:
        state.update(active=queue[0]["task"], phase="WORK")
    else:
        # Everything was dismissed: the next fresh reviewer gets the final word.
        state["phase"] = "REVIEW"


def finished(state, result):
    if state.get("blocked"):
        text(result["blocking"], "blocking")
        effect()
        state["final"] = result
        pause(state, result["blocking"])
        return
    text(result["summary"], "summary")
    texts(result.get("questions", []), "questions", False)
    if len(result.get("questions", [])) > 3:
        raise ValueError("ask the user at most three questions")
    wanted = {o["id"] for o in state["scorecard"] if o["kind"] == "judged"}
    judged = {}
    for entry in result.get("judged", []):
        if entry["id"] not in wanted or entry["state"] not in JUDGED:
            raise ValueError("judged entries need a judged scorecard id and state unmet, partial or met")
        text(entry["evidence"], "judged evidence")
        judged[entry["id"]] = {"state": entry["state"], "evidence": entry["evidence"]}
    if set(judged) != wanted:
        raise ValueError("judge every judged outcome: " + ", ".join(sorted(wanted)))
    committed = git("rev-parse", "HEAD") != state["base"]
    if committed:
        text(result["pr_title"], "pr_title")
        if "\n" in result["pr_title"]:
            raise ValueError("pr_title must be one line")
        text(result["pr_body"], "pr_body")
    effect()
    final = measure(state["scorecard"])
    state["readings"]["final"] = final
    met = {o["id"]: reached(o, judged, final) for o in state["scorecard"]}
    state.update(final={**result, "judged": judged, "met": met}, status="DONE", committed=committed,
                 dod_met=all(met.values()), finished=time.time(), request=None)


def report(state):
    """The short report each brief ends with; it is appended to the pull request."""
    lines = [f"## Brief report: {state['title']}", "", f"Status: {state['status']}"]
    final = state.get("final") or {}
    if state.get("scorecard"):
        lines += ["", "### Definition of done", "", "| Outcome | Result |", "| --- | --- |"]
        base = state["readings"].get("baseline", {}).get("values", {})
        last = state["readings"].get("final", {}).get("values", {})
        for o in state["scorecard"]:
            if o["kind"] == "measured":
                detail = f"{base.get(o['id'])} -> {last.get(o['id'])} (target {o['target']})"
            else:
                detail = final.get("judged", {}).get(o["id"], {}).get("evidence", "not judged")
            mark = "met" if final.get("met", {}).get(o["id"]) else "not met"
            lines.append(f"| {o['outcome']} | {mark}: {detail} |".replace("\n", " "))
    lines += ["", "### Review rounds", "", f"Rounds: {state['round']}."]
    for review in state["reviews"]:
        lines.append(f"- Round {review['round']}: {len(review['blocking'])} to fix. {review['summary']}")
    sections = [("Remaining important findings", [f"{f['finding']}" for f in state["remaining_high"]]),
                ("Dismissed findings", [f"{f['finding']} (why: {f['reason']})" for f in state["dismissed"]]),
                ("Minor findings", [f"{f['severity']}: {f['finding']}" for f in state["minor"]]),
                ("Suggestions not built (out of scope)", state["suggestions"]),
                ("Abandoned tasks", [f"{t['title']} ({t.get('kept_at', 'no commits')})"
                                     for t in state["tasks"] if t["status"] == "ABANDON"]),
                ("Questions for you", final.get("questions", []))]
    for title, items in sections:
        if items:
            lines += ["", f"### {title}", "", *[f"- {i}" for i in items]]
    if state["status"] == "PAUSED":
        lines += ["", "### Why it paused", "", state["reason"]]
    return "\n".join(lines) + "\n"


# ---------- stage protocol ----------

def transition(state, result, queue):
    stage = state["request"]["stage"]
    phase = state["request"]["phase"]
    if result.get("request_id") != state["request"]["id"]:
        raise ValueError("stale result: request_id does not match request.json")
    if guard(clean=False) != state["branch"]:
        raise RuntimeError("branch changed")
    if stage != "WORK" and dirty():
        raise ValueError("only workers change files: restore the worktree with git checkout and git clean")
    candidate = copy.deepcopy(state)
    if stage == "WORK":
        worked(candidate, result)
    elif stage == "REVIEW":
        reviewed(candidate, result, queue["max_rounds"])
    elif phase == "PLAN":
        plan(candidate, result)
    elif phase == "CHECK":
        checked(candidate, result, queue["max_turns"])
    elif phase == "TRIAGE":
        triaged(candidate, result)
    else:
        finished(candidate, result)
    return candidate


def check_result():
    """Let an agent validate its result before it ends its turn; nothing is changed."""
    global DRY
    state, queue = current(), load_queue()
    if not state["request"]:
        raise ValueError("no stage is waiting for a result")
    result = read_json(brief_dir(state["id"]) / "result.json")
    DRY = True
    try:
        transition(state, result, queue)
    except Checked:
        pass
    return f"OK: result.json is valid for this {state['request']['phase']} request"


def apply_result():
    state, queue = current(), load_queue()
    if not state["request"]:
        return "BRIEF_END" if state["status"] != "ACTIVE" else "CONTINUE"
    request = state["request"]
    try:
        result = read_json(brief_dir(state["id"]) / "result.json")
        state = transition(state, result, queue)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        detail = f"missing field {error}" if isinstance(error, KeyError) else error
        state["protocol_failures"] += 1
        state["reason"] = f"Invalid {request['phase']} result ({type(error).__name__}): {detail}"
        state["events"].append({"kind": "PROTOCOL_ERROR", "at": time.time(), "reason": state["reason"]})
        if state["protocol_failures"] > 2:
            pause(state, state["reason"] + "; recovery exhausted, work preserved.")
            return close(state, queue)
        save(state)
        return "RECOVER"
    except RuntimeError as error:
        pause(state, f"Git guard failed: {error}; work preserved.")
        return close(state, queue)
    state["events"].append({"kind": "RESULT", "phase": request["phase"], "at": time.time(),
                            "seconds": time.time() - request["started"]})
    state.update(request=None, protocol_failures=0)
    if state["status"] != "ACTIVE":
        return close(state, queue)
    save(state)
    return "CONTINUE"


def close(state, queue):
    """Write the report and record the brief's end in the queue."""
    state["request"] = None
    save(state)
    atomic(brief_dir(state["id"]) / "report.md", report(state))
    entry = next(b for b in queue["briefs"] if b["id"] == state["id"])
    entry.update(status=state["status"], committed=bool(state.get("committed")),
                 head=git("rev-parse", "HEAD"), dod_met=state.get("dod_met"))
    save_queue(queue)
    return "BRIEF_END"


def usage_ready(state, floors, weekly, buffer):
    env = {**os.environ, "GOAL_USAGE_FILE": str((brief_dir(state["id"]) / "usage.json").resolve())}
    usage_file = brief_dir(state["id"]) / "usage.json"
    usage_file.unlink(missing_ok=True)
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name("usage.py")), *map(str, floors), weekly],
                              env=env, capture_output=True, text=True, timeout=120)
        values = read_json(usage_file)["remaining"]
        ready = (proc.returncode == 0 and len(values) == 3 and
                 all(type(v) in (float, int) and v >= min(100, f + buffer) for v, f in zip(values, floors)))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        ready, values = False, []
    state["usage"] = {"at": time.time(), "remaining": values, "floors": floors, "ready": ready}
    return ready


def route(floors, poll, weekly, buffer):
    state, queue = current(), load_queue()
    if state["status"] != "ACTIVE":
        return close(state, queue)
    if time.time() >= queue["deadline"]:
        pause(state, "The safety deadline was reached; work is preserved on the branch.")
        return close(state, queue)
    if not usage_ready(state, floors, weekly, buffer):
        state["events"].append({"kind": "USAGE_PAUSE", "at": time.time()})
        save(state)
        time.sleep(min(poll, max(0, queue["deadline"] - time.time())))
        return "USAGE_PAUSED"
    try:
        # A pending request may be a recovery of a turn that left changes behind; apply judges those.
        if guard(clean=not state["request"]) != state["branch"]:
            raise RuntimeError("branch changed")
    except (RuntimeError, subprocess.CalledProcessError) as error:
        pause(state, f"Git guard failed: {error}; work preserved.")
        return close(state, queue)
    stage = {"WORK": "WORK", "REVIEW": "REVIEW"}.get(state["phase"], "MANAGE")
    if not state["request"]:
        state["request"] = {"id": uuid.uuid4().hex, "stage": stage, "phase": state["phase"],
                            "started": time.time(), "head": git("rev-parse", "HEAD")}
    request = {**state["request"], "brief_path": state["brief_path"], "base": state["base"],
               "round": state["round"], "max_rounds": queue["max_rounds"],
               "recovery": state["reason"] if state["protocol_failures"] else "",
               "result_path": str(brief_dir(state["id"]) / "result.json"),
               "state_path": str(brief_dir(state["id"]) / "state.json")}
    if state["phase"] == "WORK":
        item = task(state)
        fix = state["fixes"][0] if state["fixes"] else None
        request["task"] = {**{k: item[k] for k in ("id", "title", "scope", "outcomes")},
                           "base": item["base"], "turn": item["turns"] + 1,
                           "kind": "fix" if fix else ("build" if not item["turns"] else "continue"),
                           "direction": fix["direction"] if fix else item.get("direction", ""),
                           "findings": fix["findings"] if fix else []}
    if state["phase"] == "CHECK":
        request["task"] = task(state)
    if state["phase"] == "TRIAGE":
        request["findings"] = state["reviews"][-1]["blocking"]
        request["tasks"] = [{k: t[k] for k in ("id", "title", "scope")} for t in state["tasks"] if t["status"] == "ACCEPT"]
    if state["phase"] == "FINISH":
        request["blocked"] = bool(state.get("blocked"))
        request["pr_base"] = next(b for b in queue["briefs"] if b["id"] == state["id"])["pr_base"] or "default branch"
    write_json(brief_dir(state["id"]) / "request.json", request)
    save(state)
    return stage


def session():
    """One worker conversation per task, so a revision or a fix continues the build that made it."""
    state = current()
    return f"task-{state['id']}-{state['active']}"


# ---------- pull requests and summary ----------

def gh(*args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def publish():
    """Push a finished brief and open (or update) its pull request; prints NEXT or QUEUE_END."""
    queue = load_queue()
    entry = next((b for b in reversed(queue["briefs"]) if b["status"] in {"DONE", "PAUSED"}
                  and not b.get("published")), None)
    if entry is None:
        return "QUEUE_END"
    state = read_json(brief_dir(entry["id"]) / "state.json")
    entry["published"] = True
    note = publish_pr(queue, entry, state)
    entry["pr"] = note
    save_queue(queue)
    print(f"{entry['id']}: {note}", file=sys.stderr)
    return "QUEUE_END" if entry["status"] == "PAUSED" or time.time() >= queue["deadline"] else "NEXT"


def publish_pr(queue, entry, state):
    if entry["status"] != "DONE":
        return "paused: no pull request"
    if not state.get("committed"):
        return "no changes: no pull request"
    if not queue["pull_request"]:
        return "pull_request is false"
    if "origin" not in git("remote").split():
        return "no origin remote: branch kept locally"
    try:
        if not shutil.which("gh"):
            raise RuntimeError("gh is not installed")
        gh("auth", "status")
        base = entry["pr_base"] or gh("repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
        body = (state["final"]["pr_body"].strip() + "\n\n" + report(state)).strip()[:PR_BODY_LIMIT]
        body_file = brief_dir(state["id"]) / "pr-body.md"
        atomic(body_file, body)
        # Never force: if someone else moved the remote branch, the push fails and nothing is lost.
        git("push", "-q", "origin", f"{entry['head']}:refs/heads/{entry['branch']}")
        url = gh("pr", "list", "--head", entry["branch"], "--state", "open", "--json", "url", "--jq", ".[0].url")
        if url:
            gh("pr", "edit", url, "--title", state["final"]["pr_title"], "--body-file", str(body_file))
        else:
            url = gh("pr", "create", "--base", base, "--head", entry["branch"], "--title", state["final"]["pr_title"],
                     "--body-file", str(body_file), *([] if state["dod_met"] else ["--draft"])).splitlines()[-1]
        return url
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return f"pull request skipped: {error}"


def summary():
    queue = load_queue()
    lines = ["# Managing managers run", "", "| Brief | Status | Done | Pull request |", "| --- | --- | --- | --- |"]
    for b in queue["briefs"]:
        done = {True: "met", False: "not met"}.get(b.get("dod_met"), "-")
        lines.append(f"| {b['id']} | {b['status']} | {done} | {b.get('pr', '-')} |")
    paused = [b["id"] for b in queue["briefs"] if b["status"] == "PAUSED"]
    if paused:
        lines += ["", f"The queue stopped at {paused[0]}. Read .atelier/mm/briefs/{paused[0]}/report.md."]
    text_ = "\n".join(lines) + "\n"
    atomic(ROOT / "summary.md", text_)
    return text_


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("hours", type=float)
    p.add_argument("max_rounds", type=int)
    p.add_argument("max_turns", type=int)
    p.add_argument("pull_request")
    p.add_argument("base", nargs="?", default="")
    p = sub.add_parser("route")
    p.add_argument("floors", type=int, nargs=3)
    p.add_argument("poll", type=int)
    p.add_argument("weekly")
    p.add_argument("buffer", type=int)
    for name in ("start", "apply", "check", "session", "publish", "summary"):
        sub.add_parser(name)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init(args.hours, args.max_rounds, args.max_turns, args.pull_request, args.base)
        elif args.command == "route":
            if not all(1 <= n <= 100 for n in args.floors) or not 1 <= args.poll <= 3600 or not 0 <= args.buffer <= 25:
                raise ValueError("floors must be 1-100, poll 1-3600 and buffer 0-25")
            if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", args.weekly):
                raise ValueError("invalid Claude weekly meter name")
            result = route(args.floors, args.poll, args.weekly, args.buffer)
        elif args.command == "check":
            try:
                result = check_result()
            except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
                print(f"INVALID ({type(error).__name__}): {f'missing field {error}' if isinstance(error, KeyError) else error}")
                return 1
        else:
            result = {"start": start, "apply": apply_result, "session": session,
                      "publish": publish, "summary": summary}[args.command]()
        print(result, end="")
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"managing-managers: {error}; work preserved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
