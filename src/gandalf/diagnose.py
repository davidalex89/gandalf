"""Turn the validation log into a plain-language account of the mistakes caught during planning.

Every rejected submission is matched against a catalogue of known causes, each saying in
plain words what was caught. Rejected output never enters a plan: each was corrected
before its stage was accepted. Whether a rejection was the model's mistake or GANDALF's
own isn't guessed here (D-145).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Cause:
    id: str
    pattern: str
    plain: str

    def matches(self, error: str) -> bool:
        return re.search(self.pattern, error) is not None


# Specific causes first: each error is matched to the first cause that fits.
CATALOGUE = (
    Cause("monitor", r"^monitor: ",
          "A PR monitoring judgement that didn't fit the signals or the plan"),
    Cause("options-format", r"options\[\d+\]\.(option|text)|recommendation\.(option|text)",
          "Question options written in the wrong format"),
    Cause("recommendation-mismatch", r"recommendation: .* isn't one of the options",
          "A recommendation that didn't exactly match one of its options"),
    Cause("basis-missing", r"basis\[\d+\]\.text|evidence needs a cite|opinion needs its reasoning",
          "A suggestion missing its evidence or reasoning"),
    Cause("cite-not-file", r"does not match \[\^:",
          "Evidence that wasn't a file in the repo (often a question number)"),
    Cause("cite-missing-file", r"which isn't in the repo|example .* isn't in the repo",
          "A citation of a file that doesn't exist in the repo"),
    Cause("duplicate-file", r"more than once\. List each file once|which (PR-\d+) creates, but \1 does not depend on \1",
          "A file listed twice in one PR"),
    Cause("secret", r"looks like a real secret",
          "Something that looks like a real secret in the plan, or a PR skipping the repo's secret scan"),
    Cause("iac", r"is infrastructure as code",
          "A PR that would change infrastructure as code"),
    Cause("client-app", r"client app|infrastructure needs \w+$|is for client apps|names contract .*, which this design",
          "Another team's app (or infrastructure) described with the wrong fields, or waited on"),
    Cause("when-missing", r"say what the code does while it isn't there yet",
          "Code that relies on another team's work without saying what happens while it's missing"),
    Cause("external-order", r"must be set up after|sets up \S+ before",
          "Another team's work set up in the wrong order"),
    Cause("switch-on", r"switched on by configuration|switches on \S+, but no earlier step|switches on \S+, which isn't",
          "A switch-on by configuration without its step, or before other teams are ready"),
    Cause("assumption-ref", r"isn't a question the designer delegated",
          "An assumption citing a question the designer answered rather than delegated"),
    Cause("security-breaks", r"breaks \S+.*, so it must be fixed|breaks \S+, which the brief and design don't have",
          "A security finding that breaks the brief, accepted instead of fixed or naming something that isn't there"),
    Cause("security-contradiction", r"so ask the designer which wins|contradicts DEC-\d+, which isn't|contradicts \S+, which isn't a current",
          "A security fix that went against a design decision without asking the designer"),
    Cause("carried", r"earlier requirement|from the earlier review|keeps \S+, so give",
          "A security re-review that left out, or misnamed, a requirement an earlier review placed"),
    Cause("reopens", r"re-opens .*, which isn't in the settled list",
          "A security finding re-opening something by a name that wasn't on the settled list"),
    Cause("breaks-unnamed", r"leaves breaks empty",
          "A security fix that protects a stated behaviour without naming it, which would have left it optional"),
    Cause("step-files", r"isn't in this PR's changes|isn't in any step",
          "A PR's steps and its list of changes that didn't match"),
    Cause("rejection-notes", r"say what to change; a rejection with no notes",
          "A design rejected at the checkpoint without notes saying what to change"),
    Cause("retired-assumption", r"quote an intent assumption exactly",
          "A replaced assumption that doesn't match the one it replaces"),
    Cause("proposals", r"didn't know .*: research|designer marked unknown|no proposal to accept",
          "A \"don't know\" answer without a researched proposal"),
    Cause("pause-shape", r"restructuring pause|never propose restructuring|state what is proposed|give evidence or a grounded opinion",
          "A pause not framed for the designer to decide"),
    Cause("contract-imports", r"leave imports out of a contract",
          "A contract that included imports, which the repo's own conventions decide"),
    Cause("contract-file", r"exposes \S+, whose code lives in|name the files its code will live in",
          "A contract placed in a PR that doesn't change the file its code lives in"),
    Cause("one-home", r"must be in exactly one PR|must be exposed by exactly one PR|must be numbered under component",
          "A leaf or contract placed in no PR, or in more than one"),
    Cause("schema-change", r"changes the database \(a migration\), so say what data changes",
          "A PR that migrates the database without saying what data changes"),
    Cause("isolate-steps", r"needs an ordered step list",
          "A risky change (migration, auth, breaking) not isolated in ordered steps"),
    Cause("size-reason", r"Say why it's kept whole",
          "A PR changing many files without a reason to stay whole"),
    Cause("flags", r"flag|expand/contract",
          "A flag or expand/contract pair not planned whole"),
    Cause("dependency", r"depends on itself|dependency cycle|does not depend on|also creates|must come after \(depend on\)",
          "PRs in an order that doesn't work, or relying on work they don't depend on"),
    Cause("small-pr", r"a single leaf on its own",
          "A single-leaf PR without a reason to stay separate"),
    Cause("twin-enabling", r"switch on capabilities built from exactly the same work",
          "Two switch-on PRs built on the same work, without a reason"),
    Cause("forward-state", r"neither the repo nor an earlier PR|won't exist when|generates into|already exists",
          "A PR relying on a file that won't exist yet, or creating one that already does"),
    Cause("boundary", r"^boundaries:",
          "A step between PRs left unchecked, or a gap left open"),
    Cause("inert-breaking", r"inert PR can't be breaking|inert, but leaf",
          "A PR called inert that would change behaviour"),
    Cause("security-placement", r"isn't built into any PR|one PR builds each fix|has no fix for a PR to build in|"
                                r"which the security review doesn't have",
          "A security fix not built into exactly one PR"),
    Cause("coverage", r"^coverage:",
          "Something in the intent or design that no PR covers"),
    Cause("unimplied", r"unimplied capability",
          "Work that nothing in the intent or design asked for"),
    Cause("security-path", r"design changes are for medium-or-above",
          "A security fix path that didn't match the finding's severity"),
    Cause("security-incomplete", r"needs a requirement \(or a design_change\)|so it needs a reason|needs an abuse case|"
                                 r"was not checked|so give its requirement",
          "A security finding or check left incomplete"),
    Cause("boundary-check", r"^boundary:",
          "A state between PRs left unchecked, or a gap with no re-plan"),
    Cause("fix-citation", r"is a fix, so cite the control it applies",
          "A security fix that doesn't name the control it applies"),
    Cause("required-check", r"PRs name their layer|PRs state how|enabling PRs name a design capability|precursor PRs must implement",
          "A PR missing a check or statement its type requires"),
    Cause("decision-source", r"source \w+ must (reference|name)|source cites .*isn't in the repo|superseded decisions name",
          "A design decision without a valid source"),
    Cause("superseded", r"from an earlier design is missing",
          "An earlier decision deleted instead of marked superseded"),
    Cause("frozen", r"has merged and is frozen|is frozen: keep it",
          "A change to work that has already merged"),
    Cause("order", r"must be completed first",
          "A stage submitted before the stages it depends on"),
    Cause("not-json", r"could not read a JSON object|no JSON object",
          "Output that wasn't readable JSON"),
    Cause("answers", r"no such question|no such pause|: already \w+$|give an answer|take guidance, not approval",
          "An answer that didn't match an open question"),
    Cause("enum", r"is not one of",
          "A value outside the allowed list"),
    Cause("wrong-type", r"expected an? \w+, got \w+",
          "A value of the wrong type (e.g. text where a list belongs)"),
    Cause("unknown-field", r"unknown field",
          "An unexpected field in the output"),
    Cause("missing-field", r"required field is missing",
          "A required part of the output missing"),
    Cause("format", r"must not be empty|does not match|must be >=|expected true or false|needs at least \d+ item|duplicate item",
          "A value in the wrong format (empty, too short, or not matching its pattern)"),
    Cause("duplicate-id", r"duplicate (PR id|PR slug|id|finding id)|is listed twice|more than once under external",
          "The same id or entry used twice"),
    Cause("route-decision", r"a route pause takes rerun or carry|only a route pause takes",
          "A review sent the plan back a second time; the designer answers each finding with rerun or carry (D-180)"),
    Cause("fix-moved", r"a replan keeps a fix where it was",
          "GANDALF's rule: a replan adds to a plan, it does not move a security fix out of the PR that held it (D-182)"),
    Cause("unknown-ref", r"unknown|names no leaf|which isn't in this PR|isn't one of the design's capabilities|"
                         r"isn't another external dependency",
          "A reference to something that doesn't exist in the plan"),
)


def plain_error(error: str) -> str:
    """'$.prs[0].checks[1].kind: bad' -> 'prs → checks → kind: bad', capped in length."""
    path, sep, message = error.partition(": ")
    if sep and path.startswith("$"):
        path = " → ".join(re.sub(r"\[\d+\]", "", path[2:]).split("."))
        error = f"{path}: {message}"
    return error if len(error) <= 160 else error[:157] + "…"


def cause_of(error: str) -> Cause | None:
    return next((c for c in CATALOGUE if c.matches(error)), None)


def summarise(entries: list[dict]) -> dict:
    """Group rejected submissions by cause (a submission can have several causes)."""
    rows: dict[str, dict] = {}
    for e in entries:
        if e["kind"] != "rejected":
            continue
        seen = set()
        for error in e.get("errors", []):
            if error == "More checks run once these are fixed.":     # a note, not a problem (D-105)
                continue
            cause = cause_of(error)
            key = cause.id if cause else "other:" + plain_error(error)
            if key in seen:
                continue
            seen.add(key)
            row = rows.setdefault(key, {
                "cause": cause.plain if cause else f"Other: {plain_error(error)}",
                "times": 0, "stages": [],
            })
            row["times"] += 1
            if e["stage"] not in row["stages"]:
                row["stages"].append(e["stage"])
    rejected = sum(1 for e in entries if e["kind"] == "rejected")
    return {
        "rejected": rejected,
        "causes": sorted(rows.values(), key=lambda r: (-r["times"], r["cause"])),
        "normalised": sum(len(e.get("normalised") or []) for e in entries if e["kind"] == "accepted"),
        "internal_errors": sum(1 for e in entries if e["kind"] == "internal_error"),
        "model_errors": sum(1 for e in entries if e["kind"] == "model_error"),
        "duplicates": sum(1 for e in entries if e["kind"] == "accepted" and e.get("duplicate")),
    }


def render_lines(summary: dict) -> list[str]:
    """The 'Mistakes caught during planning' section of plan.md."""
    n = summary["rejected"]
    out = []
    if n == 0:
        out.append("Every stage's output was accepted first time.")
    else:
        out.append(f"GANDALF sent {n} output{'s' if n != 1 else ''} back to the model for correction. "
                   f"{'Each was' if n != 1 else 'It was'} fixed before the stage was accepted, so none of "
                   "these is in this plan.")
        out += ["", "| What was caught | Stage | Times | Status |", "|---|---|---|---|"]
        for r in summary["causes"]:
            out.append(f"| {r['cause']} | {', '.join(r['stages'])} | {r['times']} | Fixed before acceptance |")
    extras = []
    if summary["normalised"]:
        extras.append(f"{summary['normalised']} value(s) were spelled slightly differently and fixed automatically.")
    if summary["duplicates"]:
        extras.append(f"{summary['duplicates']} submission(s) repeated an earlier one exactly (a stale file?).")
    if summary["internal_errors"]:
        extras.append(f"**GANDALF itself failed {summary['internal_errors']} time(s).** That's a GANDALF bug; "
                      "details are in `.state/log.jsonl`.")
    if summary["model_errors"]:
        extras.append(f"The model command failed {summary['model_errors']} time(s).")
    if extras:
        out += [""] + [f"- {x}" for x in extras]
    out += ["", "The raw log is in `.state/log.jsonl`."]
    return out
