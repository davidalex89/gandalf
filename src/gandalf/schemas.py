"""Output shapes for every model-driven stage.

These are structural only. Cross-references, grounding against the repo and the
cross-stage checks live in ``validate.py`` and ``checks.py``.
"""

from __future__ import annotations

import re

from gandalf.schema import Bool, Enum, Int, List, Obj, Opt, Spec, Str
from gandalf.schema import normalise as _normalise

# --- identifiers (R-01) -------------------------------------
O_ID, B_ID, C_ID = r"O-\d+", r"B-\d+", r"C-\d+"
N_ID, X_ID, S_ID = r"N-\d+", r"X-\d+", r"S-\d+"
DEC_ID, Q_ID, PA_ID = r"DEC-\d+", r"Q-\d+", r"PA-\d+"
E_ID = r"E-\d+"
PR_ID = r"PR-\d{2,}"
COMPONENT_ID, LEAF_ID = r"\d+", r"\d+\.\d+"
TRACE_ID = r"(O|B|C|X)-\d+"

LAYERS = ("database", "models", "api", "frontend", "tests", "config", "other")
LAYER = Enum(*LAYERS)

# A citation of a file in the target repo: "path", "path:12" or "path:12-30".
CITE = Str(r"[^:\n]+(:\d+(-\d+)?)?")

# One-line, plain-language summary for a person reading plan.md.
SUMMARY = Str()


class Basis(Spec):
    """Why something is suggested. Evidence needs a citation or a description;
    an opinion needs its reasoning."""

    shape = Obj({"kind": Enum("evidence", "opinion"), "text": Opt(Str()), "cite": Opt(Str())})

    def check(self, value, path, errors):
        before = len(errors)
        self.shape.check(value, path, errors)
        if len(errors) > before or not isinstance(value, dict):
            return
        if value["kind"] == "opinion" and not value.get("text"):
            errors.append(f"{path}.text: an opinion needs its reasoning in text")
        if value["kind"] == "evidence" and not (value.get("cite") or value.get("text")):
            errors.append(f"{path}: evidence needs a cite (file) or a text description")

    def describe(self):
        return "basis"


BASIS = Basis()
OPTION = Obj({
    "option": Str(),
    "consequences": Str(),
    "basis": List(BASIS, min_items=1),     # every option is grounded
})

QUESTION = Obj({
    "question": Str(),                    # in product terms; code is the evidence
    "context": Opt(Str(nonempty=False)),
    "basis": List(BASIS),
    "options": Opt(List(OPTION, min_items=2)),
    "recommendation": Opt(Obj({"option": Str(), "basis": List(BASIS, min_items=1)})),
    "reask_of": Opt(Str(Q_ID)),
    "about": Opt(Str()),                  # the item it settles, e.g. a security finding S-n
})

PROPOSAL = Obj({
    "question_id": Str(Q_ID),
    "summary": SUMMARY,
    "proposal": Str(),
    "basis": List(BASIS, min_items=1),
})

PAUSE = Obj({
    "kind": Enum("new_dependency", "architecture_change", "unimplied_capability", "restructuring"),
    "summary": SUMMARY,
    "gap": Str(),
    "where": List(CITE),
    "proposal": Opt(Str()),      # never set for "restructuring"
    "why": List(BASIS),
    "alternative": Opt(Str()),
    "options": Opt(List(OPTION)),  # required for "restructuring"
})

# Fields any model stage may add to raise items for the designer or ask the
# core to run the repo's tests in an isolated worktree.
OPEN_ITEMS = {
    "questions": Opt(List(QUESTION)),
    "proposals": Opt(List(PROPOSAL)),
    "pauses": Opt(List(PAUSE)),
    "verify": Opt(List(Obj({"reason": Str()}))),
}

COMMAND_FACT = Obj({"command": Str(), "evidence": List(CITE, min_items=1)})

CONTEXT = Obj({
    "confirmed": List(Obj({"fact": Str(), "evidence": List(CITE, min_items=1)})),
    "added": List(Obj({"area": Str(), "claim": Str(), "evidence": List(CITE, min_items=1)})),
    "contradictions": List(Obj({"scanner_fact": Str(), "observed": Str(), "evidence": List(CITE, min_items=1)})),
    "feature_trace": Opt(Obj({
        "feature": Str(),
        "files": List(Obj({"layer": LAYER, "path": CITE, "role": Str()}), min_items=1),
    })),
    "conventions": List(Obj({"rule": Str(), "example": CITE})),
    "test_commands": List(COMMAND_FACT),
    "run_commands": List(COMMAND_FACT),
    "check_commands": Opt(List(COMMAND_FACT)),    # lint, format, type checks the repo defines
    # What the code means for the feature's scope, for the design stage to raise
    "scope_notes": Opt(List(Obj({"note": Str(), "evidence": List(CITE, min_items=1)}))),
    **OPEN_ITEMS,
})

SOURCE = Enum("input", "designer", "accepted_proposal")

# A detail decided for the designer: either one they delegated ("decide it
# yourself", `ref` is that question) or a small one the stage decided without asking.
# Shown to the designer at the design checkpoint.
ASSUMPTION = Obj({"text": Str(), "why": Str(), "ref": Opt(Str(Q_ID))})
ITEM = Obj({"text": Str(), "source": SOURCE, "ref": Opt(Str())})


def _ided(pattern: str) -> Obj:
    return Obj({"id": Str(pattern), "text": Str(), "source": SOURCE, "ref": Opt(Str())})


INTENT = Obj({
    "title": Str(),
    "problem": ITEM,
    "outcomes": List(_ided(O_ID), min_items=1),
    "out_of_scope": List(ITEM),
    "behaviours": List(_ided(B_ID)),
    "constraints": List(ITEM),
    "decisions": List(ITEM),
    "unknowns": List(ITEM),
    "assumptions": Opt(List(ASSUMPTION)),
    **OPEN_ITEMS,
})

DECISION = Obj({
    "id": Str(DEC_ID),
    "summary": SUMMARY,
    "context": Str(),
    "decision": Str(),
    # security: a design change a security finding asked for, `ref` its S-n;
    # boundary: one a boundary finding asked for, `ref` its G-n
    "source": Obj({"kind": Enum("repo", "designer", "accepted_proposal", "security", "boundary"), "ref": Str()}),
    "alternatives": List(Str()),
    "consequences": Str(),
    "status": Enum("proposed", "accepted", "superseded"),
    "superseded_by": Opt(Str(DEC_ID)),
})

CONTRACT = Obj({
    "id": Str(C_ID),
    "name": Str(),
    "kind": Enum("schema", "type", "function", "route", "event", "config", "other"),
    "language": Str(),
    "body": Str(),           # the exact artifact, quoted word for word by every PR
    "why": Str(),
    # the file(s) its code lives in; the PR that exposes it must change one of them.
    # Required of new designs (validate.py); optional here only so format-2 plans load
    "files": Opt(List(Str(), min_items=1, unique=True)),
})

INVENTORY_KINDS = ("endpoint", "input", "stored_data", "outbound_call", "dependency",
                   "secret", "error_path", "repeatable_action")

LIMITATION = Obj({"summary": SUMMARY, "detail": Str(), "source": Str()})

EXTERNAL_KINDS = ("infrastructure", "client")

# Infrastructure another team builds; GANDALF writes a request, never the infrastructure
# itself.
# A client app is the other direction: another team's app, in another repo, that
# adopts what this plan builds. It gets a handoff and never holds anything back.
EXTERNAL = Obj({
    "id": Str(E_ID),
    "kind": Opt(Enum(*EXTERNAL_KINDS)),  # infrastructure when left out
    "summary": SUMMARY,                 # what the app needs from another team, in plain words
    "needs": Str(),                     # what the layer must do, or what the client should adopt
    "why": Str(),                       # which outcomes or behaviours depend on it
    "owner": Str(),                     # the owning team, or "unknown"
    "open_questions": List(Str()),      # what the owning team decides that could change the plan
    "source": Obj({"kind": Enum("input", "designer"), "ref": Str()}),
    # infrastructure only
    "environments": Opt(List(Str(), min_items=1)),
    "parameters": Opt(List(Obj({        # the settings the app reads, named the repo's way
        "name": Str(), "purpose": Str(), "example": Opt(Str()),
    }))),
    "exists": Opt(Enum("yes", "no", "unknown")),
    "ready_when": Opt(Str()),           # how anyone can confirm it's ready
    "after": Opt(List(Str(E_ID), unique=True)),   # dependencies that must be ready first
    # client apps only
    "contracts": Opt(List(Str(C_ID), min_items=1, unique=True)),   # what it can adopt, quoted in the handoff
    "until_then": Opt(Str()),           # what its users see until it adopts the feature
})
INFRA_FIELDS = ("environments", "parameters", "exists", "ready_when")
CLIENT_FIELDS = ("contracts", "until_then")

# What a data model change takes the place of, and the designer's call on it: the
# old shape `stays` as the source of truth, or `goes` once readers and writers have moved,
# `when` saying in which release or step. The fate is sourced from the designer, never decided
# by the design; "keeps working as today" settles how it behaves meanwhile, not whether it stays.
REPLACES = Obj({
    "what": Str(), "where": CITE,
    "fate": Enum("stays", "goes"), "when": Opt(Str()),
    "source": Obj({"kind": Enum("designer", "accepted_proposal"), "ref": Str()}),
})

DATA_MODEL_CHANGE = Obj({"change": Str(), "where": List(CITE), "replaces": Opt(REPLACES)})
COMPONENT = Obj({"name": Str(), "responsibility": Str(), "layer": LAYER})
INVENTORY_ITEM = Obj({
    "id": Str(N_ID), "kind": Enum(*INVENTORY_KINDS), "description": Str(),
    "contract": Opt(Str(C_ID)),
})
PRECURSOR = Obj({
    "id": Str(X_ID), "summary": SUMMARY,    # plain line for plan.md
    "where": CITE, "what": Str(), "why_blocks": Str(),
    "evidence": List(BASIS, min_items=1), "placement": Enum("before", "within"),
    "verified_by_running": Bool(),
})
SUSPECTED = Obj({"where": CITE, "what": Str(), "why_unconfirmed": Str()})
CAPABILITY = Obj({"name": Str(), "description": Str()})
RETIRED_ASSUMPTION = Obj({"text": Str(), "why": Str()})

DESIGN = Obj({
    "summary": Str(),
    "decisions": List(DECISION),
    "data_model": List(DATA_MODEL_CHANGE),
    "components": List(COMPONENT),
    "contracts": List(CONTRACT),
    "wiring": Str(),
    "inventory": List(INVENTORY_ITEM),
    "precursors": List(PRECURSOR),
    "suspected": List(SUSPECTED),
    "capabilities": List(CAPABILITY, min_items=1),
    "limitations": List(LIMITATION),               # known product limitations
    "external": Opt(List(EXTERNAL)),
    "assumptions": Opt(List(ASSUMPTION)),
    # intent assumptions a decision or answer has since overruled: quoted exactly
    "retired_assumptions": Opt(List(RETIRED_ASSUMPTION)),
    **OPEN_ITEMS,
})

# A design re-run's answer: only what changes, applied to the stored design by
# gandalf.patch. Decisions are added, never edited; replaced ones are named under `superseded`.
# Items with ids are added or replaced by id, or dropped by `remove`; the other sections are
# given whole when they change and left out when they don't.
DESIGN_PATCH = Obj({
    "patch": Bool(),
    "summary": Opt(Str()),
    "decisions": Opt(List(DECISION)),
    "superseded": Opt(List(Obj({"id": Str(DEC_ID), "by": Str(DEC_ID)}))),
    "contracts": Opt(List(CONTRACT)),
    "inventory": Opt(List(INVENTORY_ITEM)),
    "precursors": Opt(List(PRECURSOR)),
    "external": Opt(List(EXTERNAL)),
    "remove": Opt(List(Str(r"[A-Z]+-\d+"))),
    "data_model": Opt(List(DATA_MODEL_CHANGE)),
    "components": Opt(List(COMPONENT)),
    "wiring": Opt(Str()),
    "capabilities": Opt(List(CAPABILITY, min_items=1)),
    "limitations": Opt(List(LIMITATION)),
    "suspected": Opt(List(SUSPECTED)),
    "assumptions": Opt(List(ASSUMPTION)),
    "retired_assumptions": Opt(List(RETIRED_ASSUMPTION)),
    **OPEN_ITEMS,
})

APPROVAL = Obj({"approved": Bool(), "notes": Str(nonempty=False)})

LEAF = Obj({
    "id": Str(LEAF_ID),
    "deliverable": Str(),            # verb + result + conditions (R-11)
    "layer": LAYER,
    "files": List(Str()),
    "outcome": Str(),
    "contracts": List(Str(C_ID)),
    "traces": List(Str(TRACE_ID), min_items=1),
    "risk": List(Enum("migration", "auth", "breaking", "enabling")),
    # where in each existing file the leaf changes or extends something, found by reading
    "evidence": Opt(List(CITE)),
})

DECOMPOSE = Obj({
    "components": List(Obj({"id": Str(COMPONENT_ID), "name": Str(), "leaves": List(LEAF, min_items=1)}), min_items=1),
    **OPEN_ITEMS,
})

# One commit each, in order; a builder can be handed one step. `done_when` says what's
# true once it's committed; how to show it is the builder's work.
STEP = Obj({"what": Str(), "changes": List(Str(), min_items=1, unique=True), "done_when": Str()})

PR = Obj({
    "id": Str(PR_ID),
    "title": Str(),
    "summary": SUMMARY,
    "slug": Str(r"[a-z0-9]+(-[a-z0-9]+)*"),
    "type": Enum("inert", "enabling", "precursor", "release"),
    "layer": Opt(LAYER),
    "capability": Opt(Str()),
    "goal": Str(),
    "leaves": List(Str(LEAF_ID), min_items=1, unique=True),
    "depends_on": List(Str(PR_ID), unique=True),
    "size": Obj({"production_lines": Int(), "total_lines": Int(), "files": Int()}),
    "size_reason": Opt(Str()),
    "kept_separate_because": Opt(Str()),           # why a small PR isn't merged
    # what must already be true when the PR starts (earlier PRs landed), as statements
    "starting_state": Obj({"description": Str(), "facts": List(Str(), min_items=1)}),
    "read_first": List(Obj({"path": CITE, "why": Str()}), min_items=1),
    "contracts_used": List(Str(C_ID), unique=True),
    "contracts_exposed": List(Str(C_ID), unique=True),
    "changes": List(Obj({
        # generate: output of the repo's own tooling (migrations, codegen); path is its folder
        "path": Str(), "action": Enum("create", "modify", "delete", "generate"), "what": Str(),
        # the leaves this change implements; one file is listed once per PR even when
        # several leaves edit it. `leaf` (one id) is still accepted.
        "pattern": Opt(Str()), "leaves": List(Str(LEAF_ID), min_items=1, unique=True),
    }), min_items=1),
    "reuse": List(Obj({"what": Str(), "where": CITE})),
    # one commit each, in order; a builder can be handed one step. Every change's
    # path is in at least one step; a file may grow across several
    "steps": List(STEP),
    "conventions": List(Obj({"rule": Str(), "example": CITE})),
    "decisions": List(Str(DEC_ID), unique=True),
    "breaking": Obj({"is_breaking": Bool(), "detail": Str()}),
    "unobservable_how": Opt(Str()),
    "switch_off": Opt(Obj({"how": Str()})),
    # a release step changes behaviour without switching a capability on
    "behaviour_change": Opt(Str()),
    "rollback": Opt(Obj({"how": Str()})),
    # the data change this PR makes, in plain terms; the migration itself is produced the
    # repo's way (a `generate` change following its convention), not specified here
    "schema_change": Opt(Str()),
    "new_dependency": Opt(Str()),
    # each external dependency this PR's code uses, and what the code does while that one
    # isn't there yet
    "external": Opt(List(Obj({"id": Str(E_ID), "when_missing": Str()}))),
    # an enabling PR whose merge changes nothing until these settings are set: the
    # configuration is the switch, so external dependencies gate that step, not the merge
    "switched_on_by": Opt(Obj({"settings": List(Str(), min_items=1, unique=True)})),
    "flag": Opt(Obj({"name": Str(), "removed_by": Str(PR_ID)})),
    "removes_flag": Opt(Str()),
    "expand_contract": Opt(Obj({"role": Enum("expand", "contract"), "pair": Str(PR_ID)})),
    "done_when": List(Obj({"outcome": Str(), "traces": List(Str(TRACE_ID))}), min_items=1),
    "out_of_scope": List(Str()),
    # the security review's fixes this PR builds in: each one is in its changes,
    # steps and Done when, not added on top
    "security": Opt(List(Str(S_ID), unique=True)),
})

SEQUENCE = Obj({
    "prs": List(PR, min_items=1),
    # The between-PRs check, written down: what's true once each PR has merged and
    # whether every designer decision still holds then, not only at the end.
    "boundaries": List(Obj({
        "after": Str(PR_ID),
        "state": Str(),                 # what users and clients can do and see at this point
        "decisions_hold": Bool(),
        "gap": Opt(Str()),              # what doesn't hold, when decisions_hold is false
    }), min_items=1),
    "order_of_operations": List(Obj({
        "after": Str(PR_ID),
        "kind": Enum("merge", "deploy", "migrate", "backfill", "flag", "verify", "other"),
        "step": Str(),
        "external": Opt(Str(E_ID)),     # the external dependency this step sets up or confirms
        "switches_on": Opt(Str(PR_ID)), # this step sets the configuration that switches on that PR
    })),
    **OPEN_ITEMS,
})

SEVERITIES = ("low", "medium", "high", "critical")

# The boundary check: after sequencing, a separate reviewer looks only at what's
# true between PRs. A gap is fixed by re-planning the PRs, never by adding requirements.
G_ID = r"G-\d+"
BOUNDARY = Obj({
    "checked": List(Obj({
        "after": Str(PR_ID),
        "state": Str(),                 # what the reviewer finds is true once this PR has merged
        "holds": Bool(),                # every decision and required fix holds, and nothing is exposed
    }), min_items=1),
    "findings": List(Obj({
        "id": Str(G_ID),
        "after": Str(PR_ID),
        "summary": SUMMARY,
        "issue": Str(),                 # what is exposed or wrong at that point, with evidence
        "replan": Str(),                # how the PRs must be reordered or what work must move
        # when only a design change closes the gap: what the design must change. The design
        # re-runs and is reviewed again, then the PRs are re-planned
        "design_change": Opt(Str()),
    })),
})

SECURITY = Obj({
    "checked": List(Obj({"inventory": Str(N_ID), "questions": List(Str(), min_items=1)})),
    # one per capability the design switches on (the review sees the design, not PRs)
    "abuse_cases": List(Obj({"capability": Str(), "case": Str()})),
    "findings": List(Obj({
        "id": Str(S_ID),
        "summary": SUMMARY,
        "severity": Enum(*SEVERITIES),
        "concerns": Str(rf"{N_ID}|abuse:.+"),
        "issue": Str(),
        # limitation: a product limitation the designer should knowingly accept
        "response": Enum("mitigate", "eliminate", "transfer", "accept", "limitation"),
        # what must be true; the sequence stage builds it into a PR
        "requirement": Opt(Str()),
        "external": Opt(Str(E_ID)),     # puts the requirement in that external request instead
        "citation": Opt(Str()),        # required for mitigate and eliminate
        "design_change": Opt(Str()),
        "reason": Opt(Str()),
        # re-opens something already settled, by its ref in the Settled list, saying what's new
        "reopens": Opt(Obj({"ref": Str(), "why_now": Str()})),
        # restates a requirement an earlier review placed, by its ref
        "keeps": Opt(Str()),
        # design decisions its fix goes against: the designer decides which wins
        "contradicts": Opt(List(Str(DEC_ID), unique=True)),
        # what in the brief or design the plan would break (O-, B-, DEC- ids): always required
        "breaks": Opt(List(Str(rf"{O_ID}|{B_ID}|{DEC_ID}"), unique=True)),
    })),
    # earlier requirements let go, each with a reason
    "dropped": Opt(List(Obj({"ref": Str(), "reason": Str()}))),
    **OPEN_ITEMS,
})

# Designer submissions (answers to questions, proposals and pauses).
ANSWERS = Obj({
    "answers": Opt(List(Obj({
        "id": Str(Q_ID),
        "answer": Opt(Str()),
        "unknown": Opt(Bool()),
        "accept_proposal": Opt(Bool()),
        # "decide it yourself"; `answer` may name the topic it covers
        "delegate": Opt(Bool()),
    }))),
    "pauses": Opt(List(Obj({
        "id": Str(PA_ID),
        # rerun and carry answer a route pause only: a review sending the plan back a second time
        "decision": Enum("approved", "rejected", "guidance", "rerun", "carry"),
        "notes": Opt(Str(nonempty=False)),
    }))),
})

# PR monitoring: a model's judgement of the signals code flagged on a PR's branch.
M_ID = r"M-\d+"
MONITOR = Obj({
    "signals": List(Obj({
        "id": Str(M_ID),
        "verdict": Enum("change", "harmless", "unfinished"),     # unfinished: planned work not done yet
        "what": SUMMARY,                # in plain words, what the branch does differently
        "why": Str(),                   # the evidence: what in the diff shows it
    }), min_items=1),
    "summary": Str(nonempty=False),     # one plain line for the change history ("" if all harmless)
    "contradicts": List(Str(DEC_ID), unique=True),     # design decisions the change goes against
    "design_change": Opt(Str()),        # what the design must change, when it contradicts a decision
    "contracts": List(CONTRACT),        # contracts as the code now has them (same ids)
    "prs": List(PR),                    # updated specs: this PR and any unbuilt PR it reaches
})

STAGE_SCHEMAS = {
    "context": CONTEXT,
    "intent": INTENT,
    "design": DESIGN,
    "approval": APPROVAL,
    "decompose": DECOMPOSE,
    "sequence": SEQUENCE,
    "security": SECURITY,
    "boundary": BOUNDARY,
    "answers": ANSWERS,
}


# Lists a model naturally leaves out when they'd be empty: filled in, not rejected.
EMPTY_BY_DEFAULT = {
    "design": ("precursors", "suspected"),         # bugs found in the repo: usually none
    "decompose": ("risk", "contracts"),
    "sequence": ("reuse", "steps", "conventions", "decisions", "contracts_used", "contracts_exposed",
                 "out_of_scope", "depends_on"),
    "boundary": ("findings",),
}


def _fill_empty_lists(stage: str, data: dict) -> list[str]:
    notes = []
    if stage == "decompose":
        items = [(f"components[{i}].leaves[{j}]", leaf)
                 for i, comp in enumerate(data.get("components") or []) if isinstance(comp, dict)
                 for j, leaf in enumerate(comp.get("leaves") or []) if isinstance(leaf, dict)]
    elif stage == "sequence":
        items = [(f"prs[{i}]", pr) for i, pr in enumerate(data.get("prs") or []) if isinstance(pr, dict)]
    elif stage in ("design", "boundary"):
        items = [("$", data)]
    else:
        return notes
    for where, item in items:
        for field in EMPTY_BY_DEFAULT[stage]:
            if field not in item:
                item[field] = []
                notes.append(f"{where}.{field}: left out, treated as empty")
    return notes


def normalise(stage: str, data):
    """Fix obvious spellings of allowed values before validation, and fill in empty
    lists a model left out."""
    spec = STAGE_SCHEMAS.get(stage)
    if spec is None or not isinstance(data, dict):
        return data, []
    filled = _fill_empty_lists(stage, data)
    moved = (_drop_finding_ids_from_traces(data) + _strip_reuse_notes(data) + _wrap_how(data)) if stage == "sequence" else []
    data, notes = _normalise(data, spec)
    return data, filled + moved + notes


FINDING_ID = re.compile(r"(S|N|G)-\d+")


REUSE_NOTE = re.compile(r"^(?P<path>\S+)\s+\((?:[^()]*\b(?:PR|C|DEC|X)-\d+\b[^()]*)\)$")


def _wrap_how(data: dict) -> list[str]:
    """A ``switch_off`` or ``rollback`` written as plain text is kept as ``{"how": text}``:
    the prompt described both in words, and the fast tier wrote the text alone on five PRs at once."""
    notes = []
    for pr in data.get("prs") or []:
        if not isinstance(pr, dict):
            continue
        for field in ("switch_off", "rollback"):
            value = pr.get(field)
            if isinstance(value, str) and value.strip():
                pr[field] = {"how": value.strip()}
                notes.append(f"{pr.get('id', 'a PR')}: {field} given as text, kept as {{\"how\": ...}}")
    return notes


def _strip_reuse_notes(data: dict) -> list[str]:
    """A reuse ``where`` written as ``path (PR-02, C-2)`` or ``path (added by PR-01)`` keeps the
    path and drops the note: the field is a location, and the PR that creates the file is
    in the plan."""
    notes = []
    for pr in data.get("prs") or []:
        if not isinstance(pr, dict):
            continue
        for item in pr.get("reuse") or []:
            where = item.get("where") if isinstance(item, dict) else None
            m = REUSE_NOTE.match(where.strip()) if isinstance(where, str) else None
            if m:
                item["where"] = m.group("path")
                notes.append(f"{pr.get('id', 'a PR')}: reuse location {where!r} kept as {m.group('path')!r}")
    return notes


def _drop_finding_ids_from_traces(data: dict) -> list[str]:
    """A security finding (S-n), inventory item (N-n) or boundary gap (G-n) written into a
    "done when" trace is dropped with a note. Done-when traces prove outcomes,
    behaviours, contracts and precursors; a fix is credited under the PR's `security`, and the
    coverage check still rejects a fix no PR builds in. In four of eight sequence runs this
    one slip was in the first rejection, and once it was the only problem."""
    notes = []
    for pr in data.get("prs") or []:
        if not isinstance(pr, dict):
            continue
        for i, item in enumerate(pr.get("done_when") or []):
            traces = item.get("traces") if isinstance(item, dict) else None
            if not isinstance(traces, list):
                continue
            dropped = [t for t in traces if isinstance(t, str) and FINDING_ID.fullmatch(t)]
            if dropped:
                item["traces"] = [t for t in traces if t not in dropped]
                where = pr.get("id", "a PR")
                notes.append(f"{where}: done_when[{i}] traced {', '.join(dropped)}; dropped, since done-when "
                             "traces name O-, B-, C- or X- ids and a fix is credited under `security`")
    return notes
