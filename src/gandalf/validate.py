"""Stage-level validation: structure, references and grounding (DESIGN.md §5, §11.2).

``validate_stage`` is what ``gandalf submit`` runs. A stage output is stored only if it
returns no errors.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from gandalf import checks, secrets
from gandalf.schema import check
from gandalf.schemas import CLIENT_FIELDS, INFRA_FIELDS, STAGE_SCHEMAS
from gandalf.state import STAGES, PlanState

# an import line in Python, JavaScript/TypeScript or Go: not part of a contract's surface (D-139)
IMPORT_LINE = re.compile(r"""\s*(import\s+[\w{*(\"']|from\s+[\w.]+\s+import\s)""")


MORE_CHECKS = "More checks run once these are fixed."


def _dupes(ids: list[str]) -> list[str]:
    return sorted(i for i, n in Counter(ids).items() if n > 1)


def _open_items(plan: PlanState, stage: str, data: dict, report: checks.Report) -> None:
    for i, pause in enumerate(data.get("pauses") or []):
        where = f"pauses[{i}]"
        if pause["kind"] == "restructuring":
            if pause.get("proposal"):
                report.errors.append(f"{where}: never propose restructuring; frame the options instead (§6.4)")
            if not pause["options"]:
                report.errors.append(f"{where}: a restructuring pause lists the options visible (§6.4)")
            if not pause["where"]:
                report.errors.append(f"{where}: a restructuring pause cites where in the code (§6.4)")
        elif not pause.get("proposal"):
            report.errors.append(f"{where}: state what is proposed and why (§6.2)")
        if not pause["why"]:
            report.errors.append(f"{where}: give evidence or a grounded opinion (§5.3)")
    unknown = plan.unknown_questions(stage)
    for i, p in enumerate(data.get("proposals") or []):
        if p["question_id"] not in unknown:
            report.errors.append(
                f"proposals[{i}]: {p['question_id']} is not a question from {stage} the designer marked unknown")
    for i, q in enumerate(data.get("questions") or []):
        if q.get("reask_of") and q["reask_of"] not in plan.answers["questions"]:
            report.errors.append(f"questions[{i}]: reask_of names unknown {q['reask_of']}")
        rec = q.get("recommendation")
        if rec:
            names = [o["option"] for o in q.get("options") or []]
            if rec["option"] not in names:
                report.errors.append(f"questions[{i}].recommendation: {rec['option']!r} isn't one of the options "
                                     f"({names or 'none given'})")
    missing = set(unknown) - {p["question_id"] for p in data.get("proposals") or []}
    if missing:
        report.errors.append(
            f"the designer didn't know {', '.join(sorted(missing))}: research and add a proposal for each")


def _cites_exist(repo: checks.RepoView, cites: list[tuple[str, str]], report: checks.Report) -> None:
    for where, cite in cites:
        if not repo.exists(cite):
            report.errors.append(f"{where}: cites {cite}, which isn't in the repo")


def _assumptions(plan: PlanState, data: dict, report: checks.Report) -> None:
    """An assumption citing a question must cite one the designer delegated (D-96)."""
    delegated = {qid for qid, q in plan.answers["questions"].items() if q["status"] == "delegated"}
    for i, a in enumerate(data.get("assumptions") or []):
        if a.get("ref") and a["ref"] not in delegated:
            report.errors.append(f"assumptions[{i}]: {a['ref']} isn't a question the designer delegated; "
                                 "leave out ref for a detail you decided without asking, including one an "
                                 "answer left to you")


def _require(plan: PlanState, *stages: str) -> list[str]:
    return [f"{s} must be completed first" for s in stages if plan.status(s) != "done"]


def _ref_status(plan: PlanState, ref: str) -> str:
    """Why a decision's source ref can't be used, in plain words (first 0.6 eval, E-1)."""
    q = plan.answers["questions"].get(ref)
    if q is not None and q["status"] == "delegated":
        return (f"{ref} was delegated (\"decide it yourself\"), which isn't an answer: record the choice "
                f"under assumptions with ref {ref}, or source the decision from the repo pattern it follows")
    if q is not None:
        return f"{ref} is {q['status']}, not answered"
    if ref in plan.answers["pauses"]:
        return f"{ref} is still open"
    return f"there is no question or pause {ref}"


def validate_stage(plan: PlanState, stage: str, data: dict) -> checks.Report:
    report = checks.Report()
    if stage not in STAGE_SCHEMAS:
        report.errors.append(f"unknown stage {stage!r}")
        return report
    schema_errors = check(data, STAGE_SCHEMAS[stage])
    report.errors += schema_errors
    if stage == "answers" or not isinstance(data, dict):
        return report
    # run the plan rules even when a few fields are malformed, so every problem comes back
    # in one pass (D-105); if the output is too broken to check, say more checks will follow
    try:
        _check_content(plan, stage, data, report)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        if not schema_errors:
            raise
        report.errors.append(MORE_CHECKS)
    return report


def _check_content(plan: PlanState, stage: str, data: dict, report: checks.Report) -> None:
    idx = STAGES.index(stage)
    order = _require(plan, *STAGES[:idx])
    if order:
        report.errors += order
        return
    if stage in ("design", "decompose", "sequence"):     # where new code is planned (D-120)
        report.errors += secrets.scan(data)
    _open_items(plan, stage, data, report)
    repo = checks.RepoView.for_plan(plan.meta["repo"])

    if stage == "context":
        cites = []
        for key in ("confirmed", "added", "contradictions"):
            for i, item in enumerate(data[key]):
                cites += [(f"{key}[{i}]", c) for c in item["evidence"]]
        for i, c in enumerate(data["conventions"]):
            cites.append((f"conventions[{i}]", c["example"]))
        for key in ("test_commands", "run_commands"):
            for i, c in enumerate(data[key]):
                cites += [(f"{key}[{i}]", e) for e in c["evidence"]]
        for i, c in enumerate(data.get("check_commands") or []):
            cites += [(f"check_commands[{i}]", e) for e in c["evidence"]]
        for i, n in enumerate(data.get("scope_notes") or []):
            cites += [(f"scope_notes[{i}]", e) for e in n["evidence"]]
        if data.get("feature_trace"):
            cites += [(f"feature_trace.files[{i}]", f["path"]) for i, f in enumerate(data["feature_trace"]["files"])]
        _cites_exist(repo, cites, report)

    elif stage == "intent":
        ids = [o["id"] for o in data["outcomes"]] + [b["id"] for b in data["behaviours"]]
        _assumptions(plan, data, report)
        for d in _dupes(ids):
            report.errors.append(f"duplicate id {d}")

    elif stage == "design":
        _assumptions(plan, data, report)
        intent_assumed = {a["text"] for a in plan.stage("intent").get("assumptions") or []}
        for i, r in enumerate(data.get("retired_assumptions") or []):
            if r["text"] not in intent_assumed:
                report.errors.append(f"retired_assumptions[{i}]: quote an intent assumption exactly "
                                     "(this text isn't one)")
        for d in _dupes([x["id"] for x in data["decisions"]] + [c["id"] for c in data["contracts"]]
                        + [n["id"] for n in data["inventory"]] + [x["id"] for x in data["precursors"]]
                        + [e["id"] for e in data.get("external") or []]):
            report.errors.append(f"duplicate id {d}")
        dec_ids = {d["id"] for d in data["decisions"]}
        contracts = {c["id"] for c in data["contracts"]}
        answered = {qid for qid, q in plan.answers["questions"].items()
                    if q["status"] in ("answered", "accepted_proposal")}
        resolved_pauses = {pid for pid, p in plan.answers["pauses"].items() if p["status"] == "resolved"}
        # a decision may cite any finding that asked for a fix: a design change, or a requirement the
        # design elaborates on a re-run (D-166); never one that was accepted or recorded as a limitation
        design_findings = {f["id"] for r in plan.security_reviews() for f in r["findings"]
                           if f["response"] in ("mitigate", "eliminate")}
        boundary_findings = {f["id"] for r in plan.boundary_checks() for f in r["findings"] if f.get("design_change")}
        for d in data["decisions"]:
            src = d["source"]
            if src["kind"] == "repo" and not repo.exists(src["ref"]):
                report.errors.append(f"{d['id']}: source cites {src['ref']}, which isn't in the repo")
            if src["kind"] in ("designer", "accepted_proposal") \
                    and src["ref"] not in answered | resolved_pauses | {"intent", "checkpoint"}:
                report.errors.append(
                    f"{d['id']}: source {src['kind']} must reference an answered question (Q-n), "
                    f"a resolved pause (PA-n), 'intent' or 'checkpoint'. {_ref_status(plan, src['ref'])}")
            if src["kind"] == "security" and src["ref"] not in design_findings:
                report.errors.append(f"{d['id']}: source security must name a security finding that asked "
                                     f"for a fix, a design change or a requirement (got {src['ref']})")
            if src["kind"] == "boundary" and src["ref"] not in boundary_findings:
                report.errors.append(f"{d['id']}: source boundary must name a boundary finding that asked "
                                     f"for a design change (got {src['ref']})")
            if d["status"] == "superseded" and d.get("superseded_by") not in dec_ids:
                report.errors.append(f"{d['id']}: superseded decisions name the decision that replaces them")
        clients = {e["id"] for e in data.get("external") or [] if checks.is_client(e)}
        for e in data.get("external") or []:
            if checks.is_client(e):
                for name in INFRA_FIELDS + ("after",):
                    if name in e:
                        report.errors.append(f"{e['id']}: a client app adopts this plan's work, so it has no "
                                             f"{name}; leave it out")
                for name in CLIENT_FIELDS:
                    if not e.get(name):
                        report.errors.append(f"{e['id']}: a client app needs {name}: the contracts it can adopt, "
                                             "and what its users see until it does (until_then)")
                for cid in e.get("contracts") or []:
                    if cid not in contracts:
                        report.errors.append(f"{e['id']}: names contract {cid}, which this design doesn't define")
            else:
                for name in INFRA_FIELDS:
                    if name not in e:
                        report.errors.append(f"{e['id']}: infrastructure needs {name}")
                for name in CLIENT_FIELDS:
                    if name in e:
                        report.errors.append(f"{e['id']}: {name} is for client apps (kind: client); leave it out")
                for dep in e.get("after") or []:
                    if dep in clients:
                        report.errors.append(f"{e['id']}: after names {dep}, a client app, which never "
                                             "holds anything back")
            src = e["source"]
            # ask before writing an external dependency down (D-91)
            if src["kind"] == "designer" and src["ref"] not in answered:
                report.errors.append(f"{e['id']}: source designer must reference an answered question (Q-n): "
                                     "ask the designer about it first (environments, what exists, owner, constraints)")
            for dep in e.get("after") or []:
                if dep == e["id"] or dep not in {x["id"] for x in data.get("external") or []}:
                    report.errors.append(f"{e['id']}: after names {dep}, which isn't another external dependency")
            names = [x["name"] for x in e.get("parameters") or []]
            for d in _dupes(names):
                report.errors.append(f"{e['id']}: parameter {d} is listed twice")
        for c in data["contracts"]:
            if not c.get("files"):
                report.errors.append(f"{c['id']}: name the files its code will live in (files), so GANDALF can "
                                     "check the PR that exposes it changes one of them (D-138)")
            if any(IMPORT_LINE.match(line) for line in c["body"].splitlines()):
                report.errors.append(f"{c['id']}: leave imports out of a contract; it fixes the public surface "
                                     "(names, signatures, types, shapes) and the repo's conventions decide imports (D-139)")
        for n in data["inventory"]:
            if n.get("contract") and n["contract"] not in contracts:
                report.errors.append(f"{n['id']}: unknown contract {n['contract']}")
        cites = [(x["id"], x["where"]) for x in data["precursors"]]
        cites += [(f"suspected[{i}]", s["where"]) for i, s in enumerate(data["suspected"])]
        _cites_exist(repo, cites, report)
        # never delete a decision: earlier ones survive, marked superseded (§5.10, R-09)
        current = {c["id"]: c for c in data["contracts"]}
        for c in plan.meta.get("frozen_contracts", []):
            if current.get(c["id"]) != c:
                report.errors.append(f"{c['id']} is exposed by merged work and is frozen: keep it exactly as it was (§5.10)")
        previous = plan.previous_design()
        if previous:
            for d in previous["decisions"]:
                if d["id"] not in dec_ids:
                    report.errors.append(f"{d['id']} from an earlier design is missing; mark it superseded instead")

    elif stage == "approval":
        if not data["approved"] and not (data.get("notes") or "").strip():
            report.errors.append("notes: say what to change; a rejection with no notes re-runs the design "
                                 "with nothing to go on (D-170)")

    elif stage == "decompose":
        intent, design = plan.stage("intent"), plan.stage("design")
        known = {o["id"] for o in intent["outcomes"]} | {b["id"] for b in intent["behaviours"]} \
            | {c["id"] for c in design["contracts"]} | {x["id"] for x in design["precursors"]}
        contracts = {c["id"] for c in design["contracts"]}
        ids = [c["id"] for c in data["components"]]
        leaf_ids = [leaf["id"] for c in data["components"] for leaf in c["leaves"]]
        for d in _dupes(ids + leaf_ids):
            report.errors.append(f"duplicate id {d}")
        for comp in data["components"]:
            for leaf in comp["leaves"]:
                if not leaf["id"].startswith(comp["id"] + "."):
                    report.errors.append(f"leaf {leaf['id']} must be numbered under component {comp['id']}")
                for t in leaf["traces"]:
                    if t not in known:
                        report.errors.append(
                            f"leaf {leaf['id']}: traces {t}, which the intent and design don't define "
                            "(an unimplied capability needs a pause, §6.2)")
                for c in leaf["contracts"]:
                    if c not in contracts:
                        report.errors.append(f"leaf {leaf['id']}: unknown contract {c}")

    elif stage == "sequence":
        result, _ = checks.check_plan(
            intent=plan.stage("intent"), design=plan.stage("design"),
            decompose=plan.stage("decompose"), sequence=data, scan=plan.scan,
            context=plan.stage("context"), repo=repo, security=plan.stage("security"),
            frozen={p["id"] for p in plan.meta.get("frozen", [])},
            previous_sequence=plan.previous_output("sequence"))
        report.extend(result)
        frozen = {p["id"]: p for p in plan.meta.get("frozen", [])}
        new = {p["id"]: p for p in data["prs"]}
        for pid, old in frozen.items():
            if new.get(pid) != old:
                report.errors.append(f"{pid} has merged and is frozen: keep it exactly as it was (§5.10)")

    elif stage == "security":
        design = plan.stage("design")
        report.extend(checks.check_security(data, design))
        report.extend(checks.check_carried(data, checks.earlier_requirements(plan.security_reviews())))
        known = ({o["id"] for o in plan.stage("intent")["outcomes"]} | {b["id"] for b in plan.stage("intent")["behaviours"]}
                 | {d["id"] for d in design["decisions"]})
        for f in data["findings"]:
            for ref in f.get("breaks") or []:
                if ref not in known:
                    report.errors.append(f"security: {f['id']} breaks {ref}, which the brief and design don't have")
        # a fix that goes against a decision reaches the designer before any builder (D-144)
        decisions = {d["id"] for d in design["decisions"] if d["status"] != "superseded"}
        asked = {q.get("about") for q in data.get("questions") or []}
        for f in data["findings"]:
            for did in f.get("contradicts") or []:
                if did not in decisions:
                    report.errors.append(f"security: {f['id']} contradicts {did}, which isn't a current design decision")
            if f.get("contradicts") and not f.get("design_change") and f["id"] not in asked:
                report.errors.append(f"security: {f['id']} goes against {', '.join(f['contradicts'])}, so ask the "
                                     f"designer which wins (a question with about: {f['id']}), or make it a "
                                     "design_change if it's medium or above")
        settled = {i["ref"] for i in checks.settled(plan.stage("intent"), design, plan.security_reviews())}
        for f in data["findings"]:
            if f.get("reopens") and f["reopens"]["ref"] not in settled:
                report.errors.append(f"security: {f['id']} re-opens {f['reopens']['ref']!r}, which isn't in the "
                                     "settled list: copy its ref exactly, or leave out reopens for a new finding")

    elif stage == "boundary":
        report.extend(checks.check_boundary(data, plan.stage("sequence")))
