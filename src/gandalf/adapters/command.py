"""Command adapter: drive every stage with any model command (DESIGN.md §11.2).

The prompt goes to the command on stdin; a JSON object is read from its stdout. Output
that fails validation is sent back with the errors, up to ``max_retries`` times. The
designer answers questions, pauses and the design checkpoint here in the terminal, one
item at a time. The security stage is a fresh call with only its own prompt, so it
never sees the planning conversation.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from gandalf import prompts
from gandalf.adapters import extract_json
from gandalf.state import PlanState

MODEL_TIMEOUT = 1800


def call_model(model_cmd: str, prompt: str) -> str:
    proc = subprocess.run(shlex.split(model_cmd), input=prompt, capture_output=True, text=True,
                          timeout=MODEL_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"model command failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc.stdout


def _ask(prompt: str) -> str:
    print(prompt, end="", flush=True)
    line = sys.stdin.readline()
    if not line:
        raise EOFError("input closed")
    return line.strip()


def _show_basis(basis: list[dict]) -> None:
    for b in basis:
        cite = f" ({b['cite']})" if b.get("cite") else ""
        print(f"     [{b['kind']}] {b['text']}{cite}")


def answer_interactively(plan: PlanState, action: dict) -> dict:
    """Ask the designer each open item, one at a time. Returns an answers submission."""
    answers, pauses = [], []
    for qid, q in action["questions"].items():
        print(f"\n{qid} ({q['stage']}): {q['question']}")
        if q.get("context"):
            print(f"   Context: {q['context']}")
        _show_basis(q.get("basis", []))
        if q["status"] == "proposed":
            print(f"   The agent researched this and proposes: {q['proposal']['text']}")
            _show_basis(q["proposal"]["basis"])
            reply = _ask("   Accept the proposal? [y = accept / or type your own answer]: ")
            if reply.lower() in ("y", "yes"):
                answers.append({"id": qid, "accept_proposal": True})
            elif reply:
                answers.append({"id": qid, "answer": reply})
            else:
                answers.append({"id": qid, "unknown": True})
        else:
            options = q.get("options") or []
            for n, o in enumerate(options, 1):
                print(f"   {n}. {o['option']} — {o['consequences']}")
                _show_basis(o["basis"])
            if q.get("recommendation"):
                print(f"   The agent recommends: {q['recommendation']['option']}")
                _show_basis(q["recommendation"]["basis"])
            hint = "an option number, your own answer," if options else "your answer"
            reply = _ask(f"   Your answer ({hint} d to let the planner decide, or ? if you don't know): ")
            if reply in ("?", "") or reply.lower() in ("don't know", "dont know", "unknown"):
                answers.append({"id": qid, "unknown": True})
            elif reply.lower() in ("d", "decide", "you decide"):
                answers.append({"id": qid, "delegate": True})
            elif reply.isdigit() and 1 <= int(reply) <= len(options):
                answers.append({"id": qid, "answer": options[int(reply) - 1]["option"]})
            else:
                answers.append({"id": qid, "answer": reply})
    for pid, p in action["pauses"].items():
        print(f"\n{pid} — pause ({p['kind'].replace('_', ' ')}): {p['summary']}")
        print(f"   Gap: {p['gap']}")
        for w in p.get("where", []):
            print(f"   Where: {w}")
        if p.get("proposal"):
            print(f"   Proposal: {p['proposal']}")
        _show_basis(p.get("why", []))
        if p.get("alternative"):
            print(f"   Alternative without the change: {p['alternative']}")
        for o in p.get("options", []):
            print(f"   Option: {o['option']} — {o['consequences']}")
        if p["kind"] == "restructuring":
            notes = _ask("   Your guidance: ")
            pauses.append({"id": pid, "decision": "guidance", "notes": notes})
        else:
            reply = _ask("   Approve? [y/n]: ").lower()
            notes = _ask("   Notes (optional): ")
            pauses.append({"id": pid, "decision": "approved" if reply in ("y", "yes") else "rejected",
                           "notes": notes})
    return {"answers": answers, "pauses": pauses}


def run_loop(plan: PlanState, model_cmd: str, *, budget: int = 60_000, max_retries: int = 3) -> int:
    from gandalf.cli import describe_next, submit
    from gandalf.render import RenderError, render_plan
    from gandalf.worktree import run_verification

    print(f"Plan folder: {plan.plan_dir}")
    while True:
        action = plan.next_action()
        kind = action["action"]
        if kind == "render":
            try:
                result = render_plan(plan)
            except RenderError as exc:
                print(f"gandalf: {exc}", file=sys.stderr)
                return 1
            print(f"\nPlan written ({result.tier} tier): {plan.plan_dir / 'plan.md'}")
            return 0
        if kind == "verify":
            print(f"\nRunning the repo's tests in an isolated worktree for {action['stage']}...")
            run_verification(plan, action["stage"])
            continue
        if kind == "answer":
            submission = answer_interactively(plan, action)
            problems = submit(plan, "answers", submission, via="run")
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            continue
        if kind == "approve":
            print(f"\nDesign checkpoint. Read: {plan.plan_dir / 'design-for-review.md'}")
            ok = _ask("Approve the design? [y/n]: ").lower() in ("y", "yes")
            notes = "" if ok else _ask("What should change? ")
            submit(plan, "approval", {"approved": ok, "notes": notes}, via="run")
            continue

        stage = action["stage"]
        prompt = prompts.build(plan, stage, excerpts=True, budget=budget)
        print(f"\n[{stage}] asking the model...")
        for attempt in range(max_retries + 1):
            try:
                raw = call_model(model_cmd, prompt)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                errors = [str(exc)]
                plan.log("model_error", stage=stage, via="run", error=str(exc))
            else:
                try:
                    data = extract_json(raw)
                except ValueError as exc:
                    errors = [f"the reply had no JSON object: {exc}"]
                    plan.record_rejection(stage, "run", errors, raw)
                else:
                    errors = submit(plan, stage, data, via="run")
            if not errors:
                print(f"[{stage}] accepted.")
                break
            print(f"[{stage}] rejected ({len(errors)} problem(s)); re-prompting.", file=sys.stderr)
            prompt = (prompts.build(plan, stage, excerpts=True, budget=budget)
                      + "\n## Your previous output was rejected\n\nFix these problems and reply with "
                        "the corrected JSON object only:\n\n" + "\n".join(f"- {e}" for e in errors) + "\n")
        else:
            print(f"gandalf: {stage} still failing validation after {max_retries} retries.", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            print(describe_next(plan))
            return 1
