# Measuring things that vary

Loaded on demand from `SKILL.md` §7, for two cases the single-run pattern cannot
settle: performance claims, and any surface whose output differs between
identical runs.

## Performance

Declare four things **before** collecting anything. Choosing them afterwards is
how a measurement becomes a justification.

1. **The metric** — wall clock to first byte, wall clock to exit, peak RSS,
   allocations. Name the boundary precisely: "startup" is not a metric until you
   say what event ends it.
2. **The statistic** — median, p95, mean. Not "it looked faster." Tail claims
   need tail statistics; a median improvement can hide a worse p99, and for
   anything user-facing the tail is usually what was actually complained about.
3. **The threshold** — the number from the claim, and what counts as clearing it.
4. **How many trials, decided in advance.** Otherwise you stop when the numbers
   look right, which guarantees they eventually will.

Then collect:

- **Separate cold from warm.** They are different claims. A cold-start number
  needs the caches actually cold each trial — which usually means resetting
  something between them, not just running the command again.
- **Alternate or randomise the order.** Control-then-treatment ten times in a row
  lets machine drift — thermal, background load, page cache filling — masquerade
  as your effect.
- **Keep every sample**, or a full summary with a spread. A pair of single
  numbers cannot distinguish a real change from ordinary variance.
- **Say what else was running.** A build in another window is a confound.

**The decision rule:** if the difference does not clearly exceed the spread of
the samples, it has not been demonstrated. Overlapping distributions are
INCONCLUSIVE with the trial count named, never a PASS narrated as "slightly
faster." Two samples per side is not a measurement; it is two anecdotes.

## Non-deterministic behaviour

Agents, concurrency, network calls, retry and timeout logic, anything seeded by
time or randomness: one run per side proves nothing, because one lucky control
failure beside one lucky treatment success satisfies every other rule in this
skill while being pure noise.

Fix in advance:

- **The inputs** — the exact prompt, request sequence, or seed set. Reused
  verbatim on both sides.
- **The scoring rule** — what makes a single trial a success. Written down before
  you see any output, so it cannot bend.
- **The trial count**, matched across control and treatment.

Then report **rates, not verdicts on individual runs**: "control 2/20, treatment
19/20" is evidence; "it worked when I tried it" is not. Hold the model, the
runtime, and the configuration fixed, and say which ones you could not hold.

**A claim stated deterministically that only holds sometimes is FAIL,** not a
qualified pass. "Always returns 429 after the eleventh request" is refuted by a
twelfth request that succeeds once in twenty. If the true claim is statistical,
the claim in §1 should have said so — and rewriting it after seeing the output is
exactly what the pre-declared refutation condition exists to prevent.

## Both at once

Performance measured on a stochastic surface needs both disciplines: pre-declared
statistic *and* pre-declared trial count, with the order alternated. This is
where an under-powered comparison is most likely to look convincing, because
there are two independent sources of variance and only one of them is visible in
the numbers you chose to print.
