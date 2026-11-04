"""GANDALF: Grounded Architecture, Narrow Diffs, Agent-Led Flow."""

__version__ = "2.0.2"

# Version of the plan.json format. Raise it when the structure
# changes in a way that could break a reader.
SCHEMA_VERSION = "5"
# Version of plan.json (2.0: spec.md, PR sections, `file` only with --split). Raise it when its
# structure changes in a way a reader of plan.json must know about.
PLAN_SCHEMA_VERSION = "6"
