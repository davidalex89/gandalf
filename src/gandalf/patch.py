"""A design re-run answers with a patch: only what changed, applied here to the stored
design. A re-run that writes the whole design again spends most of its output on text that is
already stored, and contracts can drift between passes. A patch
keeps every unchanged contract byte for byte and leaves the superseded text where it is."""
from copy import deepcopy

BY_ID = ("contracts", "inventory", "precursors", "external")
WHOLE = ("summary", "data_model", "components", "wiring", "capabilities", "limitations", "suspected",
         "assumptions", "retired_assumptions")
OPEN = ("questions", "proposals", "pauses", "verify")

NO_BASE = "patch: there is no earlier design to patch: this is the first design, so send the whole design"


def is_patch(data) -> bool:
    return isinstance(data, dict) and data.get("patch") is True


def apply(previous: dict, patch: dict) -> tuple[dict, list[str]]:
    """The previous design with the patch applied, and what in the patch couldn't be applied."""
    errors: list[str] = []
    design = {k: deepcopy(v) for k, v in previous.items() if k not in OPEN}
    ids = {d["id"] for d in design["decisions"]}
    for d in patch.get("decisions") or []:
        if d["id"] in ids:
            errors.append(f"{d['id']} is already in the design: a decision is never edited; add a new one and "
                          f"list {d['id']} under superseded with the new id as by")
        else:
            design["decisions"].append(d)
            ids.add(d["id"])
    for s in patch.get("superseded") or []:
        dec = next((d for d in design["decisions"] if d["id"] == s["id"]), None)
        if dec is None:
            errors.append(f"superseded: there is no {s['id']} to supersede")
        elif s["by"] not in ids or s["by"] == s["id"]:
            errors.append(f"superseded: {s['id']} by {s['by']}, which is neither in the design nor a new "
                          "decision in this patch")
        else:
            dec["status"] = "superseded"
            dec["superseded_by"] = s["by"]
    for key in BY_ID:
        items = patch.get(key)
        if items is None:
            continue
        current = list(design.get(key) or [])
        where = {x["id"]: i for i, x in enumerate(current)}
        for x in items:
            if x["id"] in where:
                current[where[x["id"]]] = x
            else:
                current.append(x)
        design[key] = current
    for rid in patch.get("remove") or []:
        if rid.startswith("DEC-"):
            errors.append(f"remove: {rid} is a decision, which is never removed; supersede it")
            continue
        hit = False
        for key in BY_ID:
            kept = [x for x in design.get(key) or [] if x["id"] != rid]
            hit = hit or len(kept) != len(design.get(key) or [])
            if key in design or kept:
                design[key] = kept
        if not hit:
            errors.append(f"remove: there is no {rid} in the design")
    for key in WHOLE + OPEN:
        if key in patch:
            design[key] = patch[key]
    return design, errors
