## The previous design

This is a re-run. Start from the design below and change only what the guidance and
answers require. Answer with a **patch**, not the whole design: a JSON object with
`"patch": true` holding only what changes. GANDALF applies it to the design below and checks
the result against every design rule.

- `decisions`: new decisions only, with new ids. A decision is never edited. One that is
  replaced is listed in `superseded` as `{"id": "DEC-3", "by": "DEC-9"}`; its text stays in
  the record, and the new decision says in full what it keeps and what it changes.
- `contracts`, `inventory`, `precursors`, `external`: entries added or changed, matched by
  id. Leave an unchanged contract out, so its wording stays exactly as later PRs quote it.
  `remove` lists the ids of entries that no longer apply (never a decision).
- `summary`, `data_model`, `components`, `wiring`, `capabilities`, `limitations`,
  `suspected`, `assumptions`, `retired_assumptions`: each given whole when it changes, left
  out when it doesn't.
- `questions`, `pauses`, `proposals` and `verify` go in the patch as they would in a design.

```json
{"patch": true,
 "decisions": [{"id": "DEC-9", "summary": "Favourites follow visibility", "context": "The checkpoint asked for it.",
   "decision": "The list only shows projects the user can still open.", "source": {"kind": "designer", "ref": "checkpoint"},
   "alternatives": ["Show them greyed out: needs a new display state"], "consequences": "One filter in the service.",
   "status": "accepted"}],
 "superseded": [{"id": "DEC-2", "by": "DEC-9"}],
 "contracts": [{"id": "C-1", "name": "Favorite service", "kind": "function", "language": "python",
   "body": "def list_favorites(session, user_id: int) -> list[Project]: ...", "why": "Shared by router and tests.",
   "files": ["app/services/favorite_service.py"]}],
 "assumptions": [{"text": "A hidden favourite is kept, not deleted", "why": "Access can come back"}]}
```
