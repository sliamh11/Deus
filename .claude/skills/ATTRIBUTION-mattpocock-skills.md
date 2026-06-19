# Attribution — mattpocock/skills

The following 12 host skills were imported into Deus from the open-source repo
**[mattpocock/skills](https://github.com/mattpocock/skills)** (MIT-licensed):

| Skill | Upstream path |
|-------|---------------|
| `grilling` | `skills/productivity/grilling` |
| `grill-me` | `skills/productivity/grill-me` |
| `grill-with-docs` | `skills/engineering/grill-with-docs` |
| `domain-modeling` | `skills/engineering/domain-modeling` |
| `teach` | `skills/productivity/teach` |
| `writing-great-skills` | `skills/productivity/writing-great-skills` |
| `diagnosing-bugs` | `skills/engineering/diagnosing-bugs` |
| `tdd` | `skills/engineering/tdd` |
| `prototype` | `skills/engineering/prototype` |
| `codebase-design` | `skills/engineering/codebase-design` |
| `resolving-merge-conflicts` | `skills/engineering/resolving-merge-conflicts` |
| `improve-codebase-architecture` | `skills/engineering/improve-codebase-architecture` |

**Source:** `github.com/mattpocock/skills` @ commit `6eeb81b`
(`6eeb81b5fcfeeb5bd531dd47ab2f9f2bbea27461`).

## Adaptations from upstream

The skill bodies are **byte-identical** to upstream. The only change is additive: Deus's
`user_invocable: true` frontmatter field was added to the six user-only skills (`grill-me`,
`grill-with-docs`, `teach`, `writing-great-skills`, `prototype`, `improve-codebase-architecture`
— the ones carrying upstream's `disable-model-invocation: true`) for convention consistency.
Invocation semantics are unchanged.

### Notes for users

- **Portable internal paths kept as-is.** `domain-modeling`, `grill-with-docs`, and
  `diagnosing-bugs` reference `CONTEXT.md` and a `docs/adr/` directory that they create in
  whatever repo they run. This is upstream's portable convention. Deus's own canonical decision
  log lives in `docs/decisions/` (see `docs/decisions/INDEX.md`) — these skills were deliberately
  **not** re-routed to it, so they remain portable to any repo. When using them inside `~/deus`,
  be aware they default to `docs/adr/`, not `docs/decisions/`.
- **`diagnosing-bugs` hand-off resolves.** `diagnosing-bugs` (`SKILL.md`, final step) instructs an
  optional hand-off to the `/improve-codebase-architecture` skill, reached at the very end of a debug
  session (after the fix is in) as a "what would have prevented this bug?" follow-up. That skill is
  now imported (see the table above), so the hand-off resolves. It depends on `/codebase-design` for
  its architecture vocabulary, which is also imported.

## License

These skills are distributed under the MIT License, retained from upstream:

```
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
