"""PR monitoring (DESIGN.md §5.11, D-129 to D-134): an optional module, off by default.

It reads a PR's branch, read-only, and compares it with the PR's spec. Code inside the
spec's own files is the builder's business; code only flags what other PRs rely on
(contracts, files, dependencies, migrations, rules). A model judges the flagged signals.
A change that stays inside its own PR updates that spec automatically; anything reaching
other PRs, or something the designer approved, waits for the designer. Every finding is a
change history entry (``CH-n``).
"""

from __future__ import annotations

import copy
import difflib
import re
import subprocess
from pathlib import Path, PurePosixPath

from gandalf import checks, secrets
from gandalf.schema import check
from gandalf.scan.detectors import DEPENDENCY_MANIFESTS, LOCKFILES, MIGRATION_DIR, REQUIREMENTS_FILE
from gandalf.schemas import MONITOR, _normalise
from gandalf.state import PlanState, _read, _write, now

# Tests are the builder's business: changed test files are never flagged.
TEST_FILE = re.compile(r"(^|/)(tests?|spec|specs|__tests__|e2e|fixtures)/|(_test|_spec|\.test|\.spec)\.\w+$"
                       r"|(^|/)test_[^/]+$")
OPEN = ("judging", "proposed")


class MonitorError(ValueError):
    """Something the person running the check must fix (shown plainly, not as a crash)."""


# --- reading the branch (read-only git) ------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise MonitorError(f"git {' '.join(args)}: {proc.stderr.strip() or 'failed'}")
    return proc.stdout


def default_base(repo: Path) -> str:
    """The branch PRs merge into: origin's default branch, else main, else master."""
    try:
        return _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except MonitorError:
        pass
    for name in ("main", "master"):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
            return name
        except MonitorError:
            continue
    raise MonitorError("couldn't tell which branch PRs merge into: pass --base")


def read_branch(repo: Path, branch: str, base: str) -> dict:
    """What the branch changes since it left ``base``: paths by status, added lines, tip commit."""
    try:
        tip = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    except MonitorError:
        raise MonitorError(f"no branch {branch!r} in {repo.name}: GANDALF never fetches, so it must be "
                           "present locally") from None
    start = _git(repo, "merge-base", base, tip).strip()
    status: dict[str, str] = {}
    for line in _git(repo, "diff", "--name-status", "--no-renames", start, tip).splitlines():
        code, _, path = line.partition("\t")
        status[path] = code[0]
    added: dict[str, list[str]] = {}
    current = None
    for line in _git(repo, "diff", "--unified=0", "--no-renames", start, tip).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, []).append(line[1:])
    return {"tip": tip, "start": start, "status": status, "added": added}


def file_at(repo: Path, rev: str, path: str) -> str:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except MonitorError:
        return ""


def _squash(text: str) -> str:
    return " ".join(text.split())


def contract_lines(body: str) -> list[str]:
    """The lines a contract's code must contain: comments and docstrings dropped, since a
    builder writes bodies between signatures and needn't copy location notes."""
    body = re.sub(r'(?s)("""|\'\'\').*?\1', "", body)      # Python docstrings
    body = re.sub(r"(?s)/\*.*?\*/", "", body)                # block comments
    lines = [re.sub(r"\s*(\.\.\.|\{\s*\.\.\.\s*\})\s*;?$", "", _squash(line)) for line in body.splitlines()]
    return [line for line in lines if line and not line.startswith(("#", "//"))   # `: ...` is "body here"
            and not re.match(r"(import\s|from\s+[\w.]+\s+import\s)", line)]       # imports are the repo's (D-139)


# --- signals (code) ---------------------------------------------------------------------------

def _owned(path: str, pr: dict) -> bool:
    """Is ``path`` one of this PR's planned changes (a ``generate`` change owns its folder)?"""
    for ch in pr["changes"]:
        planned = checks.cite_path(ch["path"])
        if path == planned or (ch["action"] == "generate" and path.startswith(planned.rstrip("/") + "/")):
            return True
    return False


def _later(prs: list[dict], pid: str) -> set[str]:
    """PRs that build on ``pid``, directly or through others."""
    closure = checks.transitive_deps(prs)
    return {other for other, deps in closure.items() if pid in deps}


def find_signals(plan: PlanState, pid: str, branch: dict, repo: Path, done: bool, merged: set[str]) -> list[dict]:
    """Everything on the branch that other PRs rely on, each with what it reaches (D-132)."""
    sequence, design = plan.stage("sequence"), plan.stage("design")
    prs = {p["id"]: p for p in sequence["prs"]}
    pr = prs[pid]
    unbuilt = {p for p in prs if p not in merged and p != pid}
    later = _later(sequence["prs"], pid)
    users = {cid: [p["id"] for p in sequence["prs"] if cid in p["contracts_used"]] for cid in pr["contracts_exposed"]}
    status = branch["status"]
    signals: list[dict] = []

    def add(kind: str, what: str, *, paths=(), reaches=(), approved=(), rule=False, missing=False):
        # missing: planned work the branch doesn't have, which may be judged "unfinished" (D-136)
        signals.append({"id": f"M-{len(signals) + 1}", "kind": kind, "what": what, "paths": sorted(paths),
                        "reaches": sorted(set(reaches) & unbuilt), "approved": list(approved), "rule": rule,
                        "missing": missing})

    # rules first: never acceptable, whatever the reason (§8.6, §9.2)
    for path, code in sorted(status.items()):
        if code == "D":
            continue
        kind = checks.iac_kind(path, plan.scan)
        if kind:
            add("rule", f"{path} is infrastructure ({kind}), which GANDALF never lets a PR change",
                paths=[path], rule=True)
        found = secrets.find("\n".join(branch["added"].get(path, [])))
        if found:
            add("rule", f"{path} adds what looks like a real secret ({', '.join(dict.fromkeys(found))})",
                paths=[path], rule=True)
    ruled = {p for s in signals for p in s["paths"]}

    # contracts this PR exposes
    tip_text = {path: file_at(repo, branch["tip"], path) for path, code in status.items()
                if code != "D" and not TEST_FILE.search(path)}
    squashed = {path: _squash(text) for path, text in tip_text.items()}
    for c in design["contracts"]:
        if c["id"] not in pr["contracts_exposed"]:
            continue
        wanted = contract_lines(c["body"])
        homes = {checks.cite_path(f) for f in c.get("files") or []}
        own = {p: t for p, t in squashed.items() if not homes or p in homes}   # its own files (D-138)
        everything = " ".join(own.values())
        found = [line for line in wanted if line in everything]
        if wanted and len(found) == len(wanted):
            continue
        holders = [path for path, text in own.items() if any(line in text for line in found)]
        if found:
            add("contract", f"{c['id']} ({c['name']}) is in the code in a different form from the plan",
                paths=holders, reaches=users[c["id"]], approved=[f"contract {c['id']}"])
        elif done:
            # code can't find it as written (a route described in prose, say): the model checks the
            # diff of where it should be, never judging blind
            where = sorted(homes & set(squashed)) or sorted(p for p in squashed if _owned(p, pr))
            add("contract", f"{c['id']} ({c['name']}) wasn't found as written, but the PR is done",
                paths=where, reaches=users[c["id"]], approved=[f"contract {c['id']}"], missing=True)

    # dependencies
    deps = [path for path in status if PurePosixPath(path).name in LOCKFILES or REQUIREMENTS_FILE.search(path)
            or (PurePosixPath(path).name in DEPENDENCY_MANIFESTS and not _owned(path, pr))]
    if deps and not pr.get("new_dependency"):
        add("dependency", "dependency files changed, but the PR plans no new dependency",
            paths=deps, approved=["new dependency"])

    # migrations
    new_migrations = [path for path, code in status.items() if code == "A" and MIGRATION_DIR.search(path)]
    if new_migrations and not pr.get("schema_change"):
        add("migration", "a migration was added, but the PR plans no data change",
            paths=new_migrations, reaches=later, approved=["data change"])
    elif done and pr.get("schema_change") and not new_migrations:
        add("migration", "the PR plans a data change, but no migration was added",
            reaches=later, approved=["data change"], missing=True)

    # files outside the spec
    claimed = {"contract", "dependency", "migration"}
    seen = ruled | {p for s in signals if s["kind"] in claimed for p in s["paths"]}
    for path, code in sorted(status.items()):
        if path in seen or TEST_FILE.search(path) or _owned(path, pr):
            continue
        owners = [o for o in prs if o != pid and _owned(path, prs[o])]
        if code == "D":
            add("files", f"{path} was deleted, which the spec doesn't plan", paths=[path], reaches=owners)
        else:
            add("files", f"{path} changed, but isn't in the spec"
                + (f"; {', '.join(owners)} plans to change it" if owners else ""), paths=[path], reaches=owners)

    # spec files not built
    if done:
        migration_flagged = any(sig["kind"] == "migration" for sig in signals)
        for ch in pr["changes"]:
            planned = checks.cite_path(ch["path"])
            # tests are the builder's business; a missing migration is already one signal
            if TEST_FILE.search(planned) or (migration_flagged and MIGRATION_DIR.search(planned + "/")):
                continue
            if ch["action"] == "generate":
                built = any(p.startswith(planned.rstrip("/") + "/") for p in status)
            elif ch["action"] == "delete":
                built = status.get(planned) == "D"
            else:
                built = planned in status
            if not built:
                needs = [o for o in later if any(checks.cite_path(c["path"]) == planned for c in prs[o]["changes"])]
                add("files", f"{planned} was planned ({ch['action']}) but the branch doesn't change it",
                    paths=[planned], reaches=needs, missing=True)
    return signals


# --- the change history -----------------------------------------------------------------------

def _path(plan: PlanState) -> Path:
    return plan.dir / "changes.json"


def history(plan: PlanState) -> list[dict]:
    return _read(_path(plan), [])


def _save(plan: PlanState, entries: list[dict]) -> None:
    _write(_path(plan), entries)


def entry(plan: PlanState, cid: str) -> dict:
    found = next((e for e in history(plan) if e["id"] == cid), None)
    if found is None:
        raise MonitorError(f"no change {cid} in this plan")
    return found


def _update(plan: PlanState, changed: dict) -> None:
    _save(plan, [changed if e["id"] == changed["id"] else e for e in history(plan)])


def enabled(plan: PlanState) -> bool:
    return bool(plan.meta.get("monitoring"))


def switch(plan: PlanState, on: bool) -> str:
    plan.meta["monitoring"] = on
    plan.save()
    return "PR monitoring is on for this plan." if on else "PR monitoring is off for this plan."


def _ready(plan: PlanState) -> None:
    if not enabled(plan):
        raise MonitorError("PR monitoring is off for this plan: turn it on with `gandalf monitor on`")
    if plan.next_action()["action"] != "render":
        raise MonitorError("the plan isn't finished yet, so there's nothing to monitor against")


def check_branch(plan: PlanState, pid: str, branch_name: str, base: str | None, done: bool,
                 merged: list[str]) -> dict | None:
    """Read the branch and record what it flags. None when nothing is flagged (logged only)."""
    _ready(plan)
    prs = {p["id"]: p for p in plan.stage("sequence")["prs"]}
    if pid not in prs:
        raise MonitorError(f"{pid} isn't in the plan")
    frozen = {p["id"] for p in plan.meta.get("frozen", [])} | set(merged)
    unknown = [m for m in merged if m not in prs]
    if unknown:
        raise MonitorError(f"unknown merged PR(s): {', '.join(unknown)}")
    if pid in frozen:
        raise MonitorError(f"{pid} has merged, and merged PRs never change")
    waiting = [e for e in history(plan) if e["pr"] == pid and e["status"] == "proposed"]
    if waiting:
        raise MonitorError(f"{waiting[0]['id']} on {pid} waits for the designer: approve or reject it first")
    repo = Path(plan.meta["repo"]["path"])
    base = base or default_base(repo)
    branch = read_branch(repo, branch_name, base)
    signals = find_signals(plan, pid, branch, repo, done, frozen)
    plan.log("monitor", pr=pid, branch=branch_name, commit=branch["tip"], signals=len(signals))
    plan.save()
    if not signals:
        return None
    entries = history(plan)
    for e in entries:                       # a newer check replaces one still being judged
        if e["pr"] == pid and e["status"] == "judging":
            e["status"] = "replaced"
    new = {"id": f"CH-{len(entries) + 1}", "at": now(), "pr": pid, "branch": branch_name, "base": base,
           "commit": branch["tip"], "start": branch["start"], "done": done, "merged": sorted(frozen),
           "signals": signals, "status": "judging"}
    rules = [s for s in signals if s["rule"]]
    if rules and len(rules) == len(signals):
        new.update(status="revert", summary="; ".join(s["what"] for s in rules), decided_by="rules")
    _save(plan, entries + [new])
    if new["status"] == "revert":
        write_builder_note(plan, new)
    return new


# --- judging (a model, flagged signals only) ---------------------------------------------------

def judged(e: dict) -> list[dict]:
    """The signals a model judges: everything but rule breaks."""
    return [s for s in e["signals"] if not s["rule"]]


def diff_excerpt(plan: PlanState, e: dict, limit: int = 40_000) -> str:
    paths = sorted({p for s in judged(e) for p in s["paths"]})
    if not paths:
        return ""
    repo = Path(plan.meta["repo"]["path"])
    text = _git(repo, "diff", "--no-renames", e["start"], e["commit"], "--", *paths)
    return text if len(text) <= limit else text[:limit] + f"\n… (cut at {limit} characters)"


def validate_reply(plan: PlanState, e: dict, data: dict) -> tuple[dict, list[str]]:
    """Check a model's judgement and the specs it proposes, with every usual plan check."""
    data, _ = _normalise(data, MONITOR)
    errors = check(data, MONITOR)
    if errors:
        return data, errors
    asked = {s["id"] for s in judged(e)}
    given = [s["id"] for s in data["signals"]]
    for sid in sorted(asked - set(given)):
        errors.append(f"monitor: judge {sid} too (every flagged signal, once)")
    for sid in sorted(set(given) - asked):
        errors.append(f"monitor: {sid} isn't one of the flagged signals")
    real = [s for s in data["signals"] if s["verdict"] == "change"]
    flagged = {sig["id"]: sig for sig in judged(e)}
    for sig in data["signals"]:
        if sig["verdict"] == "unfinished" and not flagged.get(sig["id"], {}).get("missing"):
            errors.append(f"monitor: {sig['id']} is there in the code, so it's a change or harmless, not unfinished")
    design, sequence = plan.stage("design"), plan.stage("sequence")
    decisions = {d["id"] for d in design["decisions"]}
    for did in data["contradicts"]:
        if did not in decisions:
            errors.append(f"monitor: contradicts {did}, which the design doesn't have")
    if data["contradicts"] and not data.get("design_change"):
        errors.append("monitor: it contradicts a design decision, so say what the design must change (design_change)")
    if not real and (data["prs"] or data["contracts"] or data["contradicts"]):
        errors.append("monitor: no signal is a change, so leave prs, contracts and contradicts empty")
    if real and not data["summary"].strip():
        errors.append("monitor: say in one plain line what changed (summary)")
    if real and not data["contradicts"] and not data["prs"] and not data["contracts"]:
        errors.append(f"monitor: a real change updates the specs: give {e['pr']} as it should now read (prs)")
    known = {c["id"]: c for c in design["contracts"]}
    for c in data["contracts"]:
        if c["id"] not in known:
            errors.append(f"monitor: {c['id']} isn't a contract in this plan; new contracts need the design")
    # an accepted dependency or migration is written down where the plan tracks it
    kinds = {sig["id"]: sig["kind"] for sig in judged(e)}
    own = next((p for p in data["prs"] if p["id"] == e["pr"]), None)
    for sig in real:
        if kinds.get(sig["id"]) == "dependency" and not (own or {}).get("new_dependency"):
            errors.append(f"monitor: {sig['id']} accepts a new dependency, so give {e['pr']} with it named in "
                          "new_dependency")
        if kinds.get(sig["id"]) == "migration" and not (own or {}).get("schema_change"):
            errors.append(f"monitor: {sig['id']} accepts a data change, so give {e['pr']} with what changes "
                          "in schema_change")
    frozen = set(e["merged"])
    current = {p["id"]: p for p in sequence["prs"]}
    for p in data["prs"]:
        if p["id"] not in current:
            errors.append(f"monitor: {p['id']} isn't in the plan; new PRs need a re-plan")
        elif p["id"] in frozen:
            errors.append(f"monitor: {p['id']} has merged and never changes")
    if errors or not real or data["contradicts"]:
        return data, errors
    new_design, new_sequence = merged_plan(plan, data)
    repo = checks.RepoView.for_plan(plan.meta["repo"])
    report, _ = checks.check_plan(intent=plan.stage("intent"), design=new_design, decompose=plan.stage("decompose"),
                                  sequence=new_sequence, scan=plan.scan, context=plan.stage("context"),
                                  repo=repo, security=plan.stage("security"), frozen=frozen)
    report.extend(checks.check_security(plan.stage("security"), new_design))
    errors += report.errors
    return data, errors


def merged_plan(plan: PlanState, data: dict) -> tuple[dict, dict]:
    """The design and sequence with the judged updates folded in."""
    design, sequence = copy.deepcopy(plan.stage("design")), copy.deepcopy(plan.stage("sequence"))
    contracts = {c["id"]: c for c in data["contracts"]}
    design["contracts"] = [contracts.get(c["id"], c) for c in design["contracts"]]
    prs = {p["id"]: p for p in data["prs"]}
    sequence["prs"] = [prs.get(p["id"], p) for p in sequence["prs"]]
    return design, sequence


def reach(plan: PlanState, e: dict, data: dict) -> dict:
    """Who and what a judged change reaches: other PRs, and anything the designer approved."""
    real = {s["id"] for s in data["signals"] if s["verdict"] == "change"}
    signals = [s for s in judged(e) if s["id"] in real]
    prs = {p for s in signals for p in s["reaches"]} | {p["id"] for p in data["prs"] if p["id"] != e["pr"]}
    current = {c["id"]: c for c in plan.stage("design")["contracts"]}
    approved = [a for s in signals for a in s["approved"]]
    approved += [f"contract {c['id']}" for c in data["contracts"] if c != current.get(c["id"])]
    approved += [f"decision {d}" for d in data["contradicts"]]
    return {"prs": sorted(prs), "approved": sorted(set(approved))}


def submit_judgement(plan: PlanState, cid: str, data: dict) -> tuple[dict, list[str]]:
    """Record a model's judgement. Contained changes apply at once; others become proposals."""
    e = entry(plan, cid)
    if e["status"] != "judging":
        raise MonitorError(f"{cid} is {e['status']}, not waiting for a judgement")
    data, errors = validate_reply(plan, e, data)
    if errors:
        plan.record_rejection("monitor", "cli", errors, data)
        return e, errors
    plan.record_acceptance("monitor", "cli", data=data)
    real = [s for s in data["signals"] if s["verdict"] == "change"]
    unfinished = [s for s in data["signals"] if s["verdict"] == "unfinished"]
    rules = [s for s in e["signals"] if s["rule"]]
    flagged = "; ".join(s["what"] for s in judged(e))
    e.update(judgement=data, summary=data["summary"] or flagged, reach=reach(plan, e, data))
    if rules:
        e.update(status="revert", decided_by="rules",
                 summary="; ".join(s["what"] for s in rules) + (f". Also: {data['summary']}" if real else ""))
        write_builder_note(plan, e)
    elif not real and unfinished:
        # missing work: the builder hears what's left, the plan stays, nobody waits (D-136)
        e.update(status="unfinished", decided_by="model",
                 summary=data["summary"] or f"{e['pr']} isn't finished: {len(unfinished)} planned item(s) missing")
        write_builder_note(plan, e)
    elif not real:
        e.update(status="harmless", decided_by="model")
    elif e["reach"]["prs"] or e["reach"]["approved"]:
        e.update(status="proposed")
    else:
        e.update(status="absorbed", decided_by="automatic")
        _apply(plan, e)
    if unfinished and e["status"] in ("proposed", "absorbed"):
        write_builder_note(plan, e)            # what's still missing, whatever the designer decides
    e["judged_at"] = now()
    _update(plan, e)
    return e, []


# --- deciding and applying ------------------------------------------------------------------

def _apply(plan: PlanState, e: dict) -> None:
    """Fold an accepted change into the plan; merged PRs stay frozen (§5.10)."""
    data = e["judgement"]
    if data["contradicts"]:
        # a decision changes: the design goes back to the designer, as for a security design change
        plan.answers["guidance"].append({"stage": "design", "from": f"change {e['id']} on {e['pr']}",
                                         "text": data["design_change"], "at": now()})
        _freeze(plan, e["merged"])
        plan.invalidate_from("design", f"change {e['id']} on {e['pr']} contradicts "
                             + ", ".join(data["contradicts"]))
        e["applied"] = {"replanned_from": "design"}
        plan.save()
        return
    before_design, before_sequence = plan.stage("design"), plan.stage("sequence")
    design, sequence = merged_plan(plan, data)
    # an accepted dependency joins the inventory, so the security review asks its questions
    kinds = {sig["id"]: sig["kind"] for sig in judged(e)}
    if any(kinds.get(sig["id"]) == "dependency" and sig["verdict"] == "change" for sig in data["signals"]):
        own = next(p for p in sequence["prs"] if p["id"] == e["pr"])
        ids = [int(n["id"][2:]) for n in design["inventory"]]
        design["inventory"].append({"id": f"N-{max(ids, default=0) + 1}", "kind": "dependency",
                                    "description": f"{own['new_dependency']} (accepted while building "
                                                   f"{e['pr']}, {e['id']})"})
    reason = f"change {e['id']} on {e['pr']}"
    if design != before_design:
        plan.replace_stage("design", design, reason)
    plan.replace_stage("sequence", sequence, reason)
    if design != before_design:
        # the design changed: the independent review looks again, and the sequence places
        # its fixes again while merged PRs stay as they are (§5.11, D-149)
        plan.request_review(f"{reason} changed the design")
    changed = [p["id"] for p in data["prs"] if p != next(q for q in before_sequence["prs"] if q["id"] == p["id"])]
    e["applied"] = {"prs": changed, "contracts": [c["id"] for c in data["contracts"]
                                                  if c not in before_design["contracts"]],
                    "security_rerun": plan.status("security") != "done"}
    plan.save()


def _freeze(plan: PlanState, merged: list[str]) -> None:
    prs = {p["id"]: p for p in plan.stage("sequence")["prs"]}
    frozen = {p["id"]: p for p in plan.meta.get("frozen", [])}
    for m in merged:
        if m in prs:
            frozen.setdefault(m, prs[m])
    plan.meta["frozen"] = [frozen[k] for k in sorted(frozen)]
    exposed = {c for p in frozen.values() for c in p["contracts_exposed"]}
    plan.meta["frozen_contracts"] = [c for c in plan.stage("design")["contracts"] if c["id"] in exposed]


def decide(plan: PlanState, cid: str, approve: bool, notes: str = "") -> dict:
    e = entry(plan, cid)
    if e["status"] != "proposed":
        raise MonitorError(f"{cid} is {e['status']}; only a proposed change waits for a decision")
    e.update(decided_at=now(), decided_by="designer", notes=notes)
    if approve:
        e["status"] = "approved"
        _apply(plan, e)
    else:
        e["status"] = "rejected"
        write_builder_note(plan, e)
    _update(plan, e)
    plan.log("monitor-decision", change=cid, approved=approve)
    plan.save()
    return e


def note_file(e: dict) -> str:
    return f"monitor/{e['id'].lower()}-for-builder.md"


def write_builder_note(plan: PlanState, e: dict) -> Path:
    """What the builder brings back to the spec, after a rejection or a broken rule."""
    rules = [s for s in e["signals"] if s["rule"]]
    verdicts = {s["id"]: s for s in (e.get("judgement") or {}).get("signals", [])}

    def items(sigs):
        out = []
        for s in sigs:
            detail = verdicts.get(s["id"], {}).get("what")
            out.append(f"- {s['what']}" + (f" ({detail})" if detail and detail != s["what"] else ""))
        return out

    lines = [f"# {e['id']}: {e['pr']} on branch `{e['branch']}` at `{e['commit'][:12]}`", ""]
    if e["status"] == "revert":
        lines += ["These break GANDALF's rules and can't be accepted; undo them:", ""] + items(rules) + [""]
    elif e["status"] == "rejected":
        changed = [s for s in judged(e) if verdicts.get(s["id"], {}).get("verdict") == "change"]
        lines += ["The designer kept the plan as it was, so undo these:", ""] + items(changed) + [""]
    elif e["status"] == "proposed":
        lines += ["The designer is deciding on the changes this branch makes. Meanwhile:", ""]
    left = [s for s in judged(e) if verdicts.get(s["id"], {}).get("verdict") == "unfinished"]
    if left:
        lines += ["Not finished yet: the spec plans these and the branch doesn't have them:", ""] + items(left) + [""]
    lines = lines[:-1] if lines[-1] == "" else lines
    if e.get("notes"):
        lines += ["", f"The designer's notes: {e['notes']}"]
    lines += ["", f"Then follow the PR spec as written. Check again with "
              f"`gandalf monitor check {e['pr']} --branch {e['branch']}`.", ""]
    path = plan.plan_dir / note_file(e)
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines))
    e["note"] = note_file(e)
    return path


def proposal_lines(plan: PlanState, e: dict) -> list[str]:
    """What the designer sees before deciding."""
    data = e["judgement"]
    out = [f"{e['id']} on {e['pr']} (branch {e['branch']}): {e['summary']}"]
    for s in data["signals"]:
        out.append(f"  {s['id']} [{s['verdict']}] {s['what']}")
    if e["reach"]["prs"]:
        out.append(f"  reaches: {', '.join(e['reach']['prs'])}")
    if e["reach"]["approved"]:
        out.append(f"  touches what you approved: {', '.join(e['reach']['approved'])}")
    if data["contradicts"]:
        out.append(f"  goes against {', '.join(data['contradicts'])}; approving sends the design back: "
                   f"{data['design_change']}")
    current = {p["id"]: p for p in plan.stage("sequence")["prs"]}
    for p in data["prs"]:
        old = current[p["id"]]
        added = sorted({c["path"] for c in p["changes"]} - {c["path"] for c in old["changes"]})
        dropped = sorted({c["path"] for c in old["changes"]} - {c["path"] for c in p["changes"]})
        bits = ([f"adds {', '.join(added)}"] if added else []) + ([f"drops {', '.join(dropped)}"] if dropped else [])
        fields = [k for k in p if k != "changes" and p.get(k) != old.get(k)]
        fields += [f"changes to {c['path']}" for c in p["changes"]
                   if c["path"] not in added and c != next((o for o in old["changes"] if o["path"] == c["path"]), c)]
        bits += [f"rewrites {', '.join(fields)}"] if fields else []
        out.append(f"  {p['id']} spec would change" + (f": {'; '.join(bits)}" if bits else " (no visible difference)"))
    planned = {c["id"]: c for c in plan.stage("design")["contracts"]}
    for c in data["contracts"]:
        old_lines = planned[c["id"]]["body"].splitlines() if c["id"] in planned else []
        diff = [line for line in difflib.unified_diff(old_lines, c["body"].splitlines(), lineterm="", n=0)
                if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        out.append(f"  contract {c['id']} would change (- planned, + now):")
        out += ["      " + line for line in diff] or ["      (no change to its text)"]   # only what changes
    return out
