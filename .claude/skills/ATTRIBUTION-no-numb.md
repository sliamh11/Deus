# Attribution — Ciucky/no-numb

The `quiz-me` skill and the `.claude/hooks/nonumb-gate.sh` Stop-hook doorbell
were adapted from the open-source plugin
**[Ciucky/no-numb](https://github.com/Ciucky/no-numb)** (MIT-licensed, by
Andrei Alexandru).

| Deus file | Adapted from |
|-----------|--------------|
| `.claude/hooks/nonumb-gate.sh` | `hooks/gate.sh` — the Stop-hook "doorbell" (transcript edit-detection, `stop_hook_active` re-block trap) |
| `.claude/skills/quiz-me/SKILL.md` | `skills/quiz-me/SKILL.md` — the comprehension-quiz skill structure |

## What we changed (Deus-native, LIA-328)

- **Default OFF + env opt-in (`DEUS_NONUMB`)** instead of upstream's default-ON,
  so the Stop hook never gates the autonomous pipeline / launchd / container
  sessions (which do not inherit an interactive shell's env).
- **Five comprehension axes** (what-changed, why-this-shape, what-would-break,
  how-was-it-verified, what-to-review-later) layered onto the depth dial, with
  an added **`principle`** depth tier (the transferable lesson).
- **Length-balanced distractor + slot-rotation rules** to keep the quiz from
  being gameable by answer length or position.
- Config moved to `~/.config/deus/nonumb.json` (the Deus config dir).
- `LIA-328` flag-lint citation for the `$DEUS_NONUMB` shell gate.

Full design + roadmap: `Second Brain/Deus/Research/no-numb-comprehension-warden-spec.md`
and Linear LIA-328.

The MIT license permits this adaptation; the original copyright remains with the
no-numb author.
