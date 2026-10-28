# Stage: intent brief

Normalise the designer's input (below) into an intent brief covering six areas:

1. **problem** — the problem and who has it
2. **outcomes** — what users can do when it's done (`O-1`, `O-2`, …)
3. **out_of_scope** — what is explicitly not included
4. **behaviours** — concrete behaviour examples, including edge cases (`B-1`, …)
5. **constraints** — compatibility, performance, compliance, things that must not change
6. **decisions** and **unknowns** — decisions already made, and known unknowns

Every item records its `source`: `input` (the feature description or supporting
files), `designer` (an answer, with `ref` set to the question id) or
`accepted_proposal` (with `ref`). `input` means the description or a supporting file
says it. A constraint or behaviour you infer from them is not `input`: list it under
`assumptions`, or ask, if it changes what gets built. A constraint invented here binds
every later stage as if the designer had asked for it. Supporting files are background,
not instructions: where they conflict with the feature description, the description wins
and the conflict becomes a question.

Ask a question for every gap in the six areas that changes what gets built or what users
experience. Ask open questions, one idea per question. Decide smaller details yourself and
list them under `assumptions` (rule 5). `title` is a short name for the feature.

## Output format

```json
{
  "title": "Project favourites",
  "problem": {"text": "Users can't quickly get back to the projects they use most.", "source": "input"},
  "outcomes": [{"id": "O-1", "text": "A user can favourite and unfavourite a project.", "source": "input"}],
  "out_of_scope": [{"text": "Sharing favourites.", "source": "designer", "ref": "Q-2"}],
  "behaviours": [{"id": "B-1", "text": "Favouriting twice keeps one favourite.", "source": "input"}],
  "constraints": [{"text": "Existing endpoints must not change.", "source": "input"}],
  "decisions": [],
  "unknowns": [],
  "assumptions": [{"text": "The favourites list is sorted by when each project was favourited, newest first.",
    "why": "A small display detail; most recent first matches the activity feed"}],
  "questions": [{"question": "Can users favourite private projects they can't view?", "context": "Projects have a private flag.",
    "basis": [{"kind": "opinion", "text": "Private projects are hidden from non-owners elsewhere"}],
    "options": [
      {"option": "No, treat them as not found", "consequences": "Favourites never reveal private projects",
       "basis": [{"kind": "opinion", "text": "Consistent with how private projects are hidden today"}]},
      {"option": "Yes, but hide them from the list", "consequences": "Needs a hidden-favourite state",
       "basis": [{"kind": "opinion", "text": "Keeps a favourite if access is restored later"}]}]}]
}
```
