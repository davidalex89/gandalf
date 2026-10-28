"""Deterministic plan checks: sequencing, forward state, coverage and PR rules.

DESIGN.md §5.6 (sequence), §5.9 (cross-stage checks), §7 (inert/enabling), §8 (size,
breaking changes, migrations, flags), §9.1 (runnable checks), §10.5 (tier).
None of this trusts the model: orders, waves, lanes, "needed by", path tags and tiers are
computed here.
"""

from __future__ import annotations

import copy
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from gandalf.repo import SKIP_DIRS, iter_files
from gandalf.scan.detectors import MIGRATION_DIR, iac_kind_by_name

SIZE_BANDS = (("Small", 150), ("Medium", 350), ("Large", 500))
# A PR changing more than this many files says why it's kept whole (D-153). Size in lines is
# the model's own estimate, so it is shown as a band and never gated on; a single-leaf PR
# says why it stays separate (D-69, D-153): logical containment over fragmentation (D-13).
LARGE_PR_FILES = 10
# The token that marks the test runner in a known test command, so a targeted run of
# the same runner can be recognised.
RISK_LEAF_TAGS = {"migration", "auth", "breaking"}
# Secret scanners a repo may run; if it does, every PR's spec says it must pass (D-120).
SECRET_SCANNERS = re.compile(r"\b(gitleaks|detect-secrets|trufflehog|ggshield|git-secrets)\b")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "Report") -> None:
        self.errors += other.errors
        self.warnings += other.warnings
        self.notes += other.notes


def size_band(production_lines: int) -> str:
    for band, limit in SIZE_BANDS:
        if production_lines <= limit:
            return band
    return "Over"


def change_leaves(change: dict) -> list[str]:
    """The leaves a change implements: ``leaves``, or the older single ``leaf``."""
    return list(change["leaves"])


def cite_path(cite: str) -> str:
    """'app/models/user.py:12-30' -> 'app/models/user.py'"""
    path = cite.split(":", 1)[0].strip()
    return path.rstrip("/") or path


class RepoView:
    """Read-only view of the target repo's files, for grounding checks. Given the commit a
    plan was made against, it reads that commit from git, so the answer never depends on
    what's checked out (D-146). Without one (a folder that isn't a git repo, or a plan made
    on uncommitted changes) it reads the files on disk."""

    def __init__(self, root: Path, commit: str | None = None):
        self.root = root
        self.commit_files = _commit_files(root, commit) if commit else None
        if self.commit_files is not None:
            self.dirs = {str(PurePosixPath(p).parent) for p in self.commit_files}
            self.dirs |= {str(parent) for p in self.commit_files for parent in PurePosixPath(p).parents}
            return
        self.files = {p.as_posix() for p in iter_files(root)}
        # The scan skips folders that usually hold generated output (coverage, build, ...),
        # but a file git tracks is real source wherever it lives (D-94).
        self.tracked = _tracked_files(root)

    @classmethod
    def for_plan(cls, repo: dict) -> "RepoView":
        """The view a plan is checked against: its recorded commit, unless it was made on
        uncommitted changes or outside git."""
        commit = repo.get("commit") if repo.get("vcs") == "git" and not repo.get("dirty") else None
        return cls(Path(repo["path"]), commit)

    def exists(self, path: str) -> bool:
        path = cite_path(path)
        if self.commit_files is not None:
            return path in self.commit_files or path in self.dirs
        if path in self.files or path in self.tracked:
            return True
        parts = PurePosixPath(path).parts
        if any(p in SKIP_DIRS for p in parts) or path.startswith(("/", "..")):
            return False
        return (self.root / path).exists()

    def is_dir(self, path: str) -> bool:
        if path in ("", "."):
            return True
        if self.commit_files is not None:
            return cite_path(path) in self.dirs
        return (self.root / path).is_dir()


def iac_kind(path: str, scan: dict) -> str | None:
    """The infrastructure-as-code kind of a path, from the scan or its name (D-90)."""
    path = cite_path(path)
    for item in scan.get("infrastructure_as_code", []):
        if path == item["path"] or (item["path"].endswith("/") and path.startswith(item["path"])):
            return item["kind"]
    return iac_kind_by_name(path)


def is_client(e: dict) -> bool:
    """Another team's app that adopts this plan's work (D-127), not something it needs."""
    return e.get("kind") == "client"


def external_uses(pr: dict) -> list[tuple[str, str | None]]:
    """(E-n, what the code does while it's missing) for each external dependency a PR uses."""
    return [(u["id"], u["when_missing"]) for u in pr.get("external") or []]


def external_gates(prs: list[dict]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The external dependencies each enabling PR's work uses (D-92). Returns (merge gates,
    switch-on gates): a PR switched on by configuration can merge before they're ready;
    the step that sets its configuration waits instead (D-109)."""
    closure = transitive_deps(prs)
    by_id = {p["id"]: p for p in prs}
    merge, switch = {}, {}
    for p in prs:
        if p["type"] == "enabling":
            used = {e for pid in closure.get(p["id"], set()) | {p["id"]} for e, _ in external_uses(by_id.get(pid, {}))}
            (switch if p.get("switched_on_by") else merge)[p["id"]] = sorted(used, key=lambda e: int(e.split("-")[1]))
    return merge, switch


def check_switch_on_steps(prs: list[dict], sequence: dict, switch_gates: dict[str, list[str]]) -> Report:
    """A PR switched on by configuration has a step that sets it, after its dependencies are ready (D-109)."""
    report = Report()
    steps = sequence["order_of_operations"]
    configured = {p["id"] for p in prs if p.get("switched_on_by")}
    first_ready = {}
    for i, s in enumerate(steps):
        if s.get("external"):
            first_ready.setdefault(s["external"], i)
        if s.get("switches_on") and s["switches_on"] not in configured:
            report.errors.append(f"order_of_operations: step {i + 1} switches on {s['switches_on']}, which "
                                 "isn't a PR switched on by configuration (switched_on_by)")
    for pid in sorted(configured):
        at = next((i for i, s in enumerate(steps) if s.get("switches_on") == pid), None)
        if at is None:
            report.errors.append(f"{pid} is switched on by configuration, so add the order-of-operations step "
                                 f"that sets it, with switches_on: {pid}")
            continue
        for eid in switch_gates.get(pid, []):
            if first_ready.get(eid, len(steps)) > at:
                report.errors.append(f"order_of_operations: step {at + 1} switches on {pid}, but no earlier step "
                                     f"sets up or confirms {eid} (a step with external: {eid})")
    return report


def _commit_files(root: Path, commit: str) -> set[str] | None:
    """Every file in ``commit``, or None when git can't read it."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", commit],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return {p for p in out.stdout.decode("utf-8", "replace").split("\0") if p}


def _tracked_files(root: Path) -> set[str]:
    """Files git tracks in root (empty outside a git repo)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return {p for p in out.stdout.decode("utf-8", "replace").split("\0") if p}


# --- graph ----------------------------------------------------------------------------

def topo_order(prs: list[dict]) -> tuple[list[str], list[str]]:
    """Stable topological order (listing order breaks ties). Returns (order, cycle_ids)."""
    ids = [p["id"] for p in prs]
    deps = {p["id"]: [d for d in p["depends_on"] if d in ids] for p in prs}
    done: list[str] = []
    remaining = list(ids)
    while remaining:
        ready = [i for i in remaining if all(d in done for d in deps[i])]
        if not ready:
            return done, remaining
        done.append(ready[0])
        remaining.remove(ready[0])
    return done, []


def transitive_deps(prs: list[dict]) -> dict[str, set[str]]:
    direct = {p["id"]: set(p["depends_on"]) for p in prs}
    closure: dict[str, set[str]] = {}

    def visit(i: str, stack: tuple[str, ...] = ()) -> set[str]:
        if i in closure:
            return closure[i]
        if i in stack:
            return set()
        out: set[str] = set()
        for d in direct.get(i, ()):
            out.add(d)
            out |= visit(d, stack + (i,))
        closure[i] = out
        return out

    for i in direct:
        visit(i)
    return closure


def needed_by(prs: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {p["id"]: [] for p in prs}
    for p in prs:
        for d in p["depends_on"]:
            if d in out:
                out[d].append(p["id"])
    return out


def waves(prs: list[dict], order: list[str]) -> list[list[str]]:
    """The time axis (R-04, D-150): wave 1 is every PR with no dependency, wave n+1 every PR
    whose dependencies have all merged by wave n. Everything in a wave can start together."""
    by_id = {p["id"]: p for p in prs}
    wave: dict[str, int] = {}
    for i in order:
        deps = [d for d in by_id[i]["depends_on"] if d in wave]
        wave[i] = 1 + max((wave[d] for d in deps), default=0)
    groups: dict[int, list[str]] = defaultdict(list)
    for i in order:
        groups[wave[i]].append(i)
    return [groups[k] for k in sorted(groups)]


def lanes(prs: list[dict], order: list[str]) -> list[list[str]]:
    """The track axis (D-150): a lane is a chain one builder can take end to end. Each PR
    extends the first lane whose last PR it depends on, or opens a new lane; a PR that joins
    two chains extends one and the other lane ends. The number of lanes is how many builders
    can usefully work at once."""
    by_id = {p["id"]: p for p in prs}
    out: list[list[str]] = []
    for pid in order:
        deps = set(by_id[pid]["depends_on"])
        for lane in out:
            if lane[-1] in deps:
                lane.append(pid)
                break
        else:
            out.append([pid])
    return out


# --- forward state (R-03) ----------------------------------------------------------------

def forward_state(prs: list[dict], order: list[str], repo: RepoView,
                  frozen: set[str] = frozenset()) -> tuple[dict, Report]:
    """Walk PRs in order, tracking files each creates. Returns path tags keyed by
    (pr_id, path) and a report of PRs whose preconditions nothing produces. Frozen
    (already merged) PRs are history: their files are tracked but not re-checked."""
    report = Report()
    by_id = {p["id"]: p for p in prs}
    closure = transitive_deps(prs)
    created_by: dict[str, str] = {}
    deleted_by: dict[str, str] = {}
    tags: dict[tuple[str, str], str] = {}

    for pid in order:
        pr = by_id[pid]
        upstream = closure.get(pid, set())

        def available(path: str) -> str | None:
            if path in deleted_by and deleted_by[path] in upstream:
                return None
            if path in created_by and created_by[path] in upstream:
                return f"created by {created_by[path]}"
            if repo.exists(path):
                return "exists"
            return None


        if pid in frozen:
            for change in pr["changes"]:
                path = cite_path(change["path"])
                tags[(pid, path)] = "merged"
                if change["action"] == "create":
                    created_by[path] = pid
                elif change["action"] == "delete":
                    deleted_by[path] = pid
            continue

        own = {cite_path(c["path"]) for c in pr["changes"] if c["action"] == "create"}
        reachable = own | {p for p, owner in created_by.items() if owner in upstream}
        for change in pr["changes"]:
            path = cite_path(change["path"])
            if change["action"] == "generate":
                # produced by the repo's own tooling, whose output and naming the repo's
                # conventions decide (D-77, D-102): the path, or a folder above it, must be real
                tags[(pid, path)] = "generated by repo tooling"
                ancestors = [str(a) for a in PurePosixPath(path).parents if str(a) != "."]
                if not any(repo.is_dir(p) or repo.exists(p) or any(r.startswith(p + "/") for r in reachable)
                           for p in [path, *ancestors]):
                    report.errors.append(
                        f"{pid}: generates into {path}, but neither it nor any folder above it is in the repo "
                        "or created by this PR or one it depends on. Name the folder the repo's tool writes "
                        "into, or the path the repo's convention gives")
                continue
            state = available(path)
            if change["action"] in ("modify", "delete"):
                if state is None:
                    if path in created_by and created_by[path] not in upstream:
                        report.errors.append(
                            f"{pid}: changes {path}, which {created_by[path]} creates, but "
                            f"{pid} does not depend on {created_by[path]}")
                    else:
                        report.errors.append(
                            f"{pid}: {change['action']}s {path}, which neither the repo nor an "
                            "earlier PR it depends on provides")
                    tags[(pid, path)] = "unverified"
                else:
                    tags[(pid, path)] = state
                if change["action"] == "delete":
                    deleted_by[path] = pid
            else:
                if state is not None:
                    report.errors.append(f"{pid}: creates {path}, which already exists ({state})")
                    tags[(pid, path)] = state
                    continue
                if path in created_by:
                    report.errors.append(f"{pid}: creates {path}, which {created_by[path]} also creates")
                parent = str(PurePosixPath(path).parent)
                parent_ok = repo.is_dir(parent) or any(
                    p.startswith(parent + "/") for p, owner in created_by.items()
                    if owner in upstream or owner == pid)
                tags[(pid, path)] = "new (parent exists)" if parent_ok else "unverified"
                created_by[path] = pid

        for item in pr["read_first"]:
            path = cite_path(item["path"])
            if available(path) is None:
                report.errors.append(
                    f"{pid}: read_first names {path}, which won't exist when {pid} starts")
    return tags, report


# --- whole-plan checks -----------------------------------------------------------------------

def secret_scanner(scan: dict, context: dict | None) -> str | None:
    """The secret scanner the repo runs, if any (gitleaks, detect-secrets ...): every PR's
    spec says it must pass (D-120)."""
    cmds = [f["value"] for f in scan.get("declared_commands", [])]
    cmds += [c["command"] for key in ("test_commands", "run_commands", "check_commands")
             for c in (context or {}).get(key) or []]
    found = next((SECRET_SCANNERS.search(c) for c in cmds if SECRET_SCANNERS.search(c)), None)
    return found.group(1) if found else None


def check_plan(*, intent: dict, design: dict, decompose: dict, sequence: dict,
               scan: dict, context: dict | None, repo: RepoView,
               security: dict | None = None, frozen: set[str] = frozenset(),
               previous_sequence: dict | None = None) -> tuple[Report, dict]:
    """Run every cross-stage check. Returns the report and the derived data (order,
    waves, lanes, needed-by, tags, bands) that rendering uses."""
    report = Report()
    prs = sequence["prs"]
    by_id: dict[str, dict] = {}
    for pr in prs:
        if pr["id"] in by_id:
            report.errors.append(f"duplicate PR id {pr['id']}")
        by_id[pr["id"]] = pr
    slugs = [p["slug"] for p in prs]
    for s in {s for s in slugs if slugs.count(s) > 1}:
        report.errors.append(f"duplicate PR slug {s!r}")

    for pr in prs:
        for d in pr["depends_on"]:
            if d not in by_id:
                report.errors.append(f"{pr['id']}: depends on unknown {d}")
            if d == pr["id"]:
                report.errors.append(f"{pr['id']}: depends on itself")
    order, cycle = topo_order(prs)
    if cycle:
        report.errors.append(f"dependency cycle among: {', '.join(cycle)}")
        order = order + cycle
    closure = transitive_deps(prs)

    # leaves: every leaf in exactly one PR (§5.6)
    leaves = {leaf["id"]: leaf for comp in decompose["components"] for leaf in comp["leaves"]}
    leaf_owner: dict[str, list[str]] = defaultdict(list)
    for pr in prs:
        for lid in pr["leaves"]:
            leaf_owner[lid].append(pr["id"])
            if lid not in leaves:
                report.errors.append(f"{pr['id']}: unknown leaf {lid}")
        paths = [cite_path(ch["path"]) for ch in pr["changes"]]
        for path in sorted({p for p in paths if paths.count(p) > 1}):
            # clearer than the forward-state error this used to cause (D-86)
            report.errors.append(
                f"{pr['id']}: lists {path} more than once. List each file once per PR, say everything "
                "that changes in it, and name every leaf it covers in `leaves`")
        for ch in pr["changes"]:
            named = change_leaves(ch)
            if not named:
                report.errors.append(f"{pr['id']}: change to {ch['path']} names no leaf (`leaves`)")
            for lid in named:
                if lid not in pr["leaves"]:
                    report.errors.append(f"{pr['id']}: change to {ch['path']} names leaf {lid}, which isn't in this PR")
        for lid in pr["leaves"]:
            if lid in leaves and not any(lid in change_leaves(ch) for ch in pr["changes"]):
                report.warnings.append(f"{pr['id']}: leaf {lid} has no file change listed")
    for lid in leaves:
        owners = leaf_owner.get(lid, [])
        if len(owners) != 1:
            report.errors.append(f"leaf {lid} must be in exactly one PR (found in {owners or 'none'})")

    # contracts (§9, R-03)
    design_contracts = {c["id"] for c in design["contracts"]}
    exposer: dict[str, list[str]] = defaultdict(list)
    for pr in prs:
        for cid in pr["contracts_exposed"]:
            exposer[cid].append(pr["id"])
            if cid not in design_contracts:
                report.errors.append(f"{pr['id']}: exposes unknown contract {cid}")
    for cid in design_contracts:
        if len(exposer.get(cid, [])) != 1:
            report.errors.append(f"contract {cid} must be exposed by exactly one PR (found {exposer.get(cid) or 'none'})")
    for pr in prs:
        for cid in pr["contracts_used"]:
            if cid not in design_contracts:
                report.errors.append(f"{pr['id']}: uses unknown contract {cid}")
                continue
            src = exposer.get(cid, [])
            if src and src[0] != pr["id"] and src[0] not in closure.get(pr["id"], set()):
                report.errors.append(
                    f"{pr['id']}: uses {cid}, exposed by {src[0]}, but does not depend on {src[0]}")

    externals = {e["id"] for e in design.get("external") or []}
    clients = {e["id"] for e in design.get("external") or [] if is_client(e)}
    contract_files = {c["id"]: c["files"] for c in design["contracts"] if c.get("files")}
    for pr in prs:
        changed = [cite_path(ch["path"]).rstrip("/") for ch in pr["changes"]]
        for cid in pr["contracts_exposed"]:
            wanted = contract_files.get(cid)
            if wanted and not any(cite_path(f) == p or cite_path(f).startswith(p + "/") for f in wanted for p in changed):
                report.errors.append(f"{pr['id']}: exposes {cid}, whose code lives in {', '.join(wanted)}, but doesn't "
                                     "change that file: move the contract to the PR that does, or add the change (D-138)")
    decision_ids = {d["id"] for d in design["decisions"]}
    capabilities = {c["name"] for c in design["capabilities"]}
    precursor_ids = {x["id"]: x for x in design["precursors"]}

    # per-PR rules
    for pr in prs:
        pid = pr["id"]
        for did in pr["decisions"]:
            if did not in decision_ids:
                report.errors.append(f"{pid}: unknown decision {did}")
        for ch in pr["changes"]:
            kind = iac_kind(ch["path"], scan)
            if kind:
                report.errors.append(
                    f"{pid}: {ch['path']} is infrastructure as code ({kind}). GANDALF never writes it: "
                    "describe what's needed as an external dependency (E-n) in the design, for the team "
                    "that owns it (§8.6)")
        uses = external_uses(pr)
        for eid, note in uses:
            if eid not in externals:
                report.errors.append(f"{pid}: uses unknown external dependency {eid}")
            elif eid in clients:
                report.errors.append(f"{pid}: {eid} is a client app that adopts this plan's work; nothing here "
                                     "waits for it, so leave it out of external")
            if not note:
                report.errors.append(f"{pid}: uses {eid}, so say what the code does while it isn't there yet "
                                     "(`when_missing` on that entry); it must stay safe")
        for eid in sorted({e for e, _ in uses if [x for x, _ in uses].count(e) > 1}):
            report.errors.append(f"{pid}: lists {eid} more than once under external")
        if pr["type"] == "inert":
            if not pr.get("layer"):
                report.errors.append(f"{pid}: inert PRs name their layer")
            if not pr.get("unobservable_how"):
                report.errors.append(f"{pid}: inert PRs state how they stay unobservable (unobservable_how)")
            # D-72: inert means nothing changes for users or existing clients
            if pr["breaking"]["is_breaking"]:
                report.errors.append(f"{pid}: an inert PR can't be breaking; make it a separate enabling step")
            breaking_leaves = [lid for lid in pr["leaves"] if "breaking" in leaves.get(lid, {}).get("risk", [])]
            if breaking_leaves:
                report.errors.append(
                    f"{pid}: inert, but leaf {', '.join(breaking_leaves)} is tagged breaking: either the leaf "
                    "isn't breaking (re-tag it in decompose) or it belongs in an enabling PR")
        elif pr["type"] == "enabling":
            if pr.get("capability") not in capabilities:
                report.errors.append(f"{pid}: enabling PRs name a design capability (got {pr.get('capability')!r})")
            if not pr.get("switch_off"):
                report.errors.append(f"{pid}: enabling PRs state how to switch the capability back off")
        else:  # precursor
            if not any(t in precursor_ids for leaf in pr["leaves"] for t in leaves.get(leaf, {}).get("traces", [])):
                report.errors.append(f"{pid}: precursor PRs must implement a precursor (X-n)")
        steps = pr["steps"]
        # buildable steps (D-125): each step's files are this PR's, and every file has a step
        paths = [ch["path"] for ch in pr["changes"]]
        for i, st in enumerate(steps, 1):
            for path in st["changes"]:
                if path not in paths:
                    report.errors.append(f"{pid} step {i}: names {path}, which isn't in this PR's changes")
        in_steps = {path for st in steps for path in st["changes"]}
        unstepped = [path for path in paths if steps and path not in in_steps]
        if unstepped:
            report.errors.append(f"{pid}: {', '.join(unstepped)} isn't in any step. When a PR has steps, "
                                 "each change belongs to the step (or steps) that make it")

        migrates = any(ch["action"] == "generate" and MIGRATION_DIR.search(cite_path(ch["path"]) + "/")
                       for ch in pr["changes"]) or any("migration" in leaves.get(lid, {}).get("risk", [])
                                                        for lid in pr["leaves"])
        if migrates and not pr.get("schema_change"):
            report.errors.append(f"{pid}: changes the database (a migration), so say what data changes, in "
                                 "plain terms, in schema_change")
        # The line estimate is the model's own number: shown to the reader as a band, never
        # gated on (D-153). The gates are structural, which code can see.
        band = size_band(pr["size"]["production_lines"])
        if band == "Over":
            report.warnings.append(f"{pid}: estimated over 500 production lines"
                                   + (f"; kept whole because: {pr['size_reason']}" if pr.get("size_reason")
                                      else "; split it where the work divides unless it belongs together, and say why in size_reason"))
        if pr["size"]["files"] != len(pr["changes"]):
            report.warnings.append(f"{pid}: size.files says {pr['size']['files']} but changes lists "
                                   f"{len(pr['changes'])}; the spec shows the count from changes (D-173)")
        if len(pr["changes"]) > LARGE_PR_FILES and not pr.get("size_reason"):
            report.errors.append(f"{pid}: changes {len(pr['changes'])} files. Say why it's kept whole (size_reason), "
                                 "or split it where the work divides")
        risky = {t for lid in pr["leaves"] for t in leaves.get(lid, {}).get("risk", [])} & RISK_LEAF_TAGS
        if len(prs) > 1 and pr["type"] != "precursor" and len(pr["leaves"]) == 1 and not pr.get("kept_separate_because"):
            report.errors.append(
                f"{pid}: a single leaf on its own. Merge it into the PR it logically belongs with, or say why "
                "it must stay separate (kept_separate_because) — e.g. it isolates a migration or auth change, "
                "or is the switch-on step")
        if risky and len(pr["leaves"]) > 1 and not pr["steps"]:
            report.errors.append(f"{pid}: holds a {'/'.join(sorted(risky))} leaf alongside others, so it needs an ordered step list isolating it (R-02)")

        flag = pr.get("flag")
        if flag:
            remover = by_id.get(flag["removed_by"])
            if remover is None:
                report.errors.append(f"{pid}: flag {flag['name']} is removed by unknown {flag['removed_by']}")
            else:
                if pid not in closure.get(remover["id"], set()):
                    report.errors.append(f"{remover['id']}: removes flag {flag['name']} but does not depend on {pid}")
                if remover.get("removes_flag") != flag["name"]:
                    report.errors.append(f"{remover['id']}: must declare removes_flag: {flag['name']!r}")
        ec = pr.get("expand_contract")
        if ec:
            pair = by_id.get(ec["pair"])
            if pair is None or not pair.get("expand_contract") or pair["expand_contract"]["pair"] != pid \
                    or pair["expand_contract"]["role"] == ec["role"]:
                report.errors.append(f"{pid}: expand/contract pair with {ec['pair']} is not mutual")
            elif ec["role"] == "contract" and ec["pair"] not in closure.get(pid, set()):
                report.errors.append(f"{pid}: the contract step must come after (depend on) {ec['pair']}")
        if pr["breaking"]["is_breaking"] and not (ec or flag):
            report.warnings.append(f"{pid}: breaking change mitigated by: {pr['breaking']['detail']}")

        for item in pr["reuse"]:
            if not repo.exists(item["where"]):
                report.errors.append(f"{pid}: reuse names {item['where']}, which isn't in the repo")
        for conv in pr["conventions"]:
            if not repo.exists(conv["example"]):
                report.errors.append(f"{pid}: convention example {conv['example']} isn't in the repo")

    enabling = [p for p in prs if p["type"] == "enabling"]
    builds: dict[frozenset, list[dict]] = defaultdict(list)
    for p in enabling:
        inert_upstream = frozenset(d for d in closure.get(p["id"], set()) if by_id.get(d, {}).get("type") == "inert")
        if inert_upstream:
            builds[inert_upstream].append(p)
    for group in builds.values():
        if len(group) > 1 and not all(p.get("kept_separate_because") for p in group[1:]):
            ids = ", ".join(p["id"] for p in group)
            report.errors.append(
                f"{ids} switch on capabilities built from exactly the same work: combine them into one "
                "enabling PR, or give each after the first a kept_separate_because")

    total = sum(p["size"]["production_lines"] for p in prs)
    report.notes.append(f"{len(prs)} PRs, ~{total} estimated production lines in total "
                        f"(~{total // max(len(prs), 1)} per PR)")

    for step in sequence["order_of_operations"]:
        if step["after"] not in by_id:
            report.errors.append(f"order_of_operations: unknown PR {step['after']}")
        if step.get("external") and step["external"] not in externals:
            report.errors.append(f"order_of_operations: unknown external dependency {step['external']}")
        elif step.get("external") in clients:
            report.errors.append(f"order_of_operations: {step['external']} is a client app; it has no set-up "
                                 "step, since nothing waits for it")
    report.extend(check_external_order(design, sequence))

    # every PR boundary is checked in writing (D-78, D-73)
    covered = {b["after"] for b in sequence.get("boundaries", [])}
    for pid in order:
        if pid not in covered:
            report.errors.append(f"boundaries: say what's true after {pid} merges and whether every decision still holds")
    for b in sequence.get("boundaries", []):
        if b["after"] not in by_id:
            report.errors.append(f"boundaries: unknown PR {b['after']}")
        if not b["decisions_hold"]:
            if not b.get("gap"):
                report.errors.append(f"boundaries: after {b['after']} a decision doesn't hold; describe the gap")
            if not sequence.get("pauses"):
                report.errors.append(
                    f"boundaries: after {b['after']} a decision doesn't hold. Reorder the work or move the fix "
                    "earlier so it does, or raise a pause for the designer — never leave it")

    gates, switch_gates = external_gates(prs) if not cycle else ({}, {})
    report.extend(check_switch_on_steps(prs, sequence, switch_gates))
    tags, fs = forward_state(prs, order, repo, frozen)
    report.extend(fs)
    report.extend(check_coverage(intent, design, decompose, sequence, security,
                                 previous_sequence=previous_sequence))

    derived = {
        "order": order,
        "waves": waves(prs, order) if not cycle else [order],
        "lanes": lanes(prs, order) if not cycle else [order],
        "needed_by": needed_by(prs),
        "tags": {f"{k[0]}|{k[1]}": v for k, v in tags.items()},
        "bands": {p["id"]: size_band(p["size"]["production_lines"]) for p in prs},
        "gates": gates,
        "switch_gates": switch_gates,
        "risk": {p["id"]: sorted(
            {t for lid in p["leaves"] for t in leaves.get(lid, {}).get("risk", [])}
            | ({"enabling"} if p["type"] == "enabling" else set())) for p in prs},
    }
    return report, derived


def check_external_order(design: dict, sequence: dict) -> Report:
    """An external dependency that must come after another is set up after it (D-101)."""
    report = Report()
    steps = sequence["order_of_operations"]
    first = {}
    for i, s in enumerate(steps):
        if s.get("external"):
            first.setdefault(s["external"], i)
    for e in design.get("external") or []:
        for dep in e.get("after") or []:
            if e["id"] not in first or dep not in first:
                missing = [x for x in (dep, e["id"]) if x not in first]
                report.errors.append(
                    f"order_of_operations: {e['id']} must be set up after {dep}, so add steps naming "
                    f"{' and '.join(missing)} in `external`, in that order")
            elif first[dep] > first[e["id"]]:
                report.errors.append(
                    f"order_of_operations: step {first[e['id']] + 1} sets up {e['id']} before {dep} "
                    f"(step {first[dep] + 1}), but {e['id']} must come after {dep}")
    return report


def check_coverage(intent: dict, design: dict, decompose: dict, sequence: dict,
                   security: dict | None, previous_sequence: dict | None = None) -> Report:
    """Two-way coverage (R-01, §5.9)."""
    report = Report()
    known = {o["id"] for o in intent["outcomes"]} | {b["id"] for b in intent["behaviours"]} \
        | {c["id"] for c in design["contracts"]} | {x["id"] for x in design["precursors"]}
    leaves = [leaf for comp in decompose["components"] for leaf in comp["leaves"]]
    in_leaves = {t for leaf in leaves for t in leaf["traces"]} | {c for leaf in leaves for c in leaf["contracts"]}
    in_done = {t for pr in sequence["prs"] for d in pr["done_when"] for t in d["traces"]}
    for item in sorted(known):
        if item not in in_leaves:
            report.errors.append(f"coverage: {item} is not implemented by any leaf")
        # a contract no PR exposes is reported by check_plan (exactly one PR exposes each)
        if not item.startswith("C-") and item not in in_done:
            report.errors.append(f"coverage: {item} is not checked by any PR's Done when")
    # a leaf tracing an unknown id is rejected when the breakdown is submitted (validate.py)
    for pr in sequence["prs"]:
        for d in pr["done_when"]:
            for t in d["traces"]:
                if t not in known:
                    report.errors.append(f"{pr['id']}: Done when traces unknown {t}")
    used = {e for pr in sequence["prs"] for e, _ in external_uses(pr)}
    for e in design.get("external") or []:
        if e["id"] not in used and not is_client(e):
            report.errors.append(f"coverage: external dependency {e['id']} isn't used by any PR (`external`)")

    if security:
        report.extend(check_placement(security, sequence, design, previous=previous_sequence))
    return report


def placed_findings(security: dict) -> list[dict]:
    """Findings whose requirement a PR must build in (D-149): required fixes and the
    lower-severity "Consider" notes, unless a design change replaces them or an external
    request carries them."""
    return [f for f in security["findings"]
            if f.get("requirement") and f["response"] in ("mitigate", "eliminate")
            and not f.get("design_change") and not f.get("external")]


def check_placement(security: dict, sequence: dict, design: dict, previous: dict | None = None) -> Report:
    """Every fix the security review asks for is built into exactly one PR (D-149), and a
    replan keeps each fix in the PR that held it before, unless that PR is gone (D-182)."""
    report = Report()
    findings = {f["id"]: f for f in security["findings"]}
    to_place = {f["id"] for f in placed_findings(security)}
    where: dict[str, list[str]] = {}
    for pr in sequence["prs"]:
        for sid in pr.get("security") or []:
            if sid not in findings:
                report.errors.append(f"{pr['id']}: security lists {sid}, which the security review doesn't have")
            elif sid not in to_place:
                report.errors.append(f"{pr['id']}: security lists {sid}, which has no fix for a PR to build in")
            where.setdefault(sid, []).append(pr["id"])
    if previous:
        kept = {pr["id"] for pr in sequence["prs"]}
        for old in previous["prs"]:
            for sid in old.get("security") or []:
                if sid in to_place and old["id"] in kept and old["id"] not in where.get(sid, []):
                    report.errors.append(
                        f"{old['id']}: the previous sequence built {sid} into it and this one moves it to "
                        f"{', '.join(where.get(sid) or ['nowhere'])}; a replan keeps a fix where it was unless that "
                        "PR is gone (D-182). If the fix now spans PRs, the finding stays listed here and the other "
                        "PR's changes carry its share of the work")
    for sid in sorted(to_place):
        prs = where.get(sid, [])
        if not prs:
            report.errors.append(f"coverage: security finding {sid} isn't built into any PR: list it under the "
                                 "`security` of the PR that builds what it protects, with the fix in its changes")
        elif len(prs) > 1:
            report.errors.append(f"coverage: security finding {sid} is listed by {', '.join(prs)}: one PR builds each fix")
    externals = {e["id"] for e in design.get("external") or []}
    for f in security["findings"]:
        if f.get("external") and f["external"] not in externals:
            report.errors.append(f"security: {f['id']} puts its fix in unknown {f['external']}")
    return report


def with_placement(security: dict, sequence: dict) -> dict:
    """The review with each fix's PR (or external request) filled in as ``target_pr``,
    for rendering and checks that read where a fix landed."""
    placed = {sid: pr["id"] for pr in sequence["prs"] for sid in pr.get("security") or []}
    review = copy.deepcopy(security)
    for f in review["findings"]:
        target = f.get("external") or placed.get(f["id"])
        if target:
            f["target_pr"] = target
    return review


SETTLED_RESPONSES = ("accept", "limitation", "transfer")


def settled(intent: dict, design: dict, reviews: list[dict]) -> list[dict]:
    """What the security and boundary reviewers are shown as already settled (D-124, D-160):
    the designer's accepted decisions, the assumptions they approved, known limitations, the
    brief's own decisions (D-161), and what earlier reviews accepted. A finding may re-open one by its ``ref``, saying what's new."""
    retired = {r["text"] for r in design.get("retired_assumptions") or []}
    items = [{"ref": d["id"], "what": f"{d['summary']}: {d['decision']}", "settled_by": f"decision ({d['source']['kind']})"}
             for d in design["decisions"] if d["status"] == "accepted"]
    items += [{"ref": a["text"], "what": a["why"], "settled_by": "assumption the designer approved"}
              for a in (intent.get("assumptions") or []) + (design.get("assumptions") or [])
              if a["text"] not in retired]
    items += [{"ref": lim["summary"], "what": lim["detail"], "settled_by": "known limitation"}
              for lim in design["limitations"]]
    # the brief's own decisions: the designer's answers restated in product terms (D-161)
    items += [{"ref": item.get("ref") or item["text"], "what": item["text"],
               "settled_by": f"decision in the brief ({item.get('source', 'designer')})"}
              for item in intent.get("decisions") or []]
    for n, review in enumerate(reviews, 1):
        items += [{"ref": f"review {n} {f['id']}", "what": f"{f['summary']} ({f.get('reason', '')})",
                   "settled_by": f"earlier security review: {f['response']}"}
                  for f in review.get("findings", []) if f["response"] in SETTLED_RESPONSES]
    return items


def earlier_requirements(reviews: list[dict]) -> list[dict]:
    """What the latest earlier review required of PRs (fixes and "Consider" notes). A new
    review keeps each one or drops it with a reason (D-137), so none disappears silently."""
    if not reviews:
        return []
    n = len(reviews)
    return [{"ref": f"review {n} {f['id']}", "summary": f["summary"], "severity": f["severity"],
             "requirement": f["requirement"]}
            for f in reviews[-1].get("findings", []) if f.get("requirement") and not f.get("design_change")]


def check_carried(security: dict, earlier: list[dict]) -> Report:
    report = Report()
    refs = {r["ref"] for r in earlier}
    kept = [f["keeps"] for f in security["findings"] if f.get("keeps")]
    dropped = [d["ref"] for d in security.get("dropped") or []]
    for ref in kept + dropped:
        if ref not in refs:
            report.errors.append(f"security: {ref!r} isn't a requirement from the earlier review: copy its ref exactly")
    for f in security["findings"]:
        if f.get("keeps") and not f.get("requirement"):
            report.errors.append(f"security: {f['id']} keeps {f['keeps']}, so give its requirement")
    for ref in sorted(refs - set(kept) - set(dropped)):
        report.errors.append(f"security: earlier requirement {ref} is neither kept nor dropped: restate it as a "
                             "finding with keeps, or add it to dropped with the reason")
    return report


def applied_findings(security: dict) -> list[dict]:
    """Findings a PR must build in as required fixes: medium or above, or anything that
    would break the brief or a design decision whatever its severity (D-148); mitigated or
    eliminated, and not a design change (that sends the design back)."""
    return [f for f in security["findings"]
            if (f["severity"] != "low" or f.get("breaks")) and f["response"] in ("mitigate", "eliminate")
            and not f.get("design_change")]


def check_security(security: dict, design: dict) -> Report:
    """The review of the design (D-149): every inventory entry checked, an abuse case for
    every capability, and each finding answered the way its response requires."""
    report = Report()
    inventory = {n["id"] for n in design["inventory"]}
    checked = {c["inventory"] for c in security["checked"]}
    for nid in sorted(inventory - checked):
        report.errors.append(f"security: inventory entry {nid} was not checked")
    for nid in sorted(checked - inventory):
        report.errors.append(f"security: checked unknown inventory entry {nid}")
    capabilities = {c["name"] for c in design["capabilities"]}
    abused = {a["capability"] for a in security["abuse_cases"]}
    for name in sorted(capabilities - abused):
        report.errors.append(f"security: capability {name!r} needs an abuse case")
    for name in sorted(abused - capabilities):
        report.errors.append(f"security: abuse case names {name!r}, which isn't one of the design's capabilities: "
                             "copy its name exactly")
    ids = [f["id"] for f in security["findings"]]
    for dup in {i for i in ids if ids.count(i) > 1}:
        report.errors.append(f"security: duplicate finding id {dup}")
    for f in security["findings"]:
        concerns = f["concerns"]
        if concerns.startswith("N-") and concerns not in inventory:
            report.errors.append(f"security: {f['id']} concerns unknown {concerns}")
        if concerns.startswith("abuse:") and concerns[6:] not in capabilities:
            report.errors.append(f"security: {f['id']} concerns an abuse case on unknown capability {concerns[6:]!r}")
        if f.get("design_change") and (
                (f["severity"] == "low" and not f.get("breaks")) or f["response"] not in ("mitigate", "eliminate")):
            report.errors.append(f"security: {f['id']}: design changes are for medium-or-above findings you "
                                 "mitigate or eliminate")
        named = sorted({m for k in ("summary", "issue", "requirement", "design_change")
                        for m in re.findall(r"\b[OB]-\d+\b", str(f.get(k) or ""))})
        if named and not f.get("breaks") and f["response"] in ("mitigate", "eliminate"):
            report.errors.append(f"security: {f['id']} names {', '.join(named)} but leaves breaks empty: a fix that "
                                 "protects something the brief states names it in breaks and is required whatever "
                                 "its severity (D-156)")
        if f.get("breaks") and f["response"] not in ("mitigate", "eliminate"):
            report.errors.append(f"security: {f['id']} breaks {', '.join(f['breaks'])}, so it must be fixed "
                                 "(mitigate or eliminate), not accepted")
        if f in applied_findings(security) and not f.get("requirement"):
            report.errors.append(f"security: {f['id']} ({f['severity']}) needs a requirement (or a design_change)")
        if f["response"] in ("mitigate", "eliminate") and not f.get("citation"):
            report.errors.append(f"security: {f['id']} is a fix, so cite the control it applies "
                                 "(an OWASP Proactive Control, e.g. C1)")
        if f["response"] in ("accept", "transfer", "limitation") and not f.get("reason"):
            report.errors.append(f"security: {f['id']} is a {f['response']}, so it needs a reason")
    return report


def check_boundary(boundary: dict, sequence: dict) -> Report:
    """The boundary check covers every PR, and every state that doesn't hold has a gap
    to re-plan around (D-149)."""
    report = Report()
    prs = [p["id"] for p in sequence["prs"]]
    checked = [c["after"] for c in boundary["checked"]]
    for pid in prs:
        if pid not in checked:
            report.errors.append(f"boundary: say what's true after {pid} merges and whether it holds")
    for pid in sorted(set(checked) - set(prs)):
        report.errors.append(f"boundary: unknown PR {pid}")
    # a gap opened by one PR may stay open after later ones: one finding covers it
    first_gap = min((prs.index(f["after"]) for f in boundary["findings"] if f["after"] in prs), default=None)
    for c in boundary["checked"]:
        if (not c["holds"] and c["after"] in prs
                and (first_gap is None or prs.index(c["after"]) < first_gap)):
            report.errors.append(f"boundary: after {c['after']} something doesn't hold, so raise it as a finding "
                                 "with how the PRs must change")
    for f in boundary["findings"]:
        if f["after"] not in prs:
            report.errors.append(f"boundary: {f['id']} is after unknown {f['after']}")
    ids = [f["id"] for f in boundary["findings"]]
    for dup in {i for i in ids if ids.count(i) > 1}:
        report.errors.append(f"boundary: duplicate finding id {dup}")
    return report


def select_tier(sequence: dict, approved_pauses: list[dict], force_full: bool) -> str:
    """Light only when every condition in §10.5 holds."""
    prs = sequence["prs"]
    if force_full or len(prs) > 2:
        return "full"
    if any(p["breaking"]["is_breaking"] or p.get("schema_change") or p.get("new_dependency")
           or p.get("external") for p in prs):
        return "full"
    if any(p["kind"] == "new_dependency" for p in approved_pauses):
        return "full"
    return "light"
