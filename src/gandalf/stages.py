"""The stage engine: what runs next, on which model, and how a stage output is accepted.

Shared by the command line (``cli``), the Claude Code hook (``hook``) and the model-command
adapter (``adapters.command``). It lives here rather than in ``cli`` so the adapter can use it
without importing the command line, which itself imports the adapter.
"""

from __future__ import annotations

from gandalf import settings
from gandalf.state import MODEL_STAGES, PlanState


def stage_model(plan: PlanState, stage: str) -> tuple[str, str]:
    """The model for a stage's next attempt, and a note when it has moved up a tier after
    rejections, e.g. " (moved up from haiku after 2 rejections)"."""
    n = plan.rejections(stage)
    model, _, base = settings.model_for_attempt(settings.for_plan(plan), stage, n)
    return model, (f" (moved up from {base} after {n} rejections)" if model != base else "")



def escalation_note(plan: PlanState, stage: str) -> str:
    """After a rejection: when the next attempt moves up a tier, say so and how to start it.
    A stronger model starts clean rather than inheriting a weaker one's attempt."""
    if stage not in MODEL_STAGES:
        return ""
    failed = plan.log_entries()[-1].get("model")
    model, moved = stage_model(plan, stage)
    if not moved or model == failed:
        return ""
    return (f"The next attempt runs on {model}{moved}: start a fresh agent on it with "
            f"`gandalf prompt {stage} --out \"{plan.plan_dir}\"` instead of sending these problems back.")



def describe_next(plan: PlanState) -> str:
    action = plan.next_action()
    a = action["action"]
    if a == "run":
        why = f" ({action['reason']})" if action.get("reason") else ""
        draft = (" The sequence is a draft until the boundary check passes."
                 if action["stage"] == "boundary" else "")
        model, moved = stage_model(plan, action["stage"])
        return (f"next: produce the {action['stage']} stage on {model}{moved}{why} — "
                f"`gandalf prompt {action['stage']} --out \"{plan.plan_dir}\"`.{draft}")
    if a == "answer":
        lines = [f"next: the designer answers open items from {action['stage']} (ask one at a time):"]

        def basis(items, indent="     "):
            for b in items:
                cite = f" ({b['cite']})" if b.get("cite") else ""
                lines.append(f"{indent}[{b['kind']}] {b['text']}{cite}")

        for qid, q in action["questions"].items():
            lines.append(f"  {qid}: {q['question']}")
            if q.get("context"):
                lines.append(f"     context: {q['context']}")
            basis(q.get("basis", []))
            for o in q.get("options") or []:
                lines.append(f"     option: {o['option']} — {o['consequences']}")
                basis(o["basis"], "       ")
            rec = q.get("recommendation")
            if rec:
                lines.append(f"     the agent's recommendation: {rec['option']}")
                basis(rec["basis"], "       ")
            elif q.get("options"):
                lines.append("     (no option is favoured by the evidence)")
            if q["status"] == "proposed":
                lines.append(f"     the agent researched it and proposes: {q['proposal']['text']}")
                basis(q["proposal"]["basis"], "       ")
                lines.append("     (accept the proposal, or give your own answer)")
        for pid, p in action["pauses"].items():
            lines.append(f"  {pid} pause [{p['kind']}]: {p['summary']}")
            lines.append(f"     gap: {p['gap']}")
            for w in p.get("where", []):
                lines.append(f"     where: {w}")
            if p.get("proposal"):
                lines.append(f"     proposal: {p['proposal']}")
            basis(p.get("why", []))
            if p.get("alternative"):
                lines.append(f"     alternative without the change: {p['alternative']}")
            for o in p.get("options", []):
                lines.append(f"     option: {o['option']} — {o['consequences']}")
            if p.get("kind") == "route":
                lines.append("     decide: rerun (do as the review asks) or carry (keep the plan; the finding goes "
                             "to the builder as a requirement); say why in notes")
        lines.append(f"  submit them with `gandalf submit answers FILE --out \"{plan.plan_dir}\"`")
        return "\n".join(lines)
    if a == "approve":
        return (f"next: the designer reviews {plan.plan_dir / 'design-for-review.md'} and submits "
                f"`gandalf submit approval FILE --out \"{plan.plan_dir}\"` "
                "with {\"approved\": true|false, \"notes\": \"...\"}")
    if a == "verify":
        return f"next: run the requested verification — `gandalf prompt {action['stage']} --out \"{plan.plan_dir}\"` runs it first"
    return f"next: `gandalf render --out \"{plan.plan_dir}\"`"



def submit(plan: PlanState, stage: str, data: dict, via: str = "cli", model: str | None = None) -> list[str]:
    """Validate and store a stage output or designer answers. Returns errors.
    Every rejection and acceptance is logged to ``.state/log.jsonl``."""
    from gandalf import patch as design_patch
    from gandalf.render import render_design_preview
    from gandalf.schema import check
    from gandalf.schemas import DESIGN_PATCH, normalise
    from gandalf.validate import validate_stage

    if stage in MODEL_STAGES:
        # the model the settings gave this attempt, unless the caller says otherwise
        model = model or stage_model(plan, stage)[0]
    submitted = data
    if stage == "design" and design_patch.is_patch(data):
        # a re-run answers with what changed; the stored design takes the patch
        previous = plan.previous_design()
        errors = [design_patch.NO_BASE] if previous is None else check(data, DESIGN_PATCH)
        if not errors:
            data, errors = design_patch.apply(previous, data)
        if errors:
            plan.record_rejection(stage, via, errors, submitted, model=model)
            return errors
    data, normalised = normalise(stage, data)
    report = validate_stage(plan, stage, data)
    if not report.ok:
        plan.record_rejection(stage, via, report.errors, submitted, model=model)
        return report.errors
    if stage == "answers":
        problems = plan.apply_answers(data)
        if problems:
            plan.record_rejection(stage, via, problems, data)
        else:
            plan.record_acceptance(stage, via, data=data)
            if plan.next_action()["action"] == "approve":
                # a carried finding closed the review: the checkpoint page shows it
                render_design_preview(plan)
        return problems
    previous = plan.meta["stages"].get(stage, {})
    rerun_reason = previous.get("reason") if previous.get("status") == "needs_rerun" else (
        f"{stage} produced again" if plan.stage(stage) is not None or plan.history(stage) else None)
    plan.store_stage(stage, data)
    # the log keeps the digest of what was sent (a patch, on a design re-run), so a replay sends the same
    plan.record_acceptance(stage, via, normalised, submitted, rerun_reason, model=model)
    if plan.next_action()["action"] == "approve":
        # the design has been reviewed for security: the checkpoint page shows both
        render_design_preview(plan)
    return []
