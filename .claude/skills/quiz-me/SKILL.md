---
name: quiz-me
description: Use right after writing or editing code or files for the user — before ending your turn — to quiz them on what was just built and confirm they actually understand it. Also triggers on "quiz me", "test my understanding", "check what I learned", "comprehension check", or recovering the understanding that vaporizes when you orchestrate an agent instead of authoring the code yourself.
user_invocable: true
---

# /quiz-me — quiz the user on what was just built

Adapted from the MIT-licensed no-numb plugin (github.com/Ciucky/no-numb); see
`.claude/skills/ATTRIBUTION-no-numb.md`. Runs **host-side**.

## Overview

After you write code for someone, they usually understand the result at a high
level — what it does, its inputs and outputs — but not the internals or the
*why*. That understanding "vaporizes." This skill has you administer a short
comprehension quiz on **what you just did this turn**, so the understanding
sticks. Being tested is itself the learning (retrieval practice); the quiz is
not just a check.

**Core principle:** quiz what *this turn* produced, from your own memory of what
you just did — not generic trivia, not the whole codebase, not what the app does
at a high level.

## When to run

- Right after a turn in which you edited or wrote files, before finishing.
- When the user runs `/quiz-me`.
- When the no-numb Stop hook (`.claude/hooks/nonumb-gate.sh`) blocks your turn
  and asks you to quiz. (That gate is DEFAULT-OFF; it fires only when the user
  has opted in via `DEUS_NONUMB` or `~/.config/deus/nonumb.json`.)

## Step 1 — Is it worth quizzing?

Look at what you changed this turn. **Bias toward quizzing.** Only skip when the
change is *genuinely cosmetic*: formatting / whitespace, a variable rename, a CSS
color tweak, a typo fix, a version bump. Everything else → quiz. **When in
doubt, quiz.** If you do skip, say so in one line and stop — don't quiz on
nothing.

## Step 2 — Read the settings

Read `~/.config/deus/nonumb.json` for `depth` (`"standard"` or `"deep"`; default
to `"standard"` if the file or key is missing). The optional `"principle"` depth
is the most advanced tier (below). Note whether this was a **code** change or a
**non-code** change (docs/prose/config) — it changes how the depth dial applies.

## Step 3 — The two dials: depth × axis

A good quiz crosses **two independent dials**. Decide each per question.

### Dial A — depth (how hard to retrieve the answer)
The single test for standard vs deep:

> **Do you need to read the code to answer it? Yes → deep. No → standard.**

- **`standard`** (default) — answerable WITHOUT opening a file, from
  understanding the *decisions*. Do **not** reference specific files, lines, or
  function names. *"Why does X break if that guard is removed?"*
- **`deep`** — only answerable BY reading the code. **Always point them to where
  to look** (file + function/region). Favor reasoning over recall. *"In the
  doorbell check in `gate.sh`, what edit would slip past it?"*
- **`principle`** (advanced, one tier up) — strip the specifics and ask the
  *transferable* lesson: what general class of bug/decision is this, and where
  else does it apply? *"This is an instance of what general failure pattern, and
  where else would it bite?"*

### Dial B — comprehension axis (which facet you probe)
Sample **across** these five axes — don't ask five of the same kind. The number
of questions scales to the change, with a **minimum floor so a quiz can't be
trivially passed with one softball**: at least **2** questions for any
non-cosmetic change, and at least **4** for a new file or a substantial refactor.
A one-line fix → 2; a new module → 4+. Don't pad past what the change warrants,
and don't force all five axes on a tiny change.

| Axis | Probes |
|---|---|
| **what changed** | name the real edit + new behavior |
| **why this shape** | the tradeoff chosen over the obvious alternative |
| **what would break** | predict a regression if a key line changed |
| **how was it verified** | name the actual test/command run — *or admit there wasn't one* |
| **what to review later** | one remaining uncertainty/risk worth flagging |

The **how-was-it-verified** axis is the highest-value one: it forces you to
confront whether you actually ran anything. If the honest answer is "it wasn't
verified," say so plainly — that surfaces the gap to the user before they move
on. **In gate-triggered mode** you (the model) already know what commands you
ran this turn and the user does not — so don't quiz them on a fact only you hold.
Instead, *disclose* it: state what was actually run (or that nothing was), and if
you want a question on this axis, ask what the verification would need to cover,
not "what did Claude run."

**Non-code changes** (docs/prose): the read-the-code test doesn't apply — ask
about *what changed* and treat depth as a difficulty knob. Config files are
code-like enough that deep/principle work normally.

**Stay below the awareness line (every mode):** never ask *"what does this app
do"* or *"what are its inputs and outputs."* That's the high-level understanding
the user already has — testing it teaches nothing. Aim at the internals, the
*why*, and the transferable lesson — the part that vaporizes.

## Step 4 — Deliver as multiple choice, one at a time

Deliver with `AskUserQuestion`, **one question per call**, with **four options**
each.

- **Plausible distractors.** The wrong options must be the misconceptions
  someone would actually hold if they didn't understand this code. Obvious-dummy
  options turn it back into trivia and let them pass by elimination.
- **Length-balanced options.** Write all four options at roughly the **same
  length and specificity**. Never let the correct answer be the longest or most
  detailed — that is a tell that lets the user pattern-match the answer without
  understanding it. (Equalize: if the correct answer needs a clause, give the
  distractors one too.)
- **Vary where the correct answer sits.** Decide the correct option's slot
  (1st–4th) *before* you write the options, and rotate it across questions
  (e.g. 2nd, 4th, 1st, 3rd). **Never default the correct answer to the first
  slot** — a quiz whose answer is always option 1 is passed by reflex.

> Multiple choice is required, not a style preference. `AskUserQuestion` is a
> tool call, so your turn stays alive and the gate can hold. A plain-text
> question would force you to end your turn and wait for a reply — which releases
> the gate. Do not switch to prose questions during a gated quiz. (The "Other"
> free-text box is fine for the rare question that needs a typed answer, since it
> stays inside the tool call.)

## Step 5 — Grade and explain

After each answer: if correct, confirm in a sentence. If wrong, show the correct
option **and explain *why*** — the specific low-level detail they missed. This
explanation is where the learning actually lands, so make it real, not a
restatement.

## Step 6 — Retake on any miss

Any wrong answer means the user retakes the **full** quiz. Regenerate it with
light rewording and reordering so they can't just memorize the answer key. **Stay
in this turn and keep going until they pass** — do not end your turn, summarize,
or move on to other work until every answer is correct (or the user deliberately
interrupts). On a pass, you're done; just conclude normally.

## Quick reference

| Depth | The test | References files/lines? |
|---|---|---|
| `standard` | answer it *without* reading code | **no** |
| `deep` | must *read* the code to answer | **yes** — point them there |
| `principle` | the transferable lesson, specifics stripped | sometimes |

## Common mistakes

- **Quizzing the whole repo.** Only quiz what *this turn* changed.
- **Trivia distractors.** Make the wrong answers tempting, not silly.
- **The longest option is the answer.** Length-balance every question.
- **All one axis.** Sample across the five; don't skip how-was-it-verified.
- **Drifting too high.** "What does the app do" tests what they didn't lose.
- **Standard that references code, or deep that doesn't point to it.**
- **Switching to prose in a gated turn.** Keep it `AskUserQuestion` MC.
- **Skipping too eagerly.** Only *genuinely cosmetic* changes skip.
- **Correct answer always first.** Vary its slot every question.
