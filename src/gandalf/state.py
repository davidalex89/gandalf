"""Planning progress in ``<plan folder>/.state/`` (DESIGN.md §11.2, D-50).

Holds the scan, every stage's validated output, the designer's answers, question
metrics (§5.3) and mismatch records (§5.10). Re-submitting a stage moves it and every
later stage into ``history/`` so nothing is silently overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from gandalf import SCHEMA_VERSION

# The security review sees the design before the designer's checkpoint, so its fixes are
# built into the PRs from the start; the boundary check looks at the states between PRs (D-149).
STAGES = ("context", "intent", "design", "security", "approval", "decompose", "sequence", "boundary")
MODEL_STAGES = tuple(s for s in STAGES if s != "approval")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


class PlanExistsError(Exception):
    """The plan folder already holds a different feature's plan (D-179)."""


class OldPlanError(Exception):
    """A plan written in an older format, which GANDALF no longer reads (D-141)."""


class PlanState:
    def __init__(self, plan_dir: Path):
        self.plan_dir = Path(plan_dir)
        self.dir = self.plan_dir / ".state"
        if not (self.dir / "meta.json").exists():
            raise FileNotFoundError(
                f"no GANDALF plan at {self.plan_dir} (run `gandalf scan` or `gandalf run` first)")
        self.meta = _read(self.dir / "meta.json")
        if int(self.meta.get("schema_version", "1")) < int(SCHEMA_VERSION):
            raise OldPlanError(
                f"the plan at {self.plan_dir} was made by an older GANDALF (format "
                f"{self.meta.get('schema_version', '1')}, now {SCHEMA_VERSION}); start it again with `gandalf scan` "
                "or `gandalf run`. Its files are still there to read")
        self.answers = _read(self.dir / "answers.json", {})
        self.answers.setdefault("questions", {})
        self.answers.setdefault("pauses", {})
        self.answers.setdefault("guidance", [])
        self.answers.setdefault("counter", {"Q": 0, "PA": 0})

    # --- creation -------------------------------------------------------------------
    @classmethod
    def create(cls, plan_dir: Path, *, repo: dict, scan: dict, feature_title: str,
               feature_text: str | None, supporting: list[Path] | None = None,
               force_full: bool = False) -> "PlanState":
        state_dir = Path(plan_dir) / ".state"
        existing = _read(state_dir / "meta.json")
        stored = state_dir / "input" / "feature.md"
        if existing is not None and feature_text is not None and stored.exists() \
                and stored.read_text().strip() != feature_text.strip():
            raise PlanExistsError(
                f"{plan_dir} already holds a plan for '{existing.get('feature', {}).get('title', '?')}' "
                "with a different feature text, and its finished stages would be taken as this feature's "
                "(D-179). Give this feature its own folder with --out, or its own name with --title.")
        state_dir.mkdir(parents=True, exist_ok=True)
        meta = existing
        if meta is None:
            meta = {
                "schema_version": SCHEMA_VERSION,
                "created": now(),
                "feature": {"title": feature_title},
                "force_full": force_full,
                "stages": {s: {"status": "missing"} for s in STAGES},
                "frozen": [],
                "omissions": [],
                "verify": {},
                "mismatches": [],
            }
        meta["repo"] = repo
        meta["scanned"] = now()
        _write(state_dir / "meta.json", meta)
        _write(state_dir / "scan.json", scan)
        if feature_text is not None:
            (state_dir / "input").mkdir(exist_ok=True)
            (state_dir / "input" / "feature.md").write_text(feature_text)
            for src in supporting or []:
                shutil.copy2(src, state_dir / "input" / src.name)
        return cls(plan_dir)

    # --- persistence ----------------------------------------------------------------
    def save(self) -> None:
        _write(self.dir / "meta.json", self.meta)
        _write(self.dir / "answers.json", self.answers)

    @property
    def scan(self) -> dict:
        return _read(self.dir / "scan.json", {})

    def feature_text(self) -> str:
        p = self.dir / "input" / "feature.md"
        return p.read_text() if p.exists() else ""

    def supporting_files(self) -> list[Path]:
        d = self.dir / "input"
        return sorted(p for p in d.glob("*") if p.name != "feature.md") if d.exists() else []

    def stage(self, name: str) -> dict | None:
        return _read(self.dir / "stages" / f"{name}.json")

    def status(self, name: str) -> str:
        return self.meta["stages"][name]["status"]

    def _set_status(self, name: str, status: str, **extra) -> None:
        self.meta["stages"][name] = {"status": status, **extra}

    def _archive(self, name: str, reason: str) -> None:
        src = self.dir / "stages" / f"{name}.json"
        if src.exists():
            dest = self.dir / "history" / f"{now().replace(':', '')}-{name}.json"
            dest.parent.mkdir(exist_ok=True)
            data = _read(src)
            _write(dest, {"stage": name, "archived": now(), "reason": reason, "data": data})
            src.unlink()

    def replace_stage(self, name: str, data: dict, reason: str) -> None:
        """Replace one stage's output in place, keeping later stages (PR monitoring, §5.11).
        The old output goes to ``history/`` like any other replaced one."""
        self._archive(name, reason)
        _write(self.dir / "stages" / f"{name}.json", data)

    def invalidate_from(self, name: str, reason: str) -> None:
        """Archive ``name`` and every later stage; they must be produced again."""
        start = STAGES.index(name)
        for later in STAGES[start:]:
            self._archive(later, reason)
            self._set_status(later, "missing")
            self._withdraw_open_items(later)

    def _withdraw_open_items(self, stage: str) -> None:
        """Open items raised by an output that has been replaced no longer apply."""
        for q in self.answers["questions"].values():
            if q["stage"] == stage and q["status"] in ("open", "proposed"):
                q["status"] = "withdrawn"
        for p in self.answers["pauses"].values():
            if p["stage"] == stage and p["status"] == "open":
                p["status"] = "withdrawn"

    def previous_output(self, name: str) -> dict | None:
        """What a re-run of a stage builds on: the stored output, else the last archived."""
        current = self.stage(name)
        if current is not None:
            return current
        earlier = self.history(name)
        return earlier[-1]["data"] if earlier else None

    def previous_design(self) -> dict | None:
        return self.previous_output("design")

    def history(self, name: str) -> list[dict]:
        d = self.dir / "history"
        if not d.exists():
            return []
        return [_read(p) for p in sorted(d.glob(f"*-{name}.json"))]

    def security_reviews(self) -> list[dict]:
        """Every security review so far, oldest first, the stored one last."""
        current = self.stage("security")
        return [h["data"] for h in self.history("security")] + ([current] if current else [])

    def boundary_checks(self) -> list[dict]:
        """Every boundary check so far, oldest first, the stored one last (D-159)."""
        current = self.stage("boundary")
        return [h["data"] for h in self.history("boundary")] + ([current] if current else [])

    # --- stage submission -----------------------------------------------------------
    def request_review(self, reason: str) -> None:
        """Ask for a fresh security review of an in-flight plan (PR monitoring, §5.11)
        without discarding the checkpoint, breakdown or sequence: merged PRs stay as they are."""
        self._archive("security", reason)
        self._set_status("security", "needs_rerun", reason=reason)
        self.meta["review_in_place"] = True
        self.save()

    def store_stage(self, name: str, data: dict) -> None:
        """Store a validated stage output, archiving it and later stages first."""
        in_place = name == "security" and self.meta.pop("review_in_place", False)
        if in_place:
            self._set_status("security", "missing")
        else:
            self.invalidate_from(name, f"{name} re-submitted")
        _write(self.dir / "stages" / f"{name}.json", data)
        self._register_open_items(name, data)
        self._refresh_status(name)
        if name == "approval" and not data["approved"]:
            self.answers["guidance"].append({"stage": "design", "from": "design checkpoint",
                                             "text": data.get("notes", ""), "at": now()})
            self._mark_rerun("design", "designer did not approve the design")
        elif name == "approval" and data.get("notes", "").strip():
            # approved with notes: they steer the breakdown and sequencing (D-82)
            for later in ("decompose", "sequence"):
                self.answers["guidance"].append({"stage": later, "from": "design checkpoint (approved with notes)",
                                                 "text": data["notes"], "at": now()})
        if name == "security":
            changes = [f for f in data["findings"] if f.get("design_change")]
            if changes:
                self._route("security", changes)
            elif in_place:
                # the fixes of a re-review are placed again; merged PRs stay frozen (§5.11)
                self.answers["guidance"].append({
                    "stage": "sequence", "from": "security re-review",
                    "text": "The security review ran again: build each of its fixes into a PR (`security`).",
                    "at": now()})
                self._set_status("sequence", "needs_rerun", reason="security review ran again")
                self._archive("boundary", "security review ran again")
                self._set_status("boundary", "missing")
        if name == "boundary" and data["findings"]:
            self._route("boundary", data["findings"])
        self.save()

    # --- a review sends the plan back (D-155, D-180) ---------------------------------
    LOOPS = {"security": "design_from_security", "boundary": "replan_from_boundary"}

    def rounds(self) -> dict[str, int]:
        return {loop: self.meta.get("rounds", {}).get(loop, 0) for loop in self.LOOPS.values()}

    def _route(self, review: str, findings: list[dict]) -> None:
        """Round one goes straight back. From round two the designer decides per finding:
        ``rerun`` as the review asks, or ``carry`` it as a requirement for the builder and keep
        the plan (D-180). Neither loop has a bound otherwise."""
        loop = self.LOOPS[review]
        rounds = self.meta.setdefault("rounds", {})
        n = rounds[loop] = rounds.get(loop, 0) + 1
        if n < 2:
            self._rerun_for(review, findings, n)
            return
        asks = "design changes" if review == "security" else "replans"
        for f in findings:
            pid = self._new_id("PA")
            self.answers["pauses"][pid] = {
                "kind": "route", "stage": review, "status": "open", "decision": None, "notes": None,
                "raised": now(), "route": {"review": review, "finding": f["id"], "round": n},
                "summary": f"{f['id']}: {f['summary']}",
                "gap": f.get("issue", ""),
                "where": [f["after"]] if f.get("after") else [],
                "proposal": f.get("design_change") or f.get("replan"),
                "why": [{"kind": "opinion",
                         "text": f"This is round {n} of the {review} review sending the plan back for {asks} "
                                 "(D-180). rerun: do as it asks, with another review after. carry: keep the plan "
                                 "and hand the finding on as a requirement, no re-run."}],
                "alternative": ("carry: the finding becomes a required fix a PR builds in; the design stays as it is"
                                if review == "security" else
                                "carry: the gap is recorded on the PR it follows, for the builder and your review"),
            }
        self._refresh_status(review)

    def _rerun_for(self, review: str, findings: list[dict], n: int) -> None:
        ids = ", ".join(f["id"] for f in findings)
        if review == "security":
            for f in findings:
                reopened = (f"Re-opens {f['reopens']['ref']} ({f['reopens']['why_now']}): "
                            if f.get("reopens") else "")
                self.answers["guidance"].append({
                    "stage": "design", "from": f"security finding {f['id']}",
                    "text": reopened + f["design_change"], "at": now()})
            self._mark_rerun("design", f"security review requires design changes: {ids} (round {n})")
            return
        # a gap between PRs is fixed by re-planning them, never by adding notes (D-149); a gap
        # only a design change closes goes back to the design and its review first (D-155)
        for f in findings:
            text = (f"After {f['after']}: {f['summary']}. {f['replan']} (from boundary check {f['id']}). "
                    "Every state between PRs must be correct and expose nothing.")
            for stage in ("decompose", "sequence"):
                self.answers["guidance"].append({
                    "stage": stage, "from": f"boundary check {f['id']}", "text": text, "at": now()})
        redesign = [f for f in findings if f.get("design_change")]
        for f in redesign:
            self.answers["guidance"].append({
                "stage": "design", "from": f"boundary check {f['id']}",
                "text": f"After {f['after']}: {f['summary']}. {f['design_change']}", "at": now()})
        if redesign:
            self._mark_rerun("design", "the boundary check found a gap only a design change closes: "
                             + ", ".join(f["id"] for f in redesign) + f" (round {n})")
        else:
            self._mark_rerun("decompose", f"the boundary check found a gap between PRs: {ids} (round {n})")

    def _apply_routes(self, review: str) -> None:
        """Every route pause of this review is decided: carry the carried findings in the stored
        review and re-run for the rest, or move on when nothing re-runs (D-180)."""
        pending = [p for p in self.answers["pauses"].values()
                   if p["kind"] == "route" and p["stage"] == review and p["status"] == "resolved"
                   and not p.get("applied")]
        if not pending:
            return
        data = self.stage(review)
        by_id = {f["id"]: f for f in data["findings"]}
        n = max(p["route"]["round"] for p in pending)
        rerun = []
        for p in pending:
            f = by_id[p["route"]["finding"]]
            if p["decision"] == "carry":
                f["carried"] = {"round": n, "notes": p["notes"] or "", "at": now()}
                if review == "security":
                    # the change asked for becomes the requirement a PR builds in (D-149)
                    f.setdefault("requirement", "Carried by the designer without a design change: " + f["design_change"])
                    f["carried"]["design_change"] = f.pop("design_change")
                    self.answers["guidance"].append({
                        "stage": "security", "from": f"designer (round {n})",
                        "text": f"{f['id']} was carried as a requirement, not a design change; do not ask for "
                                f"the design change again. Designer's notes: {p['notes'] or 'none'}.", "at": now()})
            else:
                rerun.append(f)
            p["applied"] = True
        _write(self.dir / "stages" / f"{review}.json", data)
        if rerun:
            self._rerun_for(review, rerun, n)
        else:
            self._set_status(review, "done", submitted=now())

    def _mark_rerun(self, name: str, reason: str) -> None:
        idx = STAGES.index(name)
        for later in STAGES[idx + 1:]:
            self._archive(later, reason)
            self._set_status(later, "missing")
            self._withdraw_open_items(later)
        self._set_status(name, "needs_rerun", reason=reason)

    def _new_id(self, kind: str) -> str:
        self.answers["counter"][kind] += 1
        return f"{kind}-{self.answers['counter'][kind]}"

    def _register_open_items(self, stage: str, data: dict) -> None:
        for q in data.get("questions") or []:
            qid = self._new_id("Q")
            self.answers["questions"][qid] = {
                "stage": stage, "question": q["question"], "context": q.get("context"),
                "basis": q.get("basis", []), "options": q.get("options") or [],
                "recommendation": q.get("recommendation"), "reask_of": q.get("reask_of"),
                "status": "open", "answer": None, "proposal": None, "asked": now(),
            }
        for p in data.get("proposals") or []:
            q = self.answers["questions"][p["question_id"]]
            q["status"] = "proposed"
            q["proposal"] = {"text": p["proposal"], "summary": p["summary"], "basis": p["basis"]}
        for pause in data.get("pauses") or []:
            pid = self._new_id("PA")
            self.answers["pauses"][pid] = {**pause, "stage": stage, "status": "open",
                                           "decision": None, "notes": None, "raised": now()}
        if data.get("verify"):
            self.meta["verify"][stage] = {"requests": data["verify"], "status": "pending"}

    def open_questions(self, stage: str | None = None) -> dict[str, dict]:
        return {qid: q for qid, q in self.answers["questions"].items()
                if q["status"] in ("open", "proposed") and (stage is None or q["stage"] == stage)}

    def open_pauses(self, stage: str | None = None) -> dict[str, dict]:
        return {pid: p for pid, p in self.answers["pauses"].items()
                if p["status"] == "open" and (stage is None or p["stage"] == stage)}

    def unknown_questions(self, stage: str) -> dict[str, dict]:
        return {qid: q for qid, q in self.answers["questions"].items()
                if q["status"] == "unknown" and q["stage"] == stage}

    def _refresh_status(self, stage: str) -> None:
        if self.open_questions(stage) or self.open_pauses(stage):
            self._set_status(stage, "blocked")
        elif self.unknown_questions(stage):
            self._set_status(stage, "needs_rerun", reason="designer didn't know; research and propose")
        elif self.meta["verify"].get(stage, {}).get("status") == "pending":
            self._set_status(stage, "verify")
        elif self.status(stage) in ("blocked", "verify"):
            self._set_status(stage, "needs_rerun", reason="answers received")
        elif self.stage(stage) is not None:
            self._set_status(stage, "done", submitted=now())

    # --- designer answers -----------------------------------------------------------
    def apply_answers(self, data: dict) -> list[str]:
        """Record answers to questions, proposals and pauses. Returns problems found."""
        problems = []
        touched: set[str] = set()
        routed: set[str] = set()
        redesign: list[tuple[str, dict]] = []
        for a in data.get("answers") or []:
            q = self.answers["questions"].get(a["id"])
            if q is None:
                problems.append(f"{a['id']}: no such question")
                continue
            if q["status"] not in ("open", "proposed"):
                problems.append(f"{a['id']}: already {q['status']}")
                continue
            if a.get("accept_proposal"):
                if not q.get("proposal"):
                    problems.append(f"{a['id']}: there is no proposal to accept")
                    continue
                q.update(status="accepted_proposal", answer=q["proposal"]["text"],
                         answer_summary=q["proposal"].get("summary"), answered=now())
            elif a.get("delegate"):
                q.update(status="delegated", answer=a.get("answer") or "", answered=now())
            elif a.get("answer"):
                q.update(status="answered", answer=a["answer"], answered=now())
            elif a.get("unknown"):
                q.update(status="unknown", answered=now())
            else:
                problems.append(f"{a['id']}: give an answer, unknown: true, delegate: true, or accept_proposal: true")
                continue
            touched.add(q["stage"])
        for p in data.get("pauses") or []:
            pause = self.answers["pauses"].get(p["id"])
            if pause is None:
                problems.append(f"{p['id']}: no such pause")
                continue
            if pause["status"] != "open":
                problems.append(f"{p['id']}: already resolved")
                continue
            if pause["kind"] == "restructuring" and p["decision"] == "approved":
                problems.append(f"{p['id']}: restructuring pauses take guidance, not approval (§6.4)")
                continue
            if pause["kind"] == "route" and p["decision"] not in ("rerun", "carry"):
                problems.append(f"{p['id']}: a route pause takes rerun or carry, not {p['decision']} (D-180)")
                continue
            if pause["kind"] != "route" and p["decision"] in ("rerun", "carry"):
                problems.append(f"{p['id']}: only a route pause takes {p['decision']} (D-180)")
                continue
            pause.update(status="resolved", decision=p["decision"], notes=p.get("notes") or "",
                         resolved=now())
            if pause["kind"] == "route":
                routed.add(pause["stage"])
                continue
            touched.add(pause["stage"])
            # a dependency or architecture change approved after the design checkpoint changes
            # the design, so the design and its review run again with the decision (D-155)
            if (p["decision"] == "approved" and pause["kind"] in ("architecture_change", "new_dependency")
                    and pause["stage"] in ("decompose", "sequence")):
                redesign.append((p["id"], pause))
        for stage in touched:
            if self.status(stage) in ("blocked", "verify", "done", "needs_rerun"):
                self._refresh_status(stage)
        for review in routed:
            if not self.open_pauses(review):
                self._apply_routes(review)
        for pid, pause in redesign:
            self.answers["guidance"].append({
                "stage": "design", "from": f"pause {pid} (approved after the design checkpoint)",
                "text": (f"{pause['summary']}. Proposal: {pause.get('proposal') or 'see the options'}. "
                         f"Designer's notes: {pause['notes'] or 'none'}. Write the change into the design, its "
                         f"decisions (source designer, ref {pid}) and its contracts."),
                "at": now()})
            self._mark_rerun("design", f"{pid} was approved after the design checkpoint and changes the design")
        self.save()
        return problems

    def answers_for(self, stage: str) -> dict:
        """The resolved Q&A, pause decisions and guidance a stage's rerun must see."""
        qs = {qid: q for qid, q in self.answers["questions"].items() if q["stage"] == stage}
        ps = {pid: p for pid, p in self.answers["pauses"].items() if p["stage"] == stage}
        guidance = [g for g in self.answers["guidance"] if g["stage"] == stage]
        return {"questions": qs, "pauses": ps, "guidance": guidance}

    def approved_pauses(self) -> list[dict]:
        return [p for p in self.answers["pauses"].values() if p.get("decision") == "approved"]

    # --- verification ---------------------------------------------------------------
    def record_verification(self, stage: str, results: list[dict]) -> None:
        self.meta["verify"][stage] = {"status": "done", "results": results, "at": now()}
        self._refresh_status(stage)
        self.save()

    # --- logging (D-67) ---------------------------------------------------------------
    def log(self, kind: str, **fields) -> dict:
        """Append one event to ``.state/log.jsonl``: every rejection, acceptance,
        normalisation and internal error is kept, so prompts can be refined from
        evidence rather than memory."""
        entry = {"at": now(), "kind": kind, **fields}
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "log.jsonl").open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def log_entries(self) -> list[dict]:
        path = self.dir / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _attempt(self, stage: str) -> int:
        """1 + the rejections of ``stage`` since it was last accepted."""
        n = 0
        for e in self.log_entries():
            if e.get("stage") != stage:
                continue
            if e["kind"] == "accepted":
                n = 0
            elif e["kind"] == "rejected":
                n += 1
        return n + 1

    def record_rejection(self, stage: str, via: str, errors: list[str], data) -> dict:
        raw = data if isinstance(data, str) else json.dumps(data, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
        saved = self.dir / "rejected" / f"{now().replace(':', '')}-{stage}-{digest}.json"
        saved.parent.mkdir(exist_ok=True)
        saved.write_text(raw)
        return self.log("rejected", stage=stage, via=via, attempt=self._attempt(stage),
                        errors=errors, digest=digest, saved=str(saved.relative_to(self.dir)))

    @staticmethod
    def digest(data) -> str:
        raw = data if isinstance(data, str) else json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def record_acceptance(self, stage: str, via: str, normalised: list[str] | None = None,
                          data=None, rerun_reason: str | None = None) -> dict:
        """Log an acceptance. ``duplicate`` marks output identical to an earlier accepted
        submission of the same stage, e.g. a stale inbox file sent again (D-79)."""
        digest = self.digest(data) if data is not None else None
        earlier = {e.get("digest") for e in self.log_entries()
                   if e["kind"] == "accepted" and e.get("stage") == stage}
        return self.log("accepted", stage=stage, via=via, attempt=self._attempt(stage),
                        normalised=normalised or [], digest=digest,
                        # approvals and answers repeat legitimately (D-111)
                        duplicate=bool(digest and digest in earlier and stage in MODEL_STAGES),
                        rerun_reason=rerun_reason)

    def archive_inbox_file(self, path: Path, stage: str) -> Path | None:
        """Move an accepted file out of ``.state/inbox/`` so it can't be sent again."""
        path = Path(path).resolve()
        inbox = (self.dir / "inbox").resolve()
        if path.parent != inbox or not path.exists():
            return None
        done = inbox / "accepted"
        done.mkdir(exist_ok=True)
        dest = done / f"{stage}-{len(list(done.glob(stage + '-*'))) + 1}.json"
        path.replace(dest)
        return dest

    def record_internal_error(self, where: str, exc: BaseException) -> dict:
        return self.log("internal_error", where=where, error=f"{type(exc).__name__}: {exc}",
                        traceback="".join(traceback.format_exception(exc)))

    @staticmethod
    def error_pattern(error: str) -> str:
        """Group similar errors: drop list indices and quoted values."""
        pattern = re.sub(r"\[\d+\]", "[n]", error)
        pattern = re.sub(r"'[^']*'", "'…'", pattern)
        return re.sub(r"\b(PR|Q|PA|S|N|C|O|B|X|DEC)-\d+", r"\1-n", pattern)

    def log_summary(self) -> dict:
        entries = self.log_entries()
        rejections = [e for e in entries if e["kind"] == "rejected"]
        by_stage = Counter(e["stage"] for e in rejections)
        attempts: dict[str, int] = {}
        for e in entries:
            if e["kind"] == "accepted":
                attempts[e["stage"]] = max(attempts.get(e["stage"], 0), e.get("attempt", 1))
        patterns = Counter(self.error_pattern(err) for e in rejections for err in e.get("errors", []))
        # a pass is each time a stage's output was accepted (not counting duplicates), with
        # the reason it had to be produced again (D-84)
        passes: dict[str, dict] = {}
        for e in entries:
            if e["kind"] == "accepted" and e["stage"] not in ("answers", "approval") and not e.get("duplicate"):
                p = passes.setdefault(e["stage"], {"count": 0, "reasons": []})
                p["count"] += 1
                if e.get("rerun_reason"):
                    p["reasons"].append(e["rerun_reason"])
        return {
            "passes_by_stage": passes,
            "rejections": len(rejections),
            "rejections_by_stage": dict(by_stage),
            "max_attempts_by_stage": attempts,
            "top_errors": [{"pattern": p, "count": n} for p, n in patterns.most_common(8)],
            "normalised_values": sum(len(e.get("normalised", [])) for e in entries if e["kind"] == "accepted"),
            "internal_errors": sum(1 for e in entries if e["kind"] == "internal_error"),
            "model_errors": sum(1 for e in entries if e["kind"] == "model_error"),
        }

    # --- progress -------------------------------------------------------------------
    def next_action(self) -> dict:
        for stage in STAGES:
            status = self.status(stage)
            if stage == "approval":
                if status != "done":
                    return {"action": "approve", "stage": "approval"}
                continue
            if status == "missing":
                return {"action": "run", "stage": stage}
            if status == "needs_rerun":
                return {"action": "run", "stage": stage,
                        "reason": self.meta["stages"][stage].get("reason")}
            if status == "blocked":
                return {"action": "answer", "stage": stage,
                        "questions": self.open_questions(stage), "pauses": self.open_pauses(stage)}
            if status == "verify":
                return {"action": "verify", "stage": stage}
        return {"action": "render"}

    def metrics(self) -> dict:
        # withdrawn questions were replaced before anyone answered them: not "asked"
        everything = list(self.answers["questions"].values())
        qs = [q for q in everything if q["status"] != "withdrawn"]
        by_stage: dict[str, int] = {}
        for q in qs:
            by_stage[q["stage"]] = by_stage.get(q["stage"], 0) + 1
        return {
            "questions_asked": len(qs),
            "questions_withdrawn": len(everything) - len(qs),
            "questions_by_stage": by_stage,
            "reasks": sum(1 for q in qs if q.get("reask_of")),
            "answered_unknown_then_proposed": sum(1 for q in qs if q["status"] == "accepted_proposal"),
            "questions_delegated": sum(1 for q in qs if q["status"] == "delegated"),
            "pauses": len(self.answers["pauses"]),
            "rounds": self.rounds(),
            "mismatches": len(self.meta.get("mismatches", [])),
            **self.log_summary(),
        }
