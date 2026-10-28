# Stage: decompose

Break the approved design into **feature → components → leaf tasks**.

- Number components `1`, `2`, … and leaves `1.1`, `1.2`, … under their component.
- Phrase each leaf as a **deliverable**: verb + result + conditions ("Add the favorites
  table with a unique (user_id, project_id) constraint"), not layer work ("database
  changes"). Tag its `layer` and name its `files` where possible.
- **Stopping rule:** a leaf is final when a builder could do it with no open decision —
  its contract is fixed, its files are named, its outcome is stated. If not, break it
  down further or ask a question.
- **Altitude:** a leaf names what must exist, in which file, and the pattern to follow.
  The code inside it (variable names, expressions, template markup, function bodies) is
  the builder's. The contracts already fix the public surface; don't restate or extend
  them in a leaf.
- **Traces:** every leaf lists the outcome (`O-n`), behaviour (`B-n`), contract (`C-n`)
  or precursor (`X-n`) ids it implements. A leaf that traces to nothing is an unimplied
  capability: raise a pause instead. Every outcome, behaviour, contract and precursor
  must be implemented by at least one leaf.
- **Risk tags:** tag leaves touching a migration (`migration`), authentication or
  authorisation (`auth`) or a breaking change (`breaking`), and the leaf that switches a
  capability on (`enabling`).

## Output format

```json
{
  "components": [
    {"id": "1", "name": "Favorite storage", "leaves": [
      {"id": "1.1", "deliverable": "Add a migration creating favorites with a unique (user_id, project_id)", "layer": "database",
       "files": ["alembic/versions/0002_favorites.py"], "outcome": "Table exists after upgrade", "contracts": ["C-1"],
       "traces": ["C-1", "B-1"], "risk": ["migration"]}
    ]}
  ]
}
```
