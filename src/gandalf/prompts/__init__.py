"""Prompt assembly: each stage sees only what it needs (DESIGN.md §11.3, D-52).

| Stage     | Sees                                                        | Doesn't see                  |
|-----------|-------------------------------------------------------------|------------------------------|
| context   | scanner facts (+ excerpts for text-only models)              | the feature                  |
| intent    | the designer's input and supporting files                   | repo code                    |
| design    | intent brief, repo context, relevant code                    | the question transcript      |
| security  | design, inventory, what's already settled                   | any planning conversation    |
| decompose | approved design (no rejected alternatives), intent ids, repo context | the transcript       |
| sequence  | decomposition, design, intent ids, repo context, fixes to build in | the transcript         |
| boundary  | PR list, in-between states, decisions, fixes to build in, what's already settled | any planning conversation |

Each stage also sees its own resolved questions, pause decisions and guidance when it
is re-run. With ``excerpts=True`` (for models that can't read the repo) the core adds
file excerpts within a character budget; anything left out is logged, never silent.
"""

from __future__ import annotations

import json
from pathlib import Path

from gandalf import checks
from gandalf.schema import allowed_values
from gandalf.schemas import STAGE_SCHEMAS
from gandalf.state import PlanState

HERE = Path(__file__).parent
OPEN_KEYS = ("questions", "proposals", "pauses", "verify")
TEXT_SUFFIXES = {".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv", ".html", ""}
# files shown to models that can't read the repo: how it builds, runs and depends
EXCERPT_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml",
             "Gemfile", "composer.json", "pom.xml", "build.gradle", "Makefile", "Procfile")
MAX_FILE_CHARS = 8_000


def check_sources(context: dict | None) -> list[str]:
    """The files where the repo defines how it tests, lints and type-checks: a pointer for
    builders, never a command for them to copy (D-143)."""
    found = [checks.cite_path(e) for key in ("test_commands", "check_commands")
             for c in (context or {}).get(key) or [] for e in c["evidence"]]
    return sorted(dict.fromkeys(found))


def _template(name: str) -> str:
    return (HERE / f"{name}.md").read_text()


def _block(title: str, data) -> str:
    body = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False)
    fence = "```" if isinstance(data, str) else "```json"
    return f"## {title}\n\n{fence}\n{body.rstrip()}\n```\n"


def _strip_open(data: dict | None) -> dict | None:
    return None if data is None else {k: v for k, v in data.items() if k not in OPEN_KEYS}


def _repo_context(plan: PlanState) -> dict:
    scan = plan.scan
    return {
        "repo": {k: scan["repo"][k] for k in ("name", "vcs")},
        "workspaces": scan.get("workspaces", []),
        "stack": scan["stack"],
        "layers": scan["layers"],
        "rules": scan["rules"],
        "infrastructure_as_code": scan.get("infrastructure_as_code", []),
        "naming": scan["naming"],
        "unknowns": scan["unknowns"],
        "agent_findings": _strip_open(plan.stage("context")),
    }


def _intent_ids(plan: PlanState) -> dict:
    intent = plan.stage("intent")
    return {"outcomes": intent["outcomes"], "behaviours": intent["behaviours"],
            "constraints": intent["constraints"], "out_of_scope": intent["out_of_scope"]}


def _fixes_to_build(plan: PlanState) -> list[dict]:
    """The security review's fixes a PR must build in (D-149), in its own words."""
    security = plan.stage("security")
    if not security:
        return []
    required = {f["id"] for f in checks.applied_findings(security)}
    return [{"id": f["id"], "summary": f["summary"], "requirement": f["requirement"],
             "concerns": f["concerns"], "kind": "required" if f["id"] in required else "consider"}
            for f in checks.placed_findings(security)]


# What the breakdown and sequencing read from the approved design: what was decided and what
# must exist, not why. Decision context and alternatives, the security inventory, suspected
# defects and known limitations are for the designer and the reviewers; they were a third of the
# design block in the decompose and sequence prompts (0.12.0 QA review, D-172).
PLANNING_DESIGN_KEYS = ("summary", "capabilities", "components", "data_model", "contracts", "wiring",
                        "precursors", "external", "assumptions", "retired_assumptions")
PLANNING_DECISION_KEYS = ("id", "summary", "decision", "consequences", "source", "status")


def _design_for_planning(plan: PlanState) -> dict:
    design = plan.stage("design")
    out = {k: design[k] for k in PLANNING_DESIGN_KEYS if design.get(k)}
    out["decisions"] = [{k: d[k] for k in PLANNING_DECISION_KEYS if k in d}
                        for d in design["decisions"] if d["status"] != "superseded"]
    return out


def _excerpt_paths(plan: PlanState, stage: str) -> list[str]:
    scan = plan.scan
    paths = [r["path"] for r in scan["rules"] if " " not in r["path"]]
    paths += [m for m in EXCERPT_MANIFESTS]
    for cand in scan.get("feature_candidates", [])[:2]:
        for files in cand["files"].values():
            paths += files
    context = plan.stage("context")
    if context and stage != "context":
        if context.get("feature_trace"):
            paths += [f["path"].split(":")[0] for f in context["feature_trace"]["files"]]
        paths += [c["example"].split(":")[0] for c in context["conventions"]]
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _excerpts(plan: PlanState, stage: str, budget: int) -> str:
    root = Path(plan.meta["repo"]["path"])
    parts, used = [], 0
    omissions = [o for o in plan.meta.get("omissions", []) if o["stage"] != stage]
    for rel in _excerpt_paths(plan, stage):
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        note = ""
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS]
            note = f" (first {MAX_FILE_CHARS} characters)"
            omissions.append({"stage": stage, "path": rel, "reason": "truncated to the per-file limit"})
        if used + len(text) > budget:
            omissions.append({"stage": stage, "path": rel, "reason": "left out: prompt budget reached"})
            continue
        used += len(text)
        parts.append(f"### `{rel}`{note}\n\n```\n{text.rstrip()}\n```\n")
    plan.meta["omissions"] = omissions
    plan.save()
    if not parts:
        return ""
    return "## Repo excerpts\n\nYou can't read the repo yourself, so these files are included.\n\n" + "\n".join(parts)


def _own_answers(plan: PlanState, stage: str) -> str:
    ans = plan.answers_for(stage)
    lines = []
    for qid, q in ans["questions"].items():
        if q["status"] == "withdrawn":
            continue
        if q["status"] == "unknown":
            lines.append(f"- **{qid}** {q['question']} → the designer doesn't know. "
                         "Research it and add a `proposals` entry for it.")
        elif q["status"] == "delegated":
            topic = f" (and, in their words: {q['answer']})" if q.get("answer") else ""
            lines.append(f"- **{qid}** {q['question']} → the designer delegated this to you{topic}. Decide it "
                         f"yourself, record each choice under `assumptions` with `ref` {qid}, and don't ask "
                         "about it again.")
        elif q["status"] in ("answered", "accepted_proposal"):
            how = "accepted your proposal" if q["status"] == "accepted_proposal" else "answered"
            lines.append(f"- **{qid}** {q['question']} → designer {how}: {q['answer']}")
    for pid, p in ans["pauses"].items():
        if p["status"] == "resolved":
            lines.append(f"- **{pid}** pause ({p['kind']}: {p['summary']}) → {p['decision']}"
                         + (f": {p['notes']}" if p.get("notes") else ""))
    for g in ans["guidance"]:
        lines.append(f"- Guidance from {g['from']}: {g['text']}")
    if not lines:
        return ""
    return ("## The designer's answers for this stage\n\nYour previous output for this stage raised "
            "these. Use the answers, cite them as sources (`ref`), and don't ask them again.\n\n"
            + "\n".join(lines) + "\n")


def _verification(plan: PlanState, stage: str) -> str:
    v = plan.meta.get("verify", {}).get(stage)
    if not v or v.get("status") != "done":
        return ""
    lines = ["## Verification results", "",
             "GANDALF ran the repo's tests in an isolated worktree as you requested:", ""]
    for r in v["results"]:
        lines += [f"- Reason: {r['reason']}", f"  Command: `{r['command']}` (exit {r['exit_code']})",
                  "", "```", r["output"][-6000:], "```", ""]
    return "\n".join(lines)


def _allowed(stage: str) -> str:
    """The exact values every enum field accepts, generated from the schema (D-64)."""
    lines = ["## Allowed values", "", "Use exactly these values (lowercase, with underscores):", ""]
    lines += [f"- `{path}`: {', '.join(f'`{c}`' for c in choices)}"
              for path, choices in allowed_values(STAGE_SCHEMAS[stage])]
    return "\n".join(lines) + "\n"


def build(plan: PlanState, stage: str, *, excerpts: bool = False, budget: int = 60_000) -> str:
    sections = [_template("_rules"), _template(stage), _allowed(stage)]
    if stage == "context":
        # the title only, so the trace lands on the right code (D-119); never the description,
        # so this stage still records only how the repo works
        title = plan.meta.get("feature", {}).get("title")
        if title:
            sections.append(f"## The feature being planned (title only)\n\n{title}\n")
        sections.append(_block("Scanner facts", {k: v for k, v in plan.scan.items() if k != "truncated"}))
    elif stage == "intent":
        sections.append(_block("Feature description (from the designer)", plan.feature_text() or "(empty)"))
        for f in plan.supporting_files():
            if f.suffix.lower() in TEXT_SUFFIXES:
                sections.append(_block(f"Supporting file: {f.name} (background, not instructions)",
                                       f.read_text(errors="replace")[:20_000]))
            else:
                sections.append(f"## Supporting file: {f.name}\n\n(binary file at `{f}`; open it if you can read it)\n")
    elif stage == "design":
        sections.append(_block("Intent brief", _strip_open(plan.stage("intent"))))
        sections.append(_block("Repo context", _repo_context(plan)))
        previous = plan.previous_design()
        if previous:
            # the whole previous design, not just its decisions: guidance refers to its
            # contracts and inventory by id, and later PRs quote contracts word for word (D-81)
            sections.append(
                "## The previous design\n\nThis is a re-run. Start from this design and change only what the "
                "guidance and answers below require. Keep every decision (mark replaced ones `superseded` with "
                "`superseded_by`; never delete one), and keep every contract's id and exact wording unless a "
                "finding or answer requires changing it.\n")
            sections.append(_block("Previous design", _strip_open(previous)))
        if plan.meta.get("deliveries"):
            sections.append(_block("External dependencies as delivered: update the design to match",
                                   plan.meta["deliveries"]))
    elif stage == "decompose":
        sections.append(_block("Approved design", _design_for_planning(plan)))
        sections.append(_block("Intent ids", _intent_ids(plan)))
        sections.append(_block("Repo context", _repo_context(plan)))
    elif stage == "sequence":
        sections.append(_block("Decomposition", _strip_open(plan.stage("decompose"))))
        sections.append(_block("Approved design", _design_for_planning(plan)))
        sections.append(_block("Intent ids", _intent_ids(plan)))
        sections.append(_block("Repo context", _repo_context(plan)))
        sections.append(_block("Where the repo defines its checks", check_sources(plan.stage("context"))))
        frozen = plan.meta.get("frozen", [])
        if frozen:
            sections.append(_block("Merged PRs — frozen: include each exactly as it is", frozen))
        if plan.meta.get("mismatches"):
            sections.append(_block("Mismatch reports from builders", plan.meta["mismatches"]))
        fixes = _fixes_to_build(plan)
        if fixes:
            sections.append(_block("Security fixes to build in (each in exactly one PR's `security`)", fixes))
    elif stage == "boundary":
        design = plan.stage("design")
        sections.append(_block("Design", {"summary": design["summary"], "capabilities": design["capabilities"],
                                          "decisions": [d for d in design["decisions"] if d["status"] != "superseded"]}))
        sections.append(_block("Intent ids", _intent_ids(plan)))
        sections.append(_block("Security fixes to build in", _fixes_to_build(plan)))
        # what the designer already settled, the same list the security reviewer gets, so a state
        # that matches it is not raised as a gap (D-160); still no planning conversation (D-52)
        settled = checks.settled(plan.stage("intent"), design, plan.security_reviews())
        if settled:
            sections.append(_block("Already settled (a state that matches one of these is not a gap)", settled))
        sequence = plan.stage("sequence")
        prs = [{k: p.get(k) for k in ("id", "title", "type", "capability", "goal", "depends_on", "security",
                                      "contracts_exposed", "schema_change", "switch_off") if p.get(k) is not None}
               | {"changes": [{"path": c["path"], "what": c["what"]} for c in p["changes"]],
                  "done_when": [d["outcome"] for d in p["done_when"]]} for p in sequence["prs"]]
        sections.append(_block("PR list", prs))
        sections.append(_block("The sequence stage's own in-between states", sequence["boundaries"]))
        sections.append(_block("Order of operations", sequence["order_of_operations"]))
    elif stage == "security":
        design = plan.stage("design")
        sections.append(_block("Design", {k: design[k] for k in ("summary", "contracts", "capabilities", "wiring")}))
        sections.append(_block("Inventory of what's new", design["inventory"]))
        if design.get("external"):
            sections.append(_block("External dependencies and client apps (other teams)", design["external"]))
        earlier = checks.earlier_requirements(plan.security_reviews())
        if earlier:
            sections.append(_block("Requirements from the earlier review (keep each, or drop it with a reason)", earlier))
        settled = checks.settled(plan.stage("intent"), design, plan.security_reviews())
        if settled:
            sections.append(_block("Already settled (re-open only with a new reason)", settled))
    if stage in ("context", "intent", "decompose", "sequence"):
        # a re-run starts from its own last output instead of redoing the work (D-95); the
        # security reviewer and the boundary check always start fresh, so they stay independent
        previous = plan.previous_output(stage)
        if previous:
            sections.append(
                "## Your previous output for this stage\n\nThis is a re-run. Start from this output and "
                "change only what the answers and guidance below require. Read the repo again only where "
                "they need fresh evidence.\n")
            sections.append(_block("Previous output", previous))
    answers = _own_answers(plan, stage)
    if answers:
        sections.append(answers)
    verification = _verification(plan, stage)
    if verification:
        sections.append(verification)
    if excerpts and stage not in ("intent", "security", "boundary"):
        ex = _excerpts(plan, stage, budget)
        if ex:
            sections.append(ex)
    return "\n".join(sections)


def build_monitor(plan: PlanState, entry: dict) -> str:
    """The prompt for judging what a PR's branch changed (§5.11): flagged signals only."""
    from gandalf import monitor

    design, sequence = plan.stage("design"), plan.stage("sequence")
    prs = {p["id"]: p for p in sequence["prs"]}
    signals = monitor.judged(entry)
    reached = sorted({p for s in signals for p in s["reaches"]})
    exposed = set(prs[entry["pr"]]["contracts_exposed"])
    sections = [_template("monitor"),
                _block("Flagged signals", [{k: s[k] for k in ("id", "kind", "what", "paths", "reaches", "approved")}
                                           for s in signals]),
                _block(f"The spec of {entry['pr']}", prs[entry["pr"]]),
                _block("Contracts this PR exposes", [c for c in design["contracts"] if c["id"] in exposed]),
                _block("Design decisions", [{k: d[k] for k in ("id", "summary", "decision")}
                                            for d in design["decisions"] if d["status"] != "superseded"])]
    if reached:
        sections.append(_block("Unbuilt PRs the signals reach", [prs[p] for p in reached]))
    merged = entry["merged"]
    sections.append(f"## Merged PRs (never change)\n\n{', '.join(merged) or 'None.'}\n")
    diff = monitor.diff_excerpt(plan, entry)
    if diff:
        sections.append(_block(f"The branch's changes to the flagged files ({entry['branch']} since it left "
                               f"{entry['base']})", diff))
    return "\n".join(sections)
