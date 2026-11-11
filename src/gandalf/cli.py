"""Command-line interface: scan, run, prompt, submit, render, repair, deliver, monitor."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from gandalf import __version__, settings
from gandalf.repo import GuardError, default_plan_dir, guard_plan_dir, identify, is_inside
from gandalf.scan import scan_repo
from gandalf.settings import SettingsError
from gandalf.state import MODEL_STAGES, OldPlanError, PlanExistsError, PlanState
from gandalf.stages import describe_next, escalation_note, stage_model, submit


def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str) -> None:
    print(f"gandalf: {msg}", file=sys.stderr)


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _feature_title(text: str | None, title: str | None) -> str:
    """Without --title, the first sentence of the first line, at most eight words."""
    if title:
        return title
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            first = SENTENCE_END.split(line, 1)[0].rstrip(".!?:;,")
            return " ".join(first.split()[:8])
    return "feature"


def open_or_create_plan(args) -> PlanState:
    """Scan the repo and create (or refresh) the plan folder outside it."""
    info = identify(args.repo)
    text = getattr(args, "feature_text", None)
    if getattr(args, "feature", None):
        text = Path(args.feature).read_text()
    title = _feature_title(text, getattr(args, "title", None))
    plan_dir = Path(args.out) if args.out else default_plan_dir(info, title)
    plan_dir = guard_plan_dir(plan_dir, info)
    scan = scan_repo(info)
    supporting = [Path(p) for p in getattr(args, "supporting", None) or []]
    flags = settings.parse_flags(getattr(args, "model", None), getattr(args, "tier", None),
                                 getattr(args, "escalate_after", None))
    plan = PlanState.create(
        plan_dir, repo=info.to_dict(), scan=scan, feature_title=title,
        feature_text=text, supporting=supporting,
        force_full=getattr(args, "full", False))
    # the plan keeps the settings it was first scanned with; a re-scan adds only new flags
    saved = plan.meta.get("models")
    layers = [("plan", saved)] if saved else [("config", settings.load_user())]
    settings.save_for_plan(plan, settings.resolve(*layers, ("command line", flags)))
    return plan


def plan_from_args(args) -> PlanState:
    return PlanState(Path(args.out).expanduser())


# --- commands ---------------------------------------------------------------------------

def cmd_scan(args) -> int:
    plan = open_or_create_plan(args)
    s = plan.scan
    _out(f"Scanned {s['repo']['path']} ({s['repo']['vcs']}, {s['file_count']} files)")
    _out(f"Plan folder: {plan.plan_dir}")
    for u in s["unknowns"]:
        _out(f"  UNKNOWN {u['area']}: {u['what']}")
    if s["repo"]["vcs"] == "none":
        _out("  Reduced mode: not a git repository (no test runs, content fingerprint instead of commit).")
    _out(describe_next(plan))
    return 0


def _load_json(path: str) -> dict:
    text = Path(path).read_text() if path != "-" else sys.stdin.read()
    from gandalf.adapters import extract_json
    return extract_json(text)


def cmd_models(args) -> int:
    """Show a plan's models per stage, or change them with --set / --tier."""
    plan = plan_from_args(args)
    current = settings.for_plan(plan)
    if args.set or args.tier or args.escalate_after is not None:
        flags = settings.parse_flags(args.set, args.tier, args.escalate_after)
        settings.save_for_plan(plan, settings.resolve(("plan", current), ("command line", flags)))
        current = settings.for_plan(plan)
    if args.json:
        _out(json.dumps({s: settings.model_for(current, s)[0] for s in settings.STAGES}, indent=2))
        return 0
    _out(f"Models for {plan.plan_dir}:")
    for line in settings.table(current):
        _out(line)
    return 0


def cmd_config(args) -> int:
    """Show the settings a new plan would get and where each comes from, or write a starter file."""
    path = settings.config_path()
    if args.init:
        if path.exists():
            _err(f"{path} already exists; edit it, or delete it to start again")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(settings.TEMPLATE)
        _out(f"Wrote {path}.")
        return 0
    resolved = settings.resolve(("config", settings.load_user()))
    if args.json:
        _out(json.dumps(resolved, indent=2))
        return 0
    _out(f"Settings file: {path}" + ("" if path.exists() else " (not created; `gandalf config --init` writes one)"))
    _out("A new plan would run:")
    for line in settings.table(resolved):
        _out(line)
    cmd = resolved["run"].get("model_cmd")
    _out(f"  gandalf run command: {cmd}" if cmd else "  gandalf run command: not set (give --model-cmd)")
    return 0


def cmd_submit(args) -> int:
    plan = plan_from_args(args)
    try:
        data = _load_json(args.file)
    except ValueError as exc:
        _err(f"could not read JSON from {args.file}: {exc}")
        return 2
    errors = submit(plan, args.stage, data, model=getattr(args, "model", None))
    if errors:
        _err(f"{args.stage} rejected ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        note = escalation_note(plan, args.stage)
        if note:
            _err(note)
        return 1
    _out(f"{args.stage} accepted.")
    if plan.log_entries()[-1].get("duplicate"):
        _out(f"warning: this {args.stage} output is identical to one accepted earlier "
             "(a stale file sent again?). It was accepted; check that's what you meant.")
    if args.file != "-":
        plan.archive_inbox_file(Path(args.file), args.stage)
    _out(describe_next(plan))
    return 0


def cmd_prompt(args) -> int:
    from gandalf import prompts

    plan = plan_from_args(args)
    stage = args.stage
    if stage is None:
        action = plan.next_action()
        if action["action"] not in ("run", "verify"):
            _out(describe_next(plan))
            return 0
        stage = action["stage"]
    if stage not in MODEL_STAGES:
        _err(f"no prompt for {stage!r}; stages are: {', '.join(MODEL_STAGES)}")
        return 2
    from gandalf.state import STAGES
    missing = [s for s in STAGES[:STAGES.index(stage)] if plan.status(s) != "done"]
    if missing:                          # a clear answer, not an internal error
        _err(f"the {stage} prompt needs the {missing[0]} stage first: it isn't complete yet.")
        _out(describe_next(plan))
        return 1
    if plan.status(stage) == "verify":
        from gandalf.worktree import run_verification
        _out(f"Running the requested verification for {stage} in an isolated worktree...", )
        run_verification(plan, stage)
    text = prompts.build(plan, stage, excerpts=args.excerpts, budget=args.budget)
    model, moved = stage_model(plan, stage)
    # on stderr, so it reaches whoever runs the stage without becoming part of the prompt
    print(f"Run the {stage} stage on {model}{moved}.", file=sys.stderr)
    if args.file:
        _write_prompt(plan, args.file, text, f"Prompt for {stage}")
    else:
        print(text)
    return 0


def _write_prompt(plan: PlanState, file: str, text: str, what: str) -> None:
    """A prompt is plan content, so it never lands inside the target repo either."""
    path = Path(file).expanduser()
    if is_inside(path.resolve(), Path(plan.meta["repo"]["path"])):
        raise GuardError(f"refusing to write {what.lower()} inside the target repo ({plan.meta['repo']['path']}); "
                         "choose a path outside it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _out(f"{what} written to {file}")


def cmd_render(args) -> int:
    from gandalf.render import RenderError, render_plan

    plan = plan_from_args(args)
    try:
        result = render_plan(plan, split=getattr(args, "split", False))
    except RenderError as exc:
        _err(str(exc))
        return 1
    _out(f"Rendered to {plan.plan_dir}:")
    for f in result.files:
        _out(f"  {f.relative_to(plan.plan_dir)}")
    for w in result.report.warnings:
        _out(f"  warning: {w}")
    for note in result.notes:
        _out(f"  note: {note}")
    return 0


def cmd_run(args) -> int:
    from gandalf.adapters.command import run_loop

    plan = open_or_create_plan(args)
    template = args.model_cmd or settings.for_plan(plan)["run"].get("model_cmd")
    if not template:
        _err("give --model-cmd, or set [run] model_cmd in " + str(settings.config_path())
             + ' (for example "claude -p --model {model}")')
        return 2
    try:
        return run_loop(plan, template, budget=args.budget, max_retries=args.retries)
    except EOFError:
        _err(f"input closed before the designer answered; resume with `gandalf run --out \"{plan.plan_dir}\" ...` "
             "or carry on with `gandalf prompt`")
        return 2


def cmd_deliver(args) -> int:
    from gandalf.repair import deliver

    plan = plan_from_args(args)
    merged = [m.strip() for m in args.merged.split(",") if m.strip()]
    try:
        _out(deliver(plan, args.dependency, Path(args.details), merged, args.as_requested))
    except (ValueError, OSError) as exc:
        _err(str(exc))
        return 1
    _out(describe_next(plan))
    return 0


def cmd_repair(args) -> int:
    from gandalf.repair import repair

    plan = plan_from_args(args)
    merged = [m.strip() for m in (args.merged or "").split(",") if m.strip()]
    try:
        summary = repair(plan, Path(args.report), merged, args.from_stage)
    except ValueError as exc:
        _err(str(exc))
        return 1
    _out(summary)
    _out(describe_next(plan))
    return 0


# --- PR monitoring (optional module) ---------------------------------------------------

def _after_change(plan: PlanState, e: dict) -> None:
    """Say what happened to a change and bring the rendered plan up to date."""
    from gandalf import monitor
    from gandalf.render import RenderError, render_plan

    status = e["status"]
    if status == "harmless":
        _out(f"{e['id']}: nothing that changes the plan; {e['pr']} still matches its spec.")
    elif status == "unfinished":
        _out(f"{e['id']}: {e['summary']}. The plan stays as it is; the builder's note is {plan.plan_dir / e['note']}")
    elif status == "revert":
        _out(f"{e['id']}: {e['summary']}. This can't be accepted: the builder's note is {plan.plan_dir / e['note']}")
    elif status == "rejected":
        _out(f"{e['id']} rejected. The builder's note is {plan.plan_dir / e['note']}")
    elif status == "proposed":
        _out("A change that reaches beyond its own PR, for the designer to decide:")
        for line in monitor.proposal_lines(plan, e):
            _out("  " + line)
        if e.get("note"):
            _out(f"  meanwhile, what's still missing went to the builder: {plan.plan_dir / e['note']}")
        _out(f"  approve with `gandalf monitor approve {e['id']} --out \"{plan.plan_dir}\"`, "
             f"or reject with `gandalf monitor reject {e['id']} --out \"{plan.plan_dir}\"`")
    elif status in ("absorbed", "approved"):
        applied = e.get("applied", {})
        if applied.get("replanned_from"):
            _out(f"{e['id']} {status}: it goes against a design decision, so the design goes back for re-planning.")
        else:
            what = ", ".join(applied.get("prs", []) + applied.get("contracts", [])) or "nothing"
            _out(f"{e['id']} {status}: {e['summary']}. Updated: {what}.")
        if plan.next_action()["action"] == "render":
            try:
                render_plan(plan)
                _out("The rendered plan is up to date.")
            except RenderError as exc:
                _err(str(exc))
        else:
            _out(describe_next(plan))


def _judge_with_model(plan: PlanState, e: dict, model_cmd: str, retries: int) -> tuple[dict, list[str]]:
    from gandalf import monitor, prompts
    from gandalf.adapters import extract_json
    from gandalf.adapters.command import call_model

    prompt = prompts.build_monitor(plan, e)
    errors: list[str] = []
    for _ in range(retries + 1):
        text = prompt if not errors else (prompt + "\n\n## Your previous reply was rejected\n\n"
                                          + "\n".join(f"- {x}" for x in errors) + "\n\nFix these and reply again.")
        try:
            data = extract_json(call_model(model_cmd, text))
        except ValueError as exc:
            errors = [f"could not read a JSON object from the reply: {exc}"]
            continue
        e, errors = monitor.submit_judgement(plan, e["id"], data)
        if not errors:
            break
    return e, errors


def cmd_monitor(args) -> int:
    from gandalf import monitor, prompts

    plan = plan_from_args(args)
    try:
        if args.action in ("on", "off"):
            _out(monitor.switch(plan, args.action == "on"))
            return 0
        if args.action == "history":
            entries = [e for e in monitor.history(plan) if e["status"] != "replaced"]   # superseded checks
            for e in entries:
                _out(f"{e['id']} {e['at'][:16]} {e['pr']} ({e['branch']}): {e['status']}"
                     + (f" — {e['summary']}" if e.get("summary") else ""))
            if not entries:
                _out("No changes recorded.")
            return 0
        if args.action == "check":
            if not (args.target and args.branch):
                _err("check needs a PR and --branch, e.g. `gandalf monitor check PR-03 --branch my-branch`")
                return 2
            merged = [m.strip() for m in (args.merged or "").split(",") if m.strip()]
            e = monitor.check_branch(plan, args.target, args.branch, args.base, args.done, merged)
            if e is None:
                _out(f"{args.target} still matches its spec: nothing on {args.branch} reaches other PRs.")
                return 0
            template = args.model_cmd or settings.for_plan(plan)["run"].get("model_cmd")
            if e["status"] == "judging" and template:
                model, _ = settings.model_for(settings.for_plan(plan), "monitor")
                e, errors = _judge_with_model(plan, e, settings.command_for(template, model), args.retries)
                if errors:
                    _err(f"{e['id']}: the model's judgement was rejected ({len(errors)} problem(s)):")
                    for x in errors:
                        print(f"  - {x}", file=sys.stderr)
                    return 1
            if e["status"] == "judging":
                rules = [s for s in e["signals"] if s["rule"]]
                if rules:      # say so now, not only once the judgement comes back
                    _out(f"{e['id']}: {len(rules)} rule break(s) on {args.branch}, which no judgement can accept:")
                    for s in rules:
                        _out(f"  {s['id']} {s['what']}")
                _out(f"{e['id']}: {len(monitor.judged(e))} signal(s) on {args.branch} for a model to judge:")
                for s in monitor.judged(e):
                    _out(f"  {s['id']} {s['what']}")
                _out(f"  `gandalf monitor prompt {e['id']} --out \"{plan.plan_dir}\"`, then "
                     f"`gandalf monitor submit {e['id']} FILE --out \"{plan.plan_dir}\"`")
                return 0
            _after_change(plan, e)
            return 0
        if not args.target:
            _err(f"{args.action} needs a change id, e.g. CH-1")
            return 2
        if args.action == "prompt":
            e = monitor.entry(plan, args.target)
            if e["status"] != "judging":
                _err(f"{e['id']} is {e['status']}; there's nothing to judge")
                return 1
            text = prompts.build_monitor(plan, e)
            if args.file:
                _write_prompt(plan, args.file, text, f"Prompt for {e['id']}")
            else:
                print(text)
            return 0
        if args.action == "submit":
            if not args.file:
                _err("submit needs the model's reply: `gandalf monitor submit CH-n FILE`")
                return 2
            try:
                reply = _load_json(args.file)
            except ValueError as exc:
                _err(f"could not read JSON from {args.file}: {exc}")
                return 2
            e, errors = monitor.submit_judgement(plan, args.target, reply)
            if errors:
                _err(f"{e['id']} judgement rejected ({len(errors)} problem(s)):")
                for x in errors:
                    print(f"  - {x}", file=sys.stderr)
                return 1
            _after_change(plan, e)
            return 0
        e = monitor.decide(plan, args.target, args.action == "approve", args.notes or "")
        _after_change(plan, e)
        return 0
    except monitor.MonitorError as exc:
        _err(str(exc))
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gandalf", description=(
        "Turn a feature idea into sequenced, self-contained PR specs grounded in an existing codebase."))
    p.add_argument("--version", action="version", version=f"gandalf {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_plan_target(sp, create: bool):
        sp.add_argument("--repo", required=create, help="path to the target codebase")
        sp.add_argument("--out", required=not create,
                        help="plan folder (default: ~/gandalf-plans/<repo>/<feature>/; never inside the repo)")

    s = sub.add_parser("scan", help="scan the target repo (no model) and create the plan folder")
    add_plan_target(s, True)
    feat = s.add_mutually_exclusive_group()
    feat.add_argument("--feature", help="file with the feature description")
    feat.add_argument("--feature-text", help="the feature description itself")
    s.add_argument("--title", help="short feature name, used for the plan folder")
    s.add_argument("--supporting", nargs="*", help="supporting files (briefs, notes, mockups)")
    s.add_argument("--full", action="store_true", help="always produce the full tier")
    s.add_argument("--model", action="append", metavar="STAGE=VALUE",
                   help="run a stage on a tier (strong, standard, fast) or a named model; repeatable")
    s.add_argument("--tier", action="append", metavar="TIER=MODEL", help="the model behind a tier; repeatable")
    s.add_argument("--escalate-after", type=int, metavar="N",
                   help="rejections in a row before a stage moves up a tier (0 turns it off)")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("run", help="plan start to finish with a model command, asking you questions here")
    add_plan_target(r, True)
    rfeat = r.add_mutually_exclusive_group(required=True)
    rfeat.add_argument("--feature", help="file with the feature description")
    rfeat.add_argument("--feature-text", help="the feature description itself")
    r.add_argument("--title", help="short feature name, used for the plan folder")
    r.add_argument("--supporting", nargs="*", help="supporting files")
    r.add_argument("--model-cmd", help='command that reads a prompt on stdin and prints JSON; {model} becomes '
                                        'each stage\'s model, e.g. "claude -p --model {model}" (default: [run] model_cmd in the settings file)')
    r.add_argument("--model", action="append", metavar="STAGE=VALUE",
                   help="run a stage on a tier (strong, standard, fast) or a named model; repeatable")
    r.add_argument("--tier", action="append", metavar="TIER=MODEL", help="the model behind a tier; repeatable")
    r.add_argument("--escalate-after", type=int, metavar="N",
                   help="rejections in a row before a stage moves up a tier (0 turns it off)")
    r.add_argument("--budget", type=int, default=60_000, help="max characters of repo excerpts per prompt")
    r.add_argument("--retries", type=int, default=3, help="re-prompts allowed when output fails validation")
    r.add_argument("--full", action="store_true", help="always produce the full tier")
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("prompt", help="print the prompt for a stage (default: the next one)")
    pr.add_argument("stage", nargs="?", choices=MODEL_STAGES)
    pr.add_argument("--out", required=True, help="plan folder")
    pr.add_argument("--file", help="write the prompt here instead of printing it")
    pr.add_argument("--excerpts", action="store_true",
                    help="include repo excerpts (for models that can't read the repo themselves)")
    pr.add_argument("--budget", type=int, default=60_000, help="max characters of repo excerpts")
    pr.set_defaults(func=cmd_prompt)

    sm = sub.add_parser("submit", help="validate and store a stage output, designer answers or approval")
    sm.add_argument("stage", choices=(*MODEL_STAGES, "approval", "answers"))
    sm.add_argument("file", help="JSON file ('-' for stdin)")
    sm.add_argument("--out", required=True, help="plan folder")
    sm.add_argument("--model", help="the model that produced it, if not the one the settings name")
    sm.set_defaults(func=cmd_submit)

    md = sub.add_parser("models", help="show or change which model runs each stage of a plan")
    md.add_argument("--out", required=True, help="plan folder")
    md.add_argument("--set", action="append", metavar="STAGE=VALUE", help="a tier or a model for a stage; repeatable")
    md.add_argument("--tier", action="append", metavar="TIER=MODEL", help="the model behind a tier; repeatable")
    md.add_argument("--escalate-after", type=int, metavar="N",
                    help="rejections in a row before a stage moves up a tier (0 turns it off)")
    md.add_argument("--json", action="store_true", help="print stage -> model as JSON")
    md.set_defaults(func=cmd_models)

    cf = sub.add_parser("config", help="show the model settings a new plan would get")
    cf.add_argument("--init", action="store_true", help="write a starter settings file")
    cf.add_argument("--json", action="store_true", help="print the resolved settings as JSON")
    cf.set_defaults(func=cmd_config)

    rd = sub.add_parser("render", help="write the plan files once every stage is complete")
    rd.add_argument("--split", action="store_true",
                    help="also write one self-contained spec per PR under prs/, for a builder that takes one file")
    rd.add_argument("--out", required=True, help="plan folder")
    rd.set_defaults(func=cmd_render)

    rp = sub.add_parser("repair", help="repair the unbuilt part of a plan after a mismatch report")
    rp.add_argument("--out", required=True, help="plan folder")
    rp.add_argument("--report", required=True, help="the builder's GANDALF MISMATCH REPORT")
    rp.add_argument("--merged", default="", help="comma-separated PRs that have merged, e.g. PR-01,PR-02")
    rp.add_argument("--from", dest="from_stage", default="sequence",
                    choices=("context", "design", "decompose", "sequence"),
                    help="earliest stage the mismatch affects (default: sequence)")
    rp.set_defaults(func=cmd_repair)

    dv = sub.add_parser("deliver", help="record what another team built for an external dependency (E-n)")
    dv.add_argument ("dependency", help="the external dependency, e.g. ")
    dv.add_argument("--out", required=True, help="plan folder")
    dv.add_argument("--details", required=True,
                    help="file saying what was built: setting names, service details, limits, answers to its open questions")
    dv.add_argument("--as-requested", action="store_true",
                    help="it matches the request: record it and change nothing else")
    dv.add_argument("--merged", default="", help="comma-separated PRs that have merged, e.g. PR-01,PR-02")
    dv.set_defaults(func=cmd_deliver)

    mo = sub.add_parser("monitor", help="optional: compare a PR's branch with its spec and reconcile changes")
    mo.add_argument("action", choices=("on", "off", "check", "prompt", "submit", "approve", "reject", "history"))
    mo.add_argument("target", nargs="?", help="the PR to check (PR-03), or a change (CH-2)")
    mo.add_argument("file", nargs="?", help="submit: the model's reply (JSON file, '-' for stdin); "
                                             "prompt: write the prompt here")
    mo.add_argument("--out", required=True, help="plan folder")
    mo.add_argument("--branch", help="check: the PR's branch, already present locally")
    mo.add_argument("--base", help="check: the branch PRs merge into (default: origin's default, main or master)")
    mo.add_argument("--done", action="store_true", help="check: the builder says the PR is finished")
    mo.add_argument("--merged", default="", help="check: comma-separated PRs that have merged")
    mo.add_argument("--model-cmd", help='check: judge flagged signals with this command, e.g. "claude -p"')
    mo.add_argument("--retries", type=int, default=2, help="check: re-prompts allowed when a judgement is rejected")
    mo.add_argument("--notes", help="approve or reject: your notes, kept in the change history")
    mo.set_defaults(func=cmd_monitor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (GuardError, FileNotFoundError, OldPlanError, PlanExistsError, SettingsError) as exc:
        _err(str(exc))
        return 2
    except BrokenPipeError:              # the reader went away (`gandalf prompt | head`): not a bug
        return 0
    except Exception as exc:  # a GANDALF bug: log it with the plan when there is one
        where = f"gandalf {args.command}"
        try:
            plan = PlanState(Path(args.out).expanduser()) if getattr(args, "out", None) else None
        except (FileNotFoundError, OldPlanError):
            plan = None
        if plan is not None:
            plan.record_internal_error(where, exc)
            _err(f"internal error in {where}: {exc} (details logged to {plan.dir / 'log.jsonl'})")
        else:
            _err(f"internal error in {where}: {exc}")
        return 3
