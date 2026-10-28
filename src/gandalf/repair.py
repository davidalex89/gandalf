"""Repair the unbuilt part of a plan after a builder reports a mismatch (DESIGN.md §5.10),
or after an external dependency is delivered differently from its request (§8.6, D-93).

Merged PRs are frozen and never change. The repo is re-scanned (it has moved on), the
mismatch is recorded, and every stage from the earliest affected one is archived to
``.state/history`` and produced again with all the usual checks and pause rules. A
repair from the design stage goes back through the design checkpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

from gandalf.repo import identify
from gandalf.scan import scan_repo
from gandalf.state import PlanState, _write, now

FIELDS = ("PR", "Section", "Expected", "Found", "Evidence")


def parse_report(text: str) -> dict:
    """Parse the fixed GANDALF MISMATCH REPORT shape (PR spec section 14)."""
    if "GANDALF DEVIATION NOTE" in text and "GANDALF MISMATCH REPORT" not in text:
        raise ValueError("this is a deviation note: it records small fixes a builder made inside the "
                         "PR's own files, the plan stands, and nothing needs repair (D-151)")
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"\s*(PR|Section|Expected|Found|Evidence)\s*:\s*(.*)$", line)
        if m:
            current = m.group(1)
            found[current] = [m.group(2)]
        elif current and line.strip() and not line.strip().startswith(("```", "GANDALF MISMATCH")):
            found[current].append(line.strip())
    report = {k.lower(): " ".join(v).strip() for k, v in found.items()}
    missing = [f for f in ("PR", "Expected", "Found") if not report.get(f.lower())]
    if missing:
        raise ValueError(f"the mismatch report is missing: {', '.join(missing)} "
                         "(use the GANDALF MISMATCH REPORT shape from the PR spec)")
    return report


def _merged_prs(plan: PlanState, merged: list[str]) -> dict[str, dict]:
    sequence = plan.stage("sequence")
    if sequence is None:
        raise ValueError("there is no sequenced plan to repair")
    prs = {p["id"]: p for p in sequence["prs"]}
    unknown = [m for m in merged if m not in prs]
    if unknown:
        raise ValueError(f"unknown merged PR(s): {', '.join(unknown)}")
    for m in merged:
        missing = [d for d in prs[m]["depends_on"] if d not in merged and
                   d not in {f["id"] for f in plan.meta.get("frozen", [])}]
        if missing:
            raise ValueError(f"{m} is merged but its dependencies {', '.join(missing)} are not listed as merged")
    return prs


def _freeze_and_rescan(plan: PlanState, prs: dict[str, dict], merged: list[str]):
    """Freeze merged PRs and the contracts they expose, and re-scan the moved-on repo."""
    frozen = {p["id"]: p for p in plan.meta.get("frozen", [])}
    for m in merged:
        frozen.setdefault(m, prs[m])
    plan.meta["frozen"] = [frozen[k] for k in sorted(frozen)]
    design = plan.stage("design")
    if design is not None:
        exposed = {c for p in frozen.values() for c in p["contracts_exposed"]}
        plan.meta["frozen_contracts"] = [c for c in design["contracts"] if c["id"] in exposed]

    info = identify(plan.meta["repo"]["path"])
    plan.meta["repo"] = info.to_dict()
    _write(plan.dir / "scan.json", scan_repo(info))
    plan.meta["scanned"] = now()
    return frozen, info


def repair(plan: PlanState, report_path: Path, merged: list[str], from_stage: str) -> str:
    report = parse_report(report_path.read_text())
    prs = _merged_prs(plan, merged)
    if report["pr"] not in prs:
        raise ValueError(f"the report names {report['pr']}, which isn't in the plan")
    if report["pr"] in merged:
        raise ValueError(f"{report['pr']} is listed as merged; a mismatch is reported on unmerged work")
    frozen, info = _freeze_and_rescan(plan, prs, merged)
    plan.meta.setdefault("mismatches", []).append({
        **report, "at": now(), "from_stage": from_stage, "merged": sorted(frozen),
        "repo_commit": info.commit, "repo_fingerprint": info.fingerprint,
    })
    plan.invalidate_from(from_stage, f"repair after mismatch in {report['pr']}")
    plan.save()
    return (f"Recorded the mismatch in {report['pr']}. Frozen (merged): {', '.join(sorted(frozen)) or 'none'}. "
            f"Re-scanned the repo and archived {from_stage} onward for re-planning.")


def deliver(plan: PlanState, eid: str, details_path: Path, merged: list[str], as_requested: bool) -> str:
    """Record what another team actually built for an external dependency (D-93)."""
    design = plan.stage("design")
    external = {e["id"]: e for e in (design or {}).get("external") or []}
    if eid not in external:
        raise ValueError(f"{eid} isn't an external dependency in this plan "
                         f"({', '.join(external) or 'it has none'})")
    if external[eid].get("kind") == "client":
        raise ValueError(f"{eid} is another team's app that adopts this plan's work; nothing waits for it, "
                         "so there's nothing to deliver")
    details = details_path.read_text().strip()
    if not details:
        raise ValueError("say what was actually built (setting names, service details, limits)")
    prs = _merged_prs(plan, merged)
    plan.meta.setdefault("deliveries", {})[eid] = {
        "at": now(), "as_requested": as_requested, "details": details}
    if as_requested:
        plan.save()
        return f"Recorded {eid} as delivered as requested. Run render to update the plan's status."
    frozen, _ = _freeze_and_rescan(plan, prs, merged)
    plan.invalidate_from("design", f"{eid} delivered with changes")
    plan.save()
    return (f"Recorded what was built for {eid}. Frozen (merged): {', '.join(sorted(frozen)) or 'none'}. "
            "Re-planning from the design stage, which will see the delivered details; the design "
            "checkpoint runs again.")
