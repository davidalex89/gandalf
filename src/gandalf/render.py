"""Write the plan (DESIGN.md §10): PR specs, plan.md, supporting documents, plan.json.

Rendering is deterministic and refuses to run unless every stage is complete and every
check passes. Sections that don't apply say "None" rather than being left out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gandalf import SCHEMA_VERSION, __version__, checks, diagnose
from gandalf.state import PlanState, now
from gandalf.state import _read as _read_json
from gandalf.prompts import check_sources

STANDARD_OUT_OF_SCOPE = (
    "Do not add this plan, its files, or references to them (e.g. \"see PR-03 spec\") to the "
    "repo, code comments, commit messages or PR descriptions.",
    "Do not change files not listed under Changes. A small fix inside a listed file or a test "
    "file that leaves every contract, decision and starting-state fact as written is yours to "
    "make: carry on and record it in your deviation note (section 14). Anything else: stop and "
    "report it (section 14).",
    "Do not write or change infrastructure as code (Terraform, CloudFormation, Pulumi, CDK, "
    "Bicep/ARM, Kubernetes or Helm manifests) or configuration management (Puppet, Ansible, Chef, "
    "Salt, anything under .github/, CODEOWNERS, CI pipeline files). If the work seems to need it, "
    "stop and report it "
    "(section 14).",
    "Never put a password, key, token or other secret anywhere in the repo (code, config, tests, "
    "docs, examples), not even as a default or for local development: read it from the repo's "
    "configuration. Example and test files get empty or obviously fake values. If the work seems "
    "to need a real secret in the repo, stop and report it (section 14).",
)
SLUG_SKIP = {"a", "an", "the", "for", "to", "of", "and", "in", "on", "with", "that", "can"}
LAYER_ORDER = ("database", "models", "api", "frontend", "tests", "config", "other")


class RenderError(Exception):
    pass


@dataclass
class Rendered:
    tier: str
    files: list[Path] = field(default_factory=list)
    report: checks.Report = field(default_factory=checks.Report)


def _none(items) -> str | None:
    return None if items else "None."


def report_time(stamp: str | None = None) -> str:
    """A time for people to read: 12-hour Pacific time, e.g. 'October 25, 2026, 11:35 AM PDT'.
    Stored timestamps stay in UTC; this is only how the report shows them."""
    when = datetime.fromisoformat(stamp) if stamp else datetime.now(timezone.utc)
    try:
        when = when.astimezone(ZoneInfo("America/Los_Angeles"))
    except ZoneInfoNotFoundError:          # no time zone database (e.g. Windows without tzdata)
        when = when.astimezone(timezone.utc)
    hour = when.hour % 12 or 12
    return (f"{when:%B} {when.day}, {when.year}, {hour}:{when:%M} {'AM' if when.hour < 12 else 'PM'} "
            f"{when.tzname()}")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "None."


def _live_assumptions(intent: dict, design: dict | None) -> tuple[list[dict], list[dict]]:
    """Intent assumptions still in force, and those the design retired (D-116)."""
    retired = {r["text"] for r in (design or {}).get("retired_assumptions") or []}
    items = intent.get("assumptions") or []
    return [a for a in items if a["text"] not in retired], [a for a in items if a["text"] in retired]


def _assumption_lines(items: list[dict]) -> list[str]:
    """Details decided for the designer (D-96): delegated ones cite the question."""
    return [f"{a['text']} *({'you delegated ' + a['ref'] if a.get('ref') else 'a small detail decided without asking'}; "
            f"why: {a['why']})*" for a in items]


def _code(body: str, language: str) -> str:
    fence = "````" if "```" in body else "```"
    return f"{fence}{language.lower()}\n{body.rstrip()}\n{fence}"


class Planner:
    """Loads a finished plan and computes everything the documents need."""

    def __init__(self, plan: PlanState):
        self.plan = plan
        action = plan.next_action()
        if action["action"] != "render":
            raise RenderError(f"the plan isn't finished yet: next step is {action['action']} {action.get('stage', '')}".strip())
        self.scan = plan.scan
        self.context = plan.stage("context")
        self.intent = plan.stage("intent")
        self.design = plan.stage("design")
        self.decompose = plan.stage("decompose")
        self.sequence = plan.stage("sequence")
        # each fix's PR comes from the sequence, which built it in (D-149)
        self.security = checks.with_placement(plan.stage("security"), self.sequence)
        self.boundary = plan.stage("boundary")
        # a gap the designer carried (D-180) stays on record even when a later re-run archived
        # the check that found it; one entry per finding id, the latest decision wins
        self.carried_gaps = list({f["id"]: f for c in plan.boundary_checks()
                                  for f in c.get("findings", []) if f.get("carried")}.values())
        self.reviews = plan.security_reviews()
        self.repo = plan.meta["repo"]
        self.reduced = self.repo["vcs"] == "none"
        self.unit = "change set" if self.reduced else "PR"

        view = checks.RepoView.for_plan(self.repo)
        report, derived = checks.check_plan(
            intent=self.intent, design=self.design, decompose=self.decompose,
            sequence=self.sequence, scan=self.scan, context=self.context, repo=view,
            security=self.security, frozen={p["id"] for p in plan.meta.get("frozen", [])})
        report.extend(checks.check_security(self.security, self.design))
        if not report.ok:
            raise RenderError("the plan fails its checks:\n" + "\n".join(f"- {e}" for e in report.errors))
        self.report = report
        self.derived = derived
        self.prs = {p["id"]: p for p in self.sequence["prs"]}
        self.order = derived["order"]
        self.lane_of = {pid: i + 1 for i, lane in enumerate(derived["lanes"]) for pid in lane}
        self.wave_of = {pid: i + 1 for i, wave in enumerate(derived["waves"]) for pid in wave}
        self.contracts = {c["id"]: c for c in self.design["contracts"]}
        self.decisions = {d["id"]: d for d in self.design["decisions"]}
        # infrastructure this plan needs, and other teams' apps that adopt it (D-127)
        self.external = {e["id"]: e for e in self.design.get("external") or [] if not checks.is_client(e)}
        self.clients = {e["id"]: e for e in self.design.get("external") or [] if checks.is_client(e)}
        self.deliveries = plan.meta.get("deliveries", {})
        # PR monitoring (§5.11): every change found on a branch, oldest first
        self.changes = [e for e in _read_json(plan.dir / "changes.json", []) if e["status"] not in ("judging", "replaced")]
        self.leaves = {leaf["id"]: leaf for comp in self.decompose["components"] for leaf in comp["leaves"]}
        self.applied = checks.applied_findings(self.security)
        self.security_by_pr: dict[str, list[dict]] = {}
        for f in self.applied:
            self.security_by_pr.setdefault(f["target_pr"], []).append(f)
        # low-severity fixes with a concrete requirement: shown as non-blocking notes (D-87)
        self.consider_by_pr: dict[str, list[dict]] = {}
        for f in self.security["findings"]:
            if (f["severity"] == "low" and not f.get("breaks") and f["response"] in ("mitigate", "eliminate")
                    and f.get("requirement") and (f.get("target_pr") in self.prs or f.get("target_pr") in self.external | self.clients)):
                self.consider_by_pr.setdefault(f["target_pr"], []).append(f)
        self.tier = checks.select_tier(self.sequence, plan.approved_pauses(), plan.meta.get("force_full", False))

    # --- helpers --------------------------------------------------------------------
    def pr_file(self, pid: str) -> str:
        num = pid.split("-", 1)[1]
        return f"prs/pr-{num}-{self.prs[pid]['slug']}.md"

    def pr_type_line(self, pr: dict) -> str:
        if pr["type"] == "inert":
            return f"Inert — {pr['layer']} layer"
        if pr["type"] == "enabling":
            return f"Enabling — switches on: {pr['capability']}"
        return "Precursor — fixes an existing issue before the work"

    def size_line(self, pr: dict) -> str:
        s = pr["size"]
        files = len(pr["changes"])          # counted, not the model's number (D-173)
        return (f"{self.derived['bands'][pr['id']]} (est. ~{s['production_lines']} production lines; "
                f"for information: ~{s['total_lines']} reviewable lines incl. tests, "
                f"{files} file{'s' if files != 1 else ''})")

    def request_file(self, eid: str) -> str:
        """requests/ for infrastructure this plan needs; handoffs/ for client apps (D-127)."""
        num = int(eid.split("-")[1])
        e = self.external.get(eid) or self.clients[eid]
        words = "".join(c if c.isalnum() else " " for c in e["summary"].lower().replace("'", "")).split()
        slug = "-".join([w for w in words if w not in SLUG_SKIP][:5]) or "dependency"
        return f"{'handoffs' if eid in self.clients else 'requests'}/e-{num:02d}-{slug}.md"

    def available_after(self, eid: str) -> tuple[list[str], list[str]]:
        """The PRs that expose what a client app adopts, and the enabling PRs that switch it on."""
        wanted = set(self.clients[eid]["contracts"])
        exposers = [pid for pid in self.order if wanted & set(self.prs[pid]["contracts_exposed"])]
        closure = checks.transitive_deps(self.sequence["prs"])
        switches = [pid for pid in self.order if self.prs[pid]["type"] == "enabling"
                    and (pid in exposers or set(exposers) & closure.get(pid, set()))]
        return exposers, switches

    def users_of(self, eid: str) -> list[str]:
        return [pid for pid in self.order if eid in {e for e, _ in checks.external_uses(self.prs[pid])}]

    def gated_by(self, eid: str) -> list[str]:
        """PRs that can't merge until this dependency is ready."""
        return [pid for pid, gates in self.derived["gates"].items() if eid in gates]

    def switch_gated_by(self, eid: str) -> list[str]:
        """PRs switched on by configuration that is set only once this dependency is ready (D-109)."""
        return [pid for pid, gates in self.derived.get("switch_gates", {}).items() if eid in gates]

    def needed_before(self, eid: str) -> str:
        parts = []
        if self.gated_by(eid):
            parts.append(f"{', '.join(self.gated_by(eid))} merges")
        parts += [f"the settings that switch on {pid} are set" for pid in self.switch_gated_by(eid)]
        return " and ".join(parts) or "the switch-on"

    def _owner(self, eid: str) -> str:
        owner = self.external[eid]["owner"]
        return "its owners" if owner == "unknown" else owner

    def delivery_status(self, eid: str) -> str:
        d = self.deliveries.get(eid)
        if not d:
            if self.external[eid]["exists"] == "yes":
                return f"already exists; confirm its settings with {self._owner(eid)}"
            return "requested"
        return "delivered as requested" if d["as_requested"] else "delivered with changes; the plan was updated to match"

    def must_hold(self, pr: dict) -> list[str]:
        """What's true of every PR of its type when it's done, stated, never a command (D-143)."""
        out = {"inert": ["The repo's existing tests pass, unchanged: nothing a user or client can observe has changed."],
               "enabling": ["The capability works end to end, as Done when describes, and the repo's existing tests "
                            "pass, updated only where this PR intends a change.",
                            f"It can be switched back off: {(pr.get('switch_off') or {}).get('how', 'see section 10')}"],
               "precursor": ["The existing problem this PR fixes no longer happens, and the repo's existing tests pass."],
               }[pr["type"]]
        if pr.get("flag"):
            out.append(f"It works with the flag `{pr['flag']['name']}` on and with it off.")
        out.append("The repo's own lint, format and type checks pass, the way the repo enforces them (some may be "
                   "advisory in its CI).")
        scanner = checks.secret_scanner(self.scan, self.context)
        if scanner:
            out.append(f"The repo's secret scanner ({scanner}) passes: no secret reaches the repo.")
        return out

    def tag(self, pid: str, path: str) -> str:
        return self.derived["tags"].get(f"{pid}|{checks.cite_path(path)}", "unverified")

    def repo_rules(self) -> list[str]:
        return [f"`{r['path']}` ({r['kind']})" for r in self.scan.get("rules", [])]

    def review_first(self) -> list[str]:
        scored = []
        for pid in self.order:
            pr = self.prs[pid]
            reasons = []
            if pr["type"] == "enabling":
                reasons.append("switches behaviour on")
            if pr["breaking"]["is_breaking"]:
                reasons.append("breaking change")
            risk = [r for r in self.derived["risk"][pid] if r != "enabling"]
            if risk:
                reasons.append("risk: " + ", ".join(risk))
            if self.security_by_pr.get(pid):
                reasons.append(f"{len(self.security_by_pr[pid])} security requirement(s)")
            if reasons:
                scored.append((len(reasons), pid, reasons))
        scored.sort(key=lambda t: (-t[0], self.order.index(t[1])))
        return [f"[{pid}]({self.pr_file(pid)}) {self.prs[pid]['summary']} — {'; '.join(r)}"
                for _, pid, r in scored[:5]]

    # --- PR spec (§10.3) ----------------------------------------------------------------
    def pr_spec(self, pid: str) -> str:
        pr = self.prs[pid]
        unit = self.unit
        nb = self.derived["needed_by"][pid]
        risk = self.derived["risk"][pid]
        out = [f"# {pid}: {pr['title']}", "", f"**In short:** {pr['summary']}", ""]
        out += [
            f"- **Type:** {self.pr_type_line(pr)}",
            f"- **Lane:** {self.lane_of[pid]} of {len(self.derived['lanes'])} (a chain one builder can take end to end)",
            f"- **Wave:** {self.wave_of[pid]} of {len(self.derived['waves'])} ("
            + ("nothing earlier to wait for" if self.wave_of[pid] == 1
               else f"can start once wave {self.wave_of[pid] - 1} has merged") + ")",
            f"- **Risk tags:** {', '.join(risk) if risk else 'None'}",
            f"- **Size:** {self.size_line(pr)}",
            f"- **Depends on:** {', '.join(pr['depends_on']) or 'None'}",
            f"- **Needed by:** {', '.join(nb) or 'None'}",
        ]
        gates = self.derived["gates"].get(pid, [])
        if gates:
            out.append(f"- **Waits for (other teams):** {', '.join(gates)} ready before this merges; "
                       "see the plan's requests/ folder")
        if pr.get("switched_on_by"):
            needs = self.derived.get("switch_gates", {}).get(pid, [])
            out.append(f"- **Switched on by configuration:** {', '.join(f'`{x}`' for x in pr['switched_on_by']['settings'])}"
                       + (f", set only once {', '.join(needs)} {'is' if len(needs) == 1 else 'are'} ready" if needs else "")
                       + ". This PR can merge before then: merging it changes nothing until they're set")
        if pr.get("size_reason"):
            out.append(f"- **Kept whole because:** {pr['size_reason']}")
        if pr.get("kept_separate_because"):
            out.append(f"- **Kept separate because:** {pr['kept_separate_because']}")
        changed = [e["id"] for e in self.changes if pid in (e.get("applied") or {}).get("prs", [])]
        if changed:
            out.append(f"- **Changed while building:** {', '.join(changed)} (see the plan's change history)")
        out.append("")

        out += ["## 1. Goal", "", pr["goal"], ""]

        out += ["## 2. Starting state", "", pr["starting_state"]["description"], ""]
        if pr["depends_on"]:
            out.append(f"Already merged before you start: {', '.join(pr['depends_on'])}.")
            out.append("")
        for eid, when_missing in checks.external_uses(pr):
            e = self.external[eid]
            if eid in self.derived["gates"].get(pid, []):
                gate = f"This PR switches the capability on, so it merges only once {eid} is ready: {e['ready_when']}."
            elif eid in self.derived.get("switch_gates", {}).get(pid, []):
                gate = (f"This PR can merge before {eid} is ready; the settings that switch it on are set only "
                        f"once it is.")
            else:
                gate = f"This PR can merge before {eid} is ready."
            out += [f"**Relies on {eid}, built by {e['owner'] if e['owner'] != 'unknown' else 'another team'}:** "
                    f"{e['summary']} ({self.delivery_status(eid)}). {gate}", "",
                    "Settings the code reads:", ""]
            out += [f"- `{x['name']}`: {x['purpose']}" + (f" (e.g. `{x['example']}`)" if x.get("example") else "")
                    for x in e["parameters"]] or ["None."]
            out += ["", f"While it isn't there: {when_missing}", ""]
            if e["open_questions"] and eid not in self.deliveries:
                out.append(f"Still to be settled by its owners, so these settings may change before you build: "
                           f"{'; '.join(e['open_questions'])}. If they differ from this spec, stop and report (section 14).")
            out.append("")
        out.append("Confirm these are true before you start. If any isn't, stop and report (section 14):")
        out.append("")
        out += [f"- {fact}" for fact in pr["starting_state"]["facts"]]
        out.append("")

        out += ["## 3. Read first", ""]
        out += [f"- `{r['path']}` — {r['why']}" for r in pr["read_first"]]
        out.append("")

        out += ["## 4. Contracts used", ""]
        if pr["contracts_used"]:
            out.append("Provided by earlier work. Match them exactly; do not change or \"improve\" them.")
            out.append("")
            for cid in pr["contracts_used"]:
                c = self.contracts[cid]
                out += [f"### {cid}: {c['name']}", "", _code(c["body"], c["language"]), ""]
        else:
            out += ["None.", ""]

        out += ["## 5. Contract exposed", ""]
        if pr["contracts_exposed"]:
            out.append("Provide exactly this surface: the names, signatures, types and shapes are what later work "
                       "quotes word for word. Arrange imports, private helpers and formatting as the repo's conventions "
                       "and linters require.")
            out.append("")
            for cid in pr["contracts_exposed"]:
                c = self.contracts[cid]
                out += [f"### {cid}: {c['name']}", "", _code(c["body"], c["language"]), "", f"Why: {c['why']}", ""]
                if c.get("files"):
                    out += [f"Lives in: {', '.join(f'`{x}`' for x in c['files'])}", ""]
        else:
            out += ["None.", ""]

        out += ["## 6. Changes", ""]
        for ch in pr["changes"]:
            line = f"- **{ch['action']}** `{ch['path']}` [{self.tag(pid, ch['path'])}] — {ch['what']}"
            if ch.get("pattern"):
                line += f" Follow: {ch['pattern']}."
            named = checks.change_leaves(ch)
            line += f" (leaf{'s' if len(named) > 1 else ''} {', '.join(named)})"
            out.append(line)
        out += ["", "**Reuse — do not recreate:**", ""]
        out.append(_bullets([f"{r['what']} — `{r['where']}`" for r in pr["reuse"]]))
        out.append("")

        out += ["## 7. Order of steps", ""]
        if not pr["steps"]:
            out.append("None — order within this change doesn't matter.")
        else:
            out += ["Build these as one commit each, in order. A fresh session may be given this spec and one "
                    "step number: it builds only that step, on this PR's branch after the earlier steps' commits, "
                    "and stops once that step's \"done when\" holds. Section 12 applies once every step is in.", ""]
            for i, st in enumerate(pr["steps"], 1):
                out += [f"{i}. {st['what']}",
                        f"   - Files: {', '.join(f'`{path}`' for path in st['changes'])}",
                        f"   - Done when: {st['done_when']}"]
        out.append("")

        out += ["## 8. Conventions", ""]
        out.append(_bullets([f"{c['rule']} — e.g. `{c['example']}`" for c in pr["conventions"]]))
        out += ["", "Follow the repo's own rules for contributors and agents:", ""]
        out.append(_bullets(self.repo_rules()))
        out.append("")

        out += ["## 9. Decisions", ""]
        if pr["decisions"]:
            for did in pr["decisions"]:
                d = self.decisions[did]
                out.append(f"- **{did}:** {d['decision']} — Why: {d['context']} Consequences: {d['consequences']}")
        else:
            out.append("None.")
        out.append("")

        out += ["## 10. Breaking changes", ""]
        b = pr["breaking"]
        out.append(f"{'Breaking' if b['is_breaking'] else 'Not breaking'}: {b['detail']}")
        if pr.get("unobservable_how"):
            out += ["", f"How this stays unobservable: {pr['unobservable_how']}"]
        if pr.get("switch_off"):
            so = pr["switch_off"]
            out += ["", f"How to switch it back off: {so['how']}"]
        if pr.get("expand_contract"):
            ec = pr["expand_contract"]
            out += ["", f"This is the **{ec['role']}** step; its pair is {ec['pair']}."]
        if pr.get("flag"):
            out += ["", f"Introduces flag `{pr['flag']['name']}`, removed by {pr['flag']['removed_by']}."]
        if pr.get("removes_flag"):
            out += ["", f"Removes flag `{pr['removes_flag']}`."]
        if pr.get("schema_change"):
            out += ["", f"Schema change: {pr['schema_change']}",
                    "Produce the migration the way this repo does (see Conventions), "
                    "and make sure it runs before anything reads the change."]
        out.append("")

        out += ["## 11. Security", ""]
        reqs = self.security_by_pr.get(pid, [])
        considers = self.consider_by_pr.get(pid, [])
        out.append(_bullets([f"**{f['id']}** ({f['severity']}): {f['requirement']} — {f['citation']}" for f in reqs]
                            + [f"**Consider ({f['id']}, low, not required):** {f['requirement']} — {f['citation']}"
                               for f in considers]))
        out.append("")

        out += ["## 12. Done when", ""]
        for d in pr["done_when"]:
            traces = f" ({', '.join(d['traces'])})" if d["traces"] else ""
            out.append(f"- {d['outcome']}{traces}")
        out += ["", "And these must hold. How you show it is your work, the way this repo checks its own:", ""]
        out += [f"- {line}" for line in self.must_hold(pr)]
        sources = check_sources(self.context)
        if sources:
            out += ["", f"Where the repo defines its tests and checks: {', '.join(f'`{x}`' for x in sources)}."]
        for f in self.carried_gaps:
            if f["after"] == pid:
                out += ["", f"The designer carried a gap the boundary check found after this PR ({f['id']}) rather "
                            f"than replanning it: {f['summary']}. What the check asked for, left to the builder's "
                            f"and reviewer's attention: {f['replan']} Designer's notes: {f['carried']['notes'] or 'none'}."]
        out.append("")

        out += ["## 13. Out of scope", ""]
        out.append(_bullets(list(pr["out_of_scope"]) + list(STANDARD_OUT_OF_SCOPE)))
        out.append("")

        out += ["## 14. If reality doesn't match", "", "Two cases (D-151).", "",
                "**A small difference you can settle inside this PR's own files**: a bug in a file listed "
                "under Changes, a test that needs adjusting, a repo command that needs another argument. "
                "If the fix leaves every contract in sections 4 and 5, every decision in section 9 and every "
                "starting-state fact as written, and adds no dependency or data change, make it and carry on. "
                "List each one in this note and hand it to the designer with the finished PR. It never goes "
                "in the repo, a commit message or the PR description. Notes from the PRs this one depends "
                "on come with this spec, if there were any: read them before the starting state, since a "
                "note can move where something lives (D-177).", "",
                "```text",
                "GANDALF DEVIATION NOTE",
                f"PR: {pid}",
                "- What: <the change, one line> | Where: <file:line> | Why: <what the spec didn't foresee>",
                "```", "",
                "**Anything else**: a starting-state fact that isn't true, a contract you can't provide as "
                "written, a change needed in a file not listed under Changes, a dependency or data change the "
                "spec doesn't name, a decision that can't hold, infrastructure, a secret. Stop. Do not "
                "improvise or work around it. Write this report and hand it to the designer:", "",
                "```text",
                "GANDALF MISMATCH REPORT",
                f"PR: {pid}",
                "Section: <number and name of the section that doesn't match>",
                "Expected: <what this spec says>",
                "Found: <what is actually there>",
                "Evidence: <file:line references, command output>",
                "```", ""]
        return "\n".join(out)

    # --- shared sections ----------------------------------------------------------------
    def banner(self) -> list[str]:
        if not self.reduced:
            return []
        return ["> **Reduced mode — the target is not a git repository.** Unavailable: running tests "
                "to verify findings (marked \"not verified by running\"); commit pinning (a content "
                f"fingerprint, `{self.repo['fingerprint'][:12]}…`, is used instead); pull requests "
                "(work is described as ordered change sets).", ""]

    def attention(self) -> list[str]:
        """One plain-language line per item; the detail lives in design.md (D-70)."""
        items = []
        for qid, q in self.plan.answers["questions"].items():
            if q["status"] == "accepted_proposal":
                summary = q.get("answer_summary") or q["answer"]
                items.append(f"**{qid}**, an agent proposal you accepted: {summary}")
        for pid, p in self.plan.answers["pauses"].items():
            if p["status"] == "resolved":
                items.append(f"**{pid}**, {p['kind'].replace('_', ' ')} you {p['decision']}: {p['summary']}")
        seen_notes = set()
        for g in self.plan.answers["guidance"]:
            if g["from"] == "design checkpoint (approved with notes)" and g["text"] not in seen_notes:
                seen_notes.add(g["text"])
                items.append(f"**Your approval note** at the design checkpoint: {g['text']}")
        assumed = len(_live_assumptions(self.intent, self.design)[0]) + len(self.design.get("assumptions") or [])
        if assumed:
            where = "intent-brief.md and design.md" if self.tier == "full" else "technical.md"
            items.append(f"**Assumptions**: {assumed} detail{'s were' if assumed != 1 else ' was'} decided for "
                         f"you, listed in {where}")
        for x in self.design["precursors"]:
            items.append(f"**{x['id']}**, an existing issue fixed {x['placement']} the work: {x.get('summary') or x['what']}")
        items += [f"**{d['ref']}**, a security requirement dropped on re-review: {d['reason']}"
                  for d in self.security.get("dropped") or []]
        for ref, f in self.settled_findings(("accept", "transfer")):
            items.append(f"**{ref}**, a {f['severity']} security risk {f['response']}ed: {f['summary']}")
        return items

    def settled_findings(self, responses: tuple[str, ...]) -> list[tuple[str, dict]]:
        """Findings settled with one of these responses, by every review. A later
        review leaves what an earlier one settled alone, so earlier ones carry forward
        unless the latest review re-opened or restated them."""
        reopened = {f["reopens"]["ref"] for f in self.security["findings"] if f.get("reopens")}
        reopened |= {f["keeps"] for f in self.security["findings"] if f.get("keeps")}
        last = len(self.reviews)
        out = []
        for n, review in enumerate(self.reviews, 1):
            for f in review.get("findings", []):
                ref = f["id"] if n == last else f"review {n} {f['id']}"
                if f["response"] in responses and (n == last or ref not in reopened):
                    out.append((ref, f))
        return out

    def limitations(self) -> list[str]:
        """Known product limitations the designer should knowingly accept (D-71)."""
        items = [f"{l['summary']} *(source: {l['source']})*" for l in self.design.get("limitations", [])]
        items += [f"{f['summary']} *(from security {ref if ref.startswith('review') else 'review ' + ref})*"
                  for ref, f in self.settled_findings(("limitation",))]
        return items

    def needed_lines(self) -> list[str]:
        """Infrastructure other teams build (D-92): one plain line each, with its request."""
        out = []
        for eid, e in self.external.items():
            owner = "owner unknown: find out who" if e["owner"] == "unknown" else e["owner"]
            status = self.delivery_status(eid)
            status = status[0].upper() + status[1:] + (". Send the request now" if eid not in self.deliveries else "")
            out.append(f"**{eid}** {e['summary']} ({owner}). {status}. "
                       f"Needed before {self.needed_before(eid)}. "
                       f"Request: [{self.request_file(eid)}]({self.request_file(eid)})")
        return out

    def request_md(self, eid: str) -> str:
        """A self-contained, tool-neutral brief for the team that builds the dependency."""
        e = self.external[eid]
        users, gated = self.users_of(eid), self.gated_by(eid)
        reqs = self.security_by_pr.get(eid, []) + self.consider_by_pr.get(eid, [])
        after = ", ".join(f"{d} ({self.external[d]['summary']})" for d in e.get("after") or []) or "nothing else"
        early = [pid for pid in users if pid not in gated]
        out = [f"# Request {eid}: {e['summary']}", "",
               f"For the team that runs this infrastructure ({e['owner']}). It's needed for the feature "
               f"\"{self.intent['title']}\" in `{self.repo['name']}`. This describes what the app needs, "
               "not how to build it: use your own tools and conventions.", "",
               f"- **Status:** {self.delivery_status(eid)}",
               f"- **Already exists?** {e['exists']}",
               f"- **Environments:** {', '.join(e['environments'])}",
               f"- **Must come after:** {after}",
               f"- **Needed before:** {self.needed_before(eid)}"
               + (f" (code that uses it but can merge before it's ready: {', '.join(early)})" if early else ""), "",
               "## What it must do", "", e["needs"], "", "## Why", "", e["why"], "",
               "## Settings the app will read", ""]
        if e["parameters"]:
            out += ["| Setting | What it's for | Example |", "|---|---|---|"]
            out += [f"| `{x['name']}` | {x['purpose']} | {('`' + x['example'] + '`') if x.get('example') else '—'} |"
                    for x in e["parameters"]]
        else:
            out.append("None.")
        out += ["", "## Security requirements", "",
                _bullets([f"**{f['id']}** ({f['severity']}): {f['requirement']} ({f['citation']})" for f in reqs]),
                "", "## Open questions for you", "",
                "Your answers may change the plan, so send them back with the details below.", "",
                _bullets(e["open_questions"]), "",
                "## How to confirm it's ready", "", e["ready_when"], "",
                "## When it's built, send back", "",
                "- the final setting names and example values (never real secrets)",
                "- anything that differs from this request, and the answers to the open questions",
                "- limits, quotas or costs the app should respect", "",
                "The designer records this with `gandalf deliver " + eid + "`; differences re-plan the work "
                "that hasn't been built yet.", ""]
        return "\n".join(out)

    def adopt_lines(self) -> list[str]:
        """Other teams' apps that can adopt the feature (D-127): one plain line each, with its handoff."""
        out = []
        for eid, e in self.clients.items():
            owner = "owner unknown: find out who" if e["owner"] == "unknown" else e["owner"]
            _, switches = self.available_after(eid)
            when = f"Usable once {', '.join(switches)} is switched on" if switches else "Usable once the plan is built"
            out.append(f"**{eid}** {e['summary']} ({owner}). {when}; nothing waits for it. "
                       f"Until then: {e['until_then']} Handoff: [{self.request_file(eid)}]({self.request_file(eid)})")
        return out

    def handoff_md(self, eid: str) -> str:
        """A self-contained brief for a team whose app can adopt what this plan builds (D-127)."""
        e = self.clients[eid]
        exposers, switches = self.available_after(eid)
        reqs = self.security_by_pr.get(eid, []) + self.consider_by_pr.get(eid, [])
        out = [f"# Handoff {eid}: {e['summary']}", "",
               f"For the team behind this app ({e['owner']}). The feature \"{self.intent['title']}\" in "
               f"`{self.repo['name']}` adds what's below. Nothing in that plan waits for your app: this is what "
               "you need to adopt it when you choose, with your own tools and conventions.", "",
               f"- **Available once:** {', '.join(exposers) or 'the plan is built'} "
               f"{'has' if len(exposers) == 1 else 'have'} merged"
               + ("; that switches it on for users" if switches and set(switches) <= set(exposers) else
                  f", and users see it once {', '.join(switches)} switches it on" if switches else ""), "",
               "## What to adopt", "", e["needs"], "", "## Why", "", e["why"], "",
               "## What your users see until then", "", e["until_then"], "",
               "## Contracts", "", "Quoted exactly as the plan's PRs build them.", ""]
        for cid in e["contracts"]:
            c = self.contracts[cid]
            out += [f"### {cid}: {c['name']}", "", c["why"], "", _code(c["body"], c["language"]), ""]
        out += ["## Security requirements", "",
                _bullets([f"**{f['id']}** ({f['severity']}): {f['requirement']}"
                          + (f" ({f['citation']})" if f.get("citation") else "") for f in reqs]),
                "", "## Open questions for you", "", _bullets(e["open_questions"]), ""]
        return "\n".join(out)

    def change_lines(self) -> list[str]:
        """The change history (§5.11): what monitoring found on each branch and what became of it."""
        outcome = {"absorbed": "updated its own spec automatically", "approved": "approved by the designer",
                   "proposed": "waiting for the designer", "rejected": "rejected: the builder goes back to the spec",
                   "revert": "breaks a rule: the builder reverts it", "harmless": "nothing that changes the plan",
                   "unfinished": "not finished: the builder has a note, the plan is unchanged"}
        out = []
        for e in self.changes:
            line = f"**{e['id']}** {report_time(e['at'])}, {e['pr']} (`{e['branch']}` at `{e['commit'][:10]}`): {e.get('summary', '')}"
            line += f" — {outcome.get(e['status'], e['status'])}"
            reach = e.get("reach") or {}
            if reach.get("prs"):
                line += f"; reaches {', '.join(reach['prs'])}"
            applied = e.get("applied") or {}
            if applied.get("prs") or applied.get("contracts"):
                line += f"; changed {', '.join(applied.get('prs', []) + applied.get('contracts', []))}"
            if applied.get("replanned_from"):
                line += "; re-planned from the design"
            if e.get("notes"):
                line += f". Notes: {e['notes']}"
            out.append(line)
        return out

    def slice_map(self) -> list[str]:
        out = ["| # | Type | Layer / capability switched on | Depends on |", "|---|---|---|---|"]
        for pid in self.order:
            pr = self.prs[pid]
            what = pr.get("layer") if pr["type"] == "inert" else (pr.get("capability") or "precursor fix")
            out.append(f"| [{pid}]({self.pr_file(pid)}) | {pr['type']} | {what} | {', '.join(pr['depends_on']) or '—'} |")
        out.append("")
        enabling = [pid for pid in self.order if self.prs[pid]["type"] == "enabling"]
        if enabling:
            out.append("### Vertical view: what each enabling step switches on")
            out.append("")
            closure = checks.transitive_deps(self.sequence["prs"])
            for pid in enabling:
                inert = [d for d in self.order if d in closure.get(pid, set()) and self.prs[d]["type"] == "inert"]
                out.append(f"- {pid} switches on *{self.prs[pid]['capability']}*, built by: {', '.join(inert) or 'None'}")
            out.append("")
        layers = [l for l in LAYER_ORDER if any(self.leaves[lid]["layer"] == l
                                               for pr in self.sequence["prs"] for lid in pr["leaves"])]
        out.append("### Layers touched")
        out.append("")
        out.append("| Layer | " + " | ".join(self.order) + " |")
        out.append("|---|" + "---|" * len(self.order))
        for layer in layers:
            cells = ["●" if any(self.leaves[lid]["layer"] == layer for lid in self.prs[pid]["leaves"]) else ""
                     for pid in self.order]
            out.append(f"| {layer} | " + " | ".join(cells) + " |")
        out.append("")
        return out

    def build_grid(self) -> list[str]:
        """The build order at a glance (D-150). Rows are lanes, chains one builder can take end
        to end; columns are waves, everything in a wave can start once the previous wave has
        merged. Other teams' work takes its own row, in the wave of the first PR that waits."""
        waves, lanes = self.derived["waves"], self.derived["lanes"]
        out = ["| Lane | " + " | ".join(f"Wave {w}" for w in range(1, len(waves) + 1)) + " |",
               "|---|" + "---|" * len(waves)]
        for n, lane in enumerate(lanes, 1):
            cells = [""] * len(waves)
            for pid in lane:
                cells[self.wave_of[pid] - 1] = pid
            out.append(f"| {n} | " + " | ".join(cells) + " |")
        teams = [""] * len(waves)
        for eid in self.external:
            waits = ([(self.wave_of[p], p, "waits") for p in self.gated_by(eid)]
                     + [(self.wave_of[p], p, "settings wait") for p in self.switch_gated_by(eid)])
            if waits:
                w, p, how = min(waits)
                cell = f"{eid} ready ({p} {how})"
                teams[w - 1] = f"{teams[w - 1]}; {cell}" if teams[w - 1] else cell
        if any(teams):
            out.append("| Other teams | " + " | ".join(teams) + " |")
        out += ["", f"Rows are lanes: a chain of {self.unit}s one builder can take end to end. Columns are "
                    "waves: everything in a wave can start once the previous wave has merged."]
        crossing = []
        for n, lane in enumerate(lanes, 1):
            for pid in lane:
                others = sorted((d for d in self.prs[pid]["depends_on"] if d in self.prs and self.lane_of[d] != n),
                                key=self.order.index)
                if others:
                    crossing.append(f"{pid} (lane {n}) also waits for "
                                    + ", ".join(f"{d} (lane {self.lane_of[d]})" for d in others) + ".")
        if crossing:
            out += [""] + [f"- {c}" for c in crossing]
        out.append("")
        for pid, needs in self.derived.get("switch_gates", {}).items():
            settings = ", ".join(f"`{x}`" for x in self.prs[pid]["switched_on_by"]["settings"])
            out += [f"{pid} is switched on by configuration ({settings})"
                    + (f" once {', '.join(needs)} {'is' if len(needs) == 1 else 'are'} ready." if needs else "."), ""]
        return out

    def sequence_lines(self) -> list[str]:
        out = []
        # one suggested review point per capability, after the last PR that switches it on (D-85)
        last_for_capability = {}
        for pid in self.order:
            if self.prs[pid]["type"] == "enabling":
                last_for_capability[self.prs[pid].get("capability")] = pid
        review_after = set(last_for_capability.values())
        announced: set[str] = set()
        for i, pid in enumerate(self.order, 1):
            pr = self.prs[pid]
            gates = self.derived["gates"].get(pid, [])
            switch = self.derived.get("switch_gates", {}).get(pid, [])
            waits = f" **Blocked until {', '.join(gates)} {'is' if len(gates) == 1 else 'are'} ready.**" if gates else ""
            out.append(f"{i}. [{pid}]({self.pr_file(pid)}) **{pr['title']}** — {pr['type']}, "
                       f"{self.derived['bands'][pid]}, lane {self.lane_of[pid]}, wave {self.wave_of[pid]}. {pr['summary']}{waits}")
            # another team's work holds this back: say so where the order is read
            for eid in gates + switch:
                e = self.external[eid]
                what = ("can't merge until it's ready" if eid in gates else
                        "can merge, but its settings are only set once it's ready")
                if eid in announced:
                    out.append(f"    - **Blocker {eid}** (see above): {pid} {what}.")
                    continue
                announced.add(eid)
                owner = "owner unknown: find out who" if e["owner"] == "unknown" else e["owner"]
                out.append(f"    - **Blocker {eid}, another team's work:** {e['summary']} ({owner}). {pid} {what}. "
                           f"Status: {self.delivery_status(eid)}. Ready when: {e['ready_when'].rstrip('.')}. "
                           f"Request: [{self.request_file(eid)}]({self.request_file(eid)})")
            if pid in review_after:
                out.append(f"    - *A natural point for your own review: after {pid}, "
                           f"\"{pr['capability']}\" can be tried end to end.* (Never blocks the plan.)")
        return out

    def intent_lines(self) -> list[str]:
        it = self.intent

        def src(item):
            ref = f" ({item['ref']})" if item.get("ref") else ""
            return f"*[{item['source'].replace('_', ' ')}{ref}]*"

        out = ["### Problem", "", f"{it['problem']['text']} {src(it['problem'])}", "", "### Outcomes", ""]
        out += [f"- **{o['id']}** {o['text']} {src(o)}" for o in it["outcomes"]]
        for title, key in (("Out of scope", "out_of_scope"), ("Behaviour examples", "behaviours"),
                           ("Constraints", "constraints"), ("Decisions already made", "decisions"),
                           ("Known unknowns", "unknowns")):
            out += ["", f"### {title}", ""]
            items = it[key]
            out += [f"- {'**' + i['id'] + '** ' if 'id' in i else ''}{i['text']} {src(i)}" for i in items] or ["None."]
        live, retired = _live_assumptions(it, self.design)
        out += ["", "### Assumptions made for you", "", _bullets(_assumption_lines(live)), ""]
        if retired:
            why = {r["text"]: r["why"] for r in self.design.get("retired_assumptions") or []}
            out += ["Replaced by later decisions:", ""] + [f"- ~~{a['text']}~~ *({why[a['text']]})*" for a in retired] + [""]
        return out

    def repo_context_lines(self) -> list[str]:
        r, s = self.repo, self.scan
        out = [f"- **Repo:** `{r['name']}` ({'git, commit ' + (r['commit'] or '?')[:12] if r['vcs'] == 'git' else 'not a git repository'})"]
        if r.get("dirty"):
            out.append("- **Note:** the working tree had uncommitted changes when scanned.")
        if s.get("workspaces"):
            out += ["", "### Workspace packages", ""]
            out += [f"- `{w['path']}` ({w['kind']}): frameworks {', '.join(w['frameworks']) or 'UNKNOWN'}; "
                    f"tests {', '.join('`' + c + '`' for c in w['test_commands']) or 'UNKNOWN'}"
                    for w in s["workspaces"]]
        out += ["", "### Stack (scanner facts)", ""]
        for key, label in (("languages", "Languages"), ("frameworks", "Frameworks"), ("orm", "ORM / data"),
                           ("test_runner", "Test runner"), ("package_manager", "Package manager"),
                           ("run_commands", "Run commands"), ("test_commands", "Test commands")):
            facts = s["stack"][key]
            val = "; ".join(f"{f['value']} [{', '.join(f['evidence'])}]" for f in facts) or "UNKNOWN"
            out.append(f"- **{label}:** {val}")
        out += ["", "### Layers", ""]
        for layer, facts in s["layers"].items():
            out.append(f"- **{layer}:** {', '.join('`' + f['value'] + '`' for f in facts) or 'UNKNOWN'}")
        out += ["", "### Repo rules", "", _bullets(self.repo_rules()), "", "### Naming", ""]
        out.append(_bullets([f"{n['language']}: {n['file_case']} ({n['share']}), e.g. `{n['example']}`"
                             for n in s["naming"]["files"]]
                            + [f"Tests: `{t['pattern']}` ({t['count']}), e.g. `{t['example']}`"
                               for t in s["naming"]["test_files"]]))
        c = self.context
        out += ["", "### Confirmed and added by the planning agent (with evidence)", ""]
        out.append(_bullets([f"Confirmed: {x['fact']} [{', '.join(x['evidence'])}]" for x in c["confirmed"]]
                            + [f"Added ({x['area']}): {x['claim']} [{', '.join(x['evidence'])}]" for x in c["added"]]
                            + [f"Test command: `{x['command']}` [{', '.join(x['evidence'])}]" for x in c["test_commands"]]
                            + [f"Run command: `{x['command']}` [{', '.join(x['evidence'])}]" for x in c["run_commands"]]))
        out += ["", "### Contradictions between the scanner and the code", ""]
        out.append(_bullets([f"Scanner: {x['scanner_fact']} — Observed: {x['observed']} [{', '.join(x['evidence'])}]"
                             for x in c["contradictions"]]))
        out += ["", "### How an existing feature is wired", ""]
        ft = c.get("feature_trace")
        out.append(f"Traced: **{ft['feature']}**\n\n" + "\n".join(f"- {f['layer']}: `{f['path']}` — {f['role']}" for f in ft["files"])
                   if ft else "None traced.")
        out += ["", "### Conventions", "", _bullets([f"{x['rule']} — e.g. `{x['example']}`" for x in c["conventions"]]),
                "", "### Unknowns from the scan", "", _bullets([f"{u['area']}: {u['what']}" for u in s["unknowns"]]), ""]
        return out

    def design_lines(self) -> list[str]:
        d = self.design
        out = [d["summary"], "", "### Decisions", ""]
        for dec in d["decisions"]:
            status = dec["status"] + (f" → {dec['superseded_by']}" if dec.get("superseded_by") else "")
            out += [f"#### {dec['id']} ({status}): {dec['summary']}", "",
                    f"- **Context:** {dec['context']}", f"- **Decision:** {dec['decision']}",
                    f"- **Source:** {dec['source']['kind']} — {dec['source']['ref']}",
                    f"- **Alternatives considered:** {'; '.join(dec['alternatives']) or 'None'}",
                    f"- **Consequences:** {dec['consequences']}", ""]
        if not d["decisions"]:
            out += ["None.", ""]
        out += ["### Data model changes", "",
                _bullets([f"{x['change']} [{', '.join(x['where']) or 'new'}]" for x in d["data_model"]]), "",
                "### Components", "", _bullets([f"**{x['name']}** ({x['layer']}): {x['responsibility']}" for x in d["components"]]),
                "", "### Contracts", ""]
        for c in d["contracts"]:
            out += [f"#### {c['id']}: {c['name']} ({c['kind']})", "", _code(c["body"], c["language"]), "", f"Why: {c['why']}", ""]
        if not d["contracts"]:
            out += ["None.", ""]
        out += ["### How it fits the existing wiring", "", d["wiring"], "",
                "### Inventory of what's new", "",
                _bullets([f"**{n['id']}** {n['kind'].replace('_', ' ')}: {n['description']}"
                          + (f" ({n['contract']})" if n.get("contract") else "") for n in d["inventory"]]), "",
                "### Precursors", ""]
        for x in d["precursors"]:
            ev = "; ".join(f"[{b['kind']}] {b['text']}" + (f" ({b['cite']})" if b.get("cite") else "") for b in x["evidence"])
            verified = "verified by running tests" if x["verified_by_running"] else "not verified by running"
            out.append(f"- **{x['id']}** `{x['where']}` — {x['what']} Blocks: {x['why_blocks']} "
                       f"Placement: {x['placement']} the work. Evidence: {ev} ({verified})")
        if not d["precursors"]:
            out.append("None.")
        out += ["", "### External dependencies (built by other teams)", "",
                _bullets([f"**{e['id']}** {e['summary']}: {e['needs']} Owner: {e['owner']}; environments: "
                          f"{', '.join(e['environments'])}; source: {e['source']['kind']} — {e['source']['ref']}. "
                          f"See [{self.request_file(e['id'])}]({self.request_file(e['id'])})"
                          for e in self.external.values()])]
        if self.clients:
            out += ["", "### Client apps in other repos (adopt this when they choose)", "",
                    _bullets([f"**{e['id']}** {e['summary']}: {e['needs']} Contracts: {', '.join(e['contracts'])}. "
                              f"Until then: {e['until_then']} Owner: {e['owner']}; source: {e['source']['kind']} — "
                              f"{e['source']['ref']}. See [{self.request_file(e['id'])}]({self.request_file(e['id'])})"
                              for e in self.clients.values()])]
        out += ["", "### Assumptions made for you", "", _bullets(_assumption_lines(d.get("assumptions") or []))]
        out += ["", "### Known limitations", "",
                _bullets([f"**{l['summary']}** {l['detail']} *(source: {l['source']})*" for l in d.get("limitations", [])])]
        out += ["", "### Suspected, not confirmed", "",
                _bullets([f"`{s['where']}` — {s['what']} (Why unconfirmed: {s['why_unconfirmed']})" for s in d["suspected"]]),
                "", "### Capabilities", "", _bullets([f"**{c['name']}**: {c['description']}" for c in d["capabilities"]]),
                "", "### Decomposition", ""]
        for comp in self.decompose["components"]:
            out.append(f"- **{comp['id']}. {comp['name']}**")
            for leaf in comp["leaves"]:
                risk = f" ⚠ {', '.join(leaf['risk'])}" if leaf["risk"] else ""
                out.append(f"  - **{leaf['id']}** [{leaf['layer']}] {leaf['deliverable']} → {leaf['outcome']} "
                           f"(traces {', '.join(leaf['traces'])}){risk}")
        out += ["", "### Slice map", ""] + self.slice_map()
        return out

    def security_lines(self) -> list[str]:
        s = self.security
        out = ["A lightweight review for obvious flaws, not a full threat model.", "",
               "### What was checked", ""]
        out.append(_bullets([f"**{c['inventory']}**: " + "; ".join(c["questions"]) for c in s["checked"]]))
        out += ["", "### Abuse cases", "", _bullets([f"**{a['capability']}**: {a['case']}" for a in s["abuse_cases"]]),
                "", "### Findings", ""]
        if s["findings"]:
            out += ["| ID | Severity | In short | Response | Outcome |", "|---|---|---|---|---|"]
            applied = {f["id"] for f in self.applied}
            for f in s["findings"]:
                if f["id"] in applied:
                    outcome = f"Applied to {f['target_pr']}: {f['requirement']}"
                elif f.get("design_change"):
                    outcome = f"Design changed: {f['design_change']}"
                elif f["response"] == "limitation":
                    outcome = f"Known limitation: {f.get('reason')}"
                elif f["response"] in ("accept", "transfer"):
                    outcome = f"{f['response'].capitalize()}ed: {f.get('reason')}"
                else:
                    outcome = f"Considered, not applied: {f.get('reason') or 'below medium severity'}"
                cite = f" ({f['citation']})" if f.get("citation") else ""
                out.append(f"| {f['id']} | {f['severity']} | {f['summary']} | {f['response']} | {outcome}{cite} |")
            out += ["", "### Detail", ""]
            out += [f"- **{f['id']}** ({f['concerns']}): {f['issue']}"
                    + (f" *Re-opens {f['reopens']['ref']}: {f['reopens']['why_now']}*" if f.get("reopens") else "")
                    + (f" *Keeps {f['keeps']}.*" if f.get("keeps") else "")
                    for f in s["findings"]]
        else:
            out.append("None.")
        if s.get("dropped"):
            out += ["", "### Dropped from the earlier review", ""]
            out += [f"- **{d['ref']}**: {d['reason']}" for d in s["dropped"]]
        if self.boundary:
            out += ["", "### Between PRs", "",
                    "A separate check of what's true once each PR has merged, after the PRs were planned:", ""]
            out += [f"- **After {c['after']}:** {c['state']}" for c in self.boundary["checked"]]
            carried = self.carried_gaps
            if carried:
                out += ["", "Gaps the designer carried instead of replanning (D-180); each is noted on the PR it follows:", ""]
                out += [f"- **{f['id']} after {f['after']}:** {f['summary']}. The check asked: {f['replan']} "
                        f"Designer's notes: {f['carried']['notes'] or 'none'}." for f in carried]
        out.append("")
        return out

    def sequence_doc_lines(self, include_order: bool = True) -> list[str]:
        out = (["### Order", ""] + self.sequence_lines() + [""]) if include_order else []
        out += ["### Lanes and waves", ""] + self.build_grid()
        out += ["### Order of operations", ""]
        out.append("\n".join(f"{i}. After {s['after']} — **{s['kind']}**"
                             + (f" ({s['external']})" if s.get("external") else "") + f": {s['step']}"
                             for i, s in enumerate(self.sequence["order_of_operations"], 1)) or "None beyond merge order.")
        out += ["", "### In-between states", "",
                "What's true once each PR has merged, and whether every decision still holds then:", ""]
        out += [f"- **After {b['after']}:** {b['state']}"
                + ("" if b["decisions_hold"] else f" ⚠ **Gap:** {b.get('gap')}")
                for b in self.sequence.get("boundaries", [])]
        out += ["", "### Breaking changes and mitigations", ""]
        out.append(_bullets([f"**{p['id']}**: {p['breaking']['detail']}" for p in self.sequence["prs"] if p["breaking"]["is_breaking"]]))
        out += ["", "### Expand / contract pairs", ""]
        out.append(_bullets([f"{p['id']} expands; {p['expand_contract']['pair']} contracts"
                             for p in self.sequence["prs"] if (p.get("expand_contract") or {}).get("role") == "expand"]))
        out += ["", "### Flags", ""]
        out.append(_bullets([f"`{p['flag']['name']}`: introduced by {p['id']}, removed by {p['flag']['removed_by']}"
                             for p in self.sequence["prs"] if p.get("flag")]))
        out.append("")
        return out

    def notes_lines(self) -> list[str]:
        r = self.report
        out = ["### Check notes", ""]
        out.append(_bullets([f"Warning: {w}" for w in r.warnings] + [f"Note: {n}" for n in r.notes]))
        om = self.plan.meta.get("omissions", [])
        out += ["", "### Context left out of prompts", ""]
        out.append(_bullets([f"{o['stage']}: `{o['path']}` ({o['reason']})" for o in om]))
        mm = self.plan.meta.get("mismatches", [])
        out += ["", "### Repairs", ""]
        out.append(_bullets([f"{report_time(x['at'])}: {x['pr']} — expected: {x['expected']}; found: {x['found']} "
                             f"(re-planned from {x['from_stage']}; merged and frozen: {', '.join(x['merged']) or 'none'})"
                             for x in mm]))
        m = self.plan.metrics()
        passes = m.get("passes_by_stage", {})
        repeated = [f"{k} ×{v['count']} ({'; '.join(dict.fromkeys(v['reasons'])) or 'no reason recorded'})"
                    for k, v in passes.items() if v["count"] > 1]
        counts = ", ".join(f"{stage} ×{info['count']}" for stage, info in passes.items()) or "none recorded"
        out += ["", "### Stage passes", "",
                "How many times each stage was produced, and why it had to run again:", "",
                f"- {counts}"]
        out += [f"- Re-runs: {r}" for r in repeated] or ["- Every stage ran once."]
        out += ["", "### Mistakes caught during planning", ""] + diagnose.render_lines(diagnose.summarise(self.plan.log_entries()))
        out += ["", "### Planning metrics", "",
                f"- Questions asked: {m['questions_asked']} ({', '.join(f'{k}: {v}' for k, v in m['questions_by_stage'].items()) or 'none'})"
                + (f"; {m['questions_withdrawn']} more withdrawn before being answered" if m.get("questions_withdrawn") else ""),
                f"- Re-asks after a later stage found something new: {m['reasks']}",
                f"- Review rounds (D-180): the security review sent the design back {m['rounds']['design_from_security']} "
                f"time(s); the boundary check sent the plan back {m['rounds']['replan_from_boundary']} time(s)",
                f"- Proposals accepted after \"don't know\": {m['answered_unknown_then_proposed']}",
                f"- Pauses: {m['pauses']}", f"- Mismatches repaired: {m['mismatches']}", ""]
        return out

    def technical_md(self) -> str:
        """Light tier: the technical documents in one file, so plan.md stays plain."""
        out = [f"# Technical detail: {self.intent['title']}", ""] + self.banner()
        out += ["## Intent", ""] + self.intent_lines()
        out += ["## Repo context", ""] + self.repo_context_lines()
        out += ["## Design", ""] + self.design_lines()
        out += ["## Security review", ""] + self.security_lines()
        out += ["## Order of operations", ""] + self.sequence_doc_lines(include_order=False)
        return "\n".join(out)

    def plan_md(self) -> str:
        title = self.intent["title"]
        out = [f"# {title}", ""] + self.banner()
        out += [f"*Generated by GANDALF {__version__} on {report_time()} — {self.tier} tier — "
                f"{len(self.order)} {self.unit}s.*", ""]
        out += ["## Summary", "", self.intent["problem"]["text"], "", self.design["summary"], ""]
        out += ["## Known limitations", "", _bullets(self.limitations()), ""]
        out += ["## Needed from other teams", "", _bullets(self.needed_lines()), ""]
        if self.clients:
            out += ["## For other teams to adopt", "", _bullets(self.adopt_lines()), ""]
        out += ["## Decisions needing attention", "",
                "One line each; the full reasoning is in "
                + ("[design.md](design.md) and [security-review.md](security-review.md)." if self.tier == "full"
                   else "[technical.md](technical.md)."), "",
                _bullets(self.attention()), ""]
        out += ["## Review these first", "", _bullets(self.review_first()), ""]
        out += ["## Slice map", ""] + self.slice_map()
        out += ["## Sequence", ""] + self.build_grid() + self.sequence_lines() + [""]
        out += ["## Risks and security", ""]
        applied = [f"{f['id']} → {f['target_pr']}: {f['summary']}" for f in self.applied]
        out.append(_bullets(applied + [f"{f['id']} {f['response']}ed: {f['summary']}" for f in self.security["findings"]
                                       if f["response"] in ("accept", "transfer")]))
        out.append("")
        if self.changes:
            out += ["## Change history", "", _bullets(self.change_lines()), ""]
        out += ["## Notes", ""] + self.notes_lines()
        out += ["## Files", ""]
        files = [f"[{self.pr_file(pid)}]({self.pr_file(pid)})" for pid in self.order]
        if self.tier == "full":
            files = ["[intent-brief.md](intent-brief.md)", "[repo-context.md](repo-context.md)",
                     "[design.md](design.md)", "[security-review.md](security-review.md)",
                     "[sequence.md](sequence.md)"] + files
        else:
            files = ["[technical.md](technical.md) — intent, repo context, design, security review, order of operations"] + files
        files += [f"[{self.request_file(e)}]({self.request_file(e)})" for e in [*self.external, *self.clients]]
        out.append(_bullets(files + ["[plan.json](plan.json)"]))
        out += ["", "Builder sessions need read access to this plan folder, e.g. `claude --add-dir <this folder>`.", ""]
        return "\n".join(out)

    def plan_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "generator": f"gandalf {__version__}",
            "generated": now(),
            "tier": self.tier,
            "vcs": self.repo["vcs"],
            "repo": {k: v for k, v in self.repo.items() if k != "path"},     # no paths from this machine
            "feature": self.intent["title"],
            "intent": {k: v for k, v in self.intent.items() if k not in ("questions", "proposals", "pauses", "verify")},
            "design": {k: v for k, v in self.design.items() if k not in ("questions", "proposals", "pauses", "verify")},
            "decomposition": self.decompose["components"],
            "prs": [{**self.prs[pid],
                     "file": self.pr_file(pid),
                     "lane": self.lane_of[pid],
                     "wave": self.wave_of[pid],
                     "needed_by": self.derived["needed_by"][pid],
                     "size_band": self.derived["bands"][pid],
                     "risk": self.derived["risk"][pid],
                     "path_tags": {ch["path"]: self.tag(pid, ch["path"]) for ch in self.prs[pid]["changes"]},
                     "security": [{"finding": f["id"], "requirement": f["requirement"]}
                                  for f in self.security_by_pr.get(pid, [])]}
                    for pid in self.order],
            "order": self.order,
            "lanes": self.derived["lanes"],
            "waves": self.derived["waves"],
            "order_of_operations": self.sequence["order_of_operations"],
            "boundaries": self.sequence.get("boundaries", []),
            "external": [{**e, "request": self.request_file(eid), "used_by": self.users_of(eid),
                          "gates": self.gated_by(eid), "switch_on_gates": self.switch_gated_by(eid),
                          "delivery": self.deliveries.get(eid)}
                         for eid, e in self.external.items()],
            "change_history": [{k: e.get(k) for k in ("id", "at", "pr", "branch", "commit", "status", "summary",
                                                       "reach", "applied", "notes", "note")} for e in self.changes],
            "clients": [{**e, "handoff": self.request_file(eid),
                         "available_after": dict(zip(("merged", "switched_on"), self.available_after(eid)))}
                        for eid, e in self.clients.items()],
            "security": {k: v for k, v in self.security.items() if k not in ("questions", "proposals", "pauses", "verify")},
            "questions": self.plan.answers["questions"],
            "pauses": self.plan.answers["pauses"],
            "metrics": self.plan.metrics(),
            "how_planning_went": diagnose.summarise(self.plan.log_entries()),
            "checks": {"warnings": self.report.warnings, "notes": self.report.notes},
            "omissions": self.plan.meta.get("omissions", []),
            "frozen": [p["id"] for p in self.plan.meta.get("frozen", [])],
            "mismatches": self.plan.meta.get("mismatches", []),
        }


def render_plan(plan: PlanState) -> Rendered:
    p = Planner(plan)
    root = plan.plan_dir
    result = Rendered(tier=p.tier, report=p.report)
    prs_dir = root / "prs"
    if prs_dir.exists():
        for old in prs_dir.glob("pr-*.md"):
            old.unlink()
    prs_dir.mkdir(parents=True, exist_ok=True)

    requests_dir = root / "requests"
    if requests_dir.exists():
        for old in requests_dir.glob("e-*.md"):
            old.unlink()
    if p.external:
        requests_dir.mkdir(parents=True, exist_ok=True)
        for eid in p.external:
            path = root / p.request_file(eid)
            path.write_text(p.request_md(eid))
            result.files.append(path)
    handoffs_dir = root / "handoffs"
    if handoffs_dir.exists():
        for old in handoffs_dir.glob("e-*.md"):
            old.unlink()
    if p.clients:
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        for eid in p.clients:
            path = root / p.request_file(eid)
            path.write_text(p.handoff_md(eid))
            result.files.append(path)

    for pid in p.order:
        text = p.pr_spec(pid)
        path = root / p.pr_file(pid)
        path.write_text(text)
        result.files.append(path)

    docs = {}
    if p.tier == "full":
        docs = {
            "intent-brief.md": "\n".join([f"# Intent brief: {p.intent['title']}", ""] + p.banner() + p.intent_lines()),
            "repo-context.md": "\n".join(["# Repo context", ""] + p.banner() + p.repo_context_lines()),
            "design.md": "\n".join([f"# Design: {p.intent['title']}", ""] + p.banner() + p.design_lines()),
            "security-review.md": "\n".join(["# Security review", ""] + p.security_lines()),
            "sequence.md": "\n".join(["# Sequence", ""] + p.banner() + p.sequence_doc_lines()),
        }
    else:
        docs = {"technical.md": p.technical_md()}
    stale_names = (("technical.md",) if p.tier == "full" else
                   ("intent-brief.md", "repo-context.md", "design.md", "security-review.md", "sequence.md"))
    for name in stale_names:
        if (root / name).exists():
            (root / name).unlink()
    docs["plan.md"] = p.plan_md()
    for name, text in docs.items():
        (root / name).write_text(text)
        result.files.append(root / name)
    (root / "plan.json").write_text(json.dumps(p.plan_json(), indent=2, ensure_ascii=False) + "\n")
    result.files.append(root / "plan.json")
    preview = root / "design-for-review.md"
    if preview.exists():
        preview.unlink()
    for stale in ("plan.pdf", "plan.html"):              # written by versions before 0.18 (D-184)
        if (root / stale).exists():
            (root / stale).unlink()
    plan.meta.pop("format", None)
    plan.save()
    return result


def _review_lines(security: dict | None) -> list[str]:
    """What the security review of this design asks for, shown at the checkpoint (D-149)."""
    if not security:
        return []
    required = {f["id"] for f in checks.applied_findings(security)}
    out = ["", "## Security review", "",
           "A separate reviewer checked this design before you see it. Fixes are built into the PRs.", ""]
    rows = []
    for f in security["findings"]:
        if f.get("design_change"):
            continue                    # already applied: the design above includes it
        if f.get("carried"):
            rows.append(f"- **{f['id']} (required fix, carried by you in round {f['carried']['round']} instead of a "
                        f"design change):** {f['summary']}. {f['requirement']}")
        elif f["id"] in required and f.get("requirement"):
            rows.append(f"- **{f['id']} (required fix):** {f['summary']}. {f['requirement']}")
        elif f["response"] in ("mitigate", "eliminate") and f.get("requirement"):
            rows.append(f"- **{f['id']} (to consider):** {f['summary']}. {f['requirement']}")
        elif f["response"] == "limitation":
            rows.append(f"- **{f['id']} (known limitation):** {f['summary']}. {f.get('reason', '')}")
        elif f["response"] in ("accept", "transfer"):
            rows.append(f"- **{f['id']} ({f['response']}ed risk):** {f['summary']}. {f.get('reason', '')}")
    return out + (rows or ["Nothing to add."])


def render_design_preview(plan: PlanState) -> Path:
    """Write design.md for the design checkpoint (§6.1), before the plan is finished."""
    design, intent = plan.stage("design"), plan.stage("intent")
    lines = [f"# Design for review: {intent['title']}", "",
             "*Draft for the design checkpoint. Approve it, or say what to change.*", "", design["summary"], ""]
    lines += ["## Known limitations", ""] + ([f"- **{l['summary']}** {l['detail']}" for l in design.get("limitations", [])] or ["None."])
    # current decisions only; replaced ones go to a short history at the end (D-83)
    current = [d for d in design["decisions"] if d["status"] != "superseded"]
    replaced = [d for d in design["decisions"] if d["status"] == "superseded"]
    lines += ["", "## Needed from other teams", ""]
    needed = [e for e in design.get("external") or [] if not checks.is_client(e)]
    for e in needed:
        settings = ", ".join(f"`{x['name']}`" for x in e["parameters"]) or "none"
        lines += [f"- **{e['id']}: {e['summary']}** {e['needs']}",
                  f"  - Owner: {e['owner']}; already exists: {e['exists']}; environments: {', '.join(e['environments'])}",
                  f"  - Settings the app will read: {settings}",
                  f"  - Still to be settled by its owners: {'; '.join(e['open_questions']) or 'nothing'}"]
    if not needed:
        lines.append("None.")
    clients = [e for e in design.get("external") or [] if checks.is_client(e)]
    if clients:
        lines += ["", "## Other teams' apps that can adopt this", "",
                  "Nothing in the plan waits for these; each team gets a handoff.", ""]
        lines += [f"- **{e['id']}: {e['summary']}** {e['needs']} Until then: {e['until_then']} "
                  f"(owner: {e['owner']}; contracts: {', '.join(e['contracts'])})" for e in clients]
    assumed = ([("intent", a) for a in _live_assumptions(intent, design)[0]]
               + [("design", a) for a in design.get("assumptions") or []])
    lines += ["", "## Assumptions made for you", ""]
    if assumed:
        lines += ["Approve them with the design, or say in your notes which to change.", ""]
        lines += [f"- {line} [{where}]" for (where, _), line in zip(assumed, _assumption_lines([a for _, a in assumed]))]
    else:
        lines.append("None.")
    lines += _review_lines(plan.stage("security"))
    lines += ["", "## Decisions", ""]
    for dec in current:
        lines.append(f"- **{dec['id']}: {dec['summary']}** {dec['decision']} *(source: {dec['source']['kind']} — {dec['source']['ref']})*")
    lines += ["", "## Contracts", ""]
    for c in design["contracts"]:
        lines += [f"### {c['id']}: {c['name']}", "", _code(c["body"], c["language"]), ""]
    lines += ["## Capabilities", ""] + [f"- **{c['name']}**: {c['description']}" for c in design["capabilities"]]
    lines += ["", "## Inventory of what's new", ""] + [f"- **{n['id']}** {n['kind']}: {n['description']}" for n in design["inventory"]]
    lines += ["", "## Precursors", ""] + ([f"- **{x['id']}** {x.get('summary', '')} — `{x['where']}`: {x['what']}"
                                          for x in design["precursors"]] or ["None."])
    lines += ["", "## Wiring", "", design["wiring"], ""]
    if replaced:
        by_id = {d["id"]: d for d in design["decisions"]}
        lines += ["## Replaced along the way", ""]
        for dec in replaced:
            new = by_id.get(dec.get("superseded_by"), {})
            why = f" ({new['source']['kind']}: {new['source']['ref']})" if new else ""
            lines.append(f"- {dec['id']} → {dec.get('superseded_by')}{why}. It said: {dec['summary']}")
        lines.append("")
    path = plan.plan_dir / "design-for-review.md"
    path.write_text("\n".join(lines))
    return path
