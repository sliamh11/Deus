package deus.wardens

import future.keywords.if
import future.keywords.in

# Guardrails policy v1 -- see docs/decisions/opa-warden-attestations-v1.md for the full design
# rationale. This is the ONLY place gate/allow decisions are made; command classification (which
# commit forms are supported at all) lives entirely in scripts/warden_policy/command_parser.py --
# this policy never parses shell commands, it only evaluates an already-normalized input.

default decision := {"allow": false, "reason": "guardrails policy produced no valid decision"}

# `supported` gates every non-default decision below. Found missing by adversarial plan-review:
# an unrecognized root schema_version, an unrecognized input contract_version, or OPA serving a
# stale ledger snapshot (generation mismatch, e.g. after a failed/ambiguous PUT even though OPA
# itself is reachable) must all fall through to a deny, not be silently accepted by a rule that
# only checked the individual attestation record's own schema_version. For `git.commit`/
# `file.write` operations specifically, a generation mismatch gets its own dedicated message
# (see the decision rule right below `supported`, LIA-535) rather than the fully generic default
# at the top of this file -- every other `not supported` cause, and every other/unrecognized
# operation, still falls through to that generic default unchanged.
supported if {
	input.contract_version == 1
	data.warden_attestations.schema_version == 1
	data.warden_attestations.generation == input.expected_generation
}

# Distinguishes ledger staleness (self-healing infra desync, LIA-533) from every other
# `supported`-gated denial and from the fully generic default above (LIA-535). Scoped to exactly
# the two operations whose decision bodies all require `supported` (git.commit, file.write) --
# NOT `input.operation != "attestation.verify"`, which would also swallow any unrecognized/future
# operation value and misreport it as ledger staleness (caught by plan-review round 1). Excludes
# attestation.verify specifically because that operation already has its own dedicated composite
# deny message (below) covering "no SHIP found ... or OPA snapshot stale/unsupported" for BOTH
# ledgers -- letting this rule also match attestation.verify inputs would create a genuine
# multi-value `decision` conflict (OPA eval_conflict_error) whenever both conditions hold
# simultaneously, which they can.
decision := {
	"allow": false,
	"reason": sprintf(
		"OPA ledger generation stale (expected %d, got %d) -- run `python3 scripts/warden_attest.py sync` or wait for the next self-heal tick (LIA-533)",
		[input.expected_generation, data.warden_attestations.generation],
	),
} if {
	input.operation in {"git.commit", "file.write"}
	input.contract_version == 1
	data.warden_attestations.schema_version == 1
	data.warden_attestations.generation != input.expected_generation
}

enrolled if data.warden_attestations.config.enforced_repos[input.repo_id].enabled

# `latest["code-review"]` is a pointer, not a trusted authority on its own -- every field the
# decision relies on is re-checked against the record itself (repo_id, gate, subject key,
# verdict), not assumed correct just because the index found it under a particular bucket.
valid_ship if {
	id := data.warden_attestations.latest[input.repo_id]["code-review"][input.subject_key]
	att := data.warden_attestations.records[id]
	att.schema_version == 1
	att.repo_id == input.repo_id
	att.gate == "code-review"
	att.subject.key == input.subject_key
	att.verdict == "SHIP"
}

decision := {"allow": true, "reason": "repo not enrolled"} if {
	supported
	input.operation == "git.commit"
	input.gate == "code-review"
	not enrolled
}

decision := {"allow": true, "reason": "matching code-review SHIP"} if {
	supported
	input.operation == "git.commit"
	input.gate == "code-review"
	enrolled
	valid_ship
}

decision := {
	"allow": false,
	"reason": sprintf("no code-review SHIP for staged tree %s", [input.subject_key]),
} if {
	supported
	input.operation == "git.commit"
	input.gate == "code-review"
	enrolled
	not valid_ship
}

# --- Multi-backend facts for the migrated gates (code-reviewer, ai-eng-warden). Read
# `latest_by_backend`, a key entirely distinct from `latest` above -- `latest`'s scalar-leaf
# shape and the three "code-review" decision bodies above are untouched by anything below.
# These are FACTS, never a `decision` -- the strict-AND across required backends stays in the
# Python shim (mirrors `_evaluate_backends`'s existing per-backend-loop shape), not here. A
# raw verdict string (not a boolean) is exposed per backend so the shim can distinguish
# COULD_NOT_RUN (fail-open) from any other non-SHIP verdict (block) -- reproducing
# `_evaluate_backends`'s real logic, which a boolean collapse could not. Gated by `supported`
# -- the same staleness/generation guard `decision` already relies on -- so a stale OPA
# snapshot can't leak a verdict through this path either. `att.subject.key == input.subject_key`
# mirrors `valid_ship`'s own re-check above: `latest_by_backend`'s index is a pointer, not a
# trusted authority on its own -- every field the decision relies on is re-checked against the
# record itself, the same principle this file's header comment states for `valid_ship`.

backend_verdict(backend) := att.verdict if {
	id := data.warden_attestations.latest_by_backend[input.repo_id][input.gate][input.subject_key][backend]
	att := data.warden_attestations.records[id]
	att.schema_version == 1
	att.repo_id == input.repo_id
	att.gate == input.gate
	att.subject.key == input.subject_key
	att.backend == backend
	supported
}

backend_verdict_map[backend] := backend_verdict(backend) if {
	some backend in input.required_backends
}

# --- ai-eng-warden gate (LIA-524) -- purely additive, mirrors the code-review triple's shape
# but reads `latest_by_backend` (role-keyed, per the ADR's "Gate-key vocabulary" table) instead
# of `latest` (gate-keyed), with `input.backend` fixed to "hermes" by the Hermes-side shim.
# Reuses `backend_verdict(backend)` (above) rather than re-deriving the lookup + record
# re-validation -- smaller Rego surface, same defense-in-depth guarantees.

ai_eng_warden_enrolled if data.warden_attestations.config.enforced_repos[input.repo_id].ai_eng_warden_enabled

valid_ai_eng_warden_ship if backend_verdict(input.backend) == "SHIP"

decision := {"allow": true, "reason": "repo not ai-eng-warden-enrolled"} if {
	supported
	input.operation == "git.commit"
	input.gate == "ai-eng-warden"
	not ai_eng_warden_enrolled
}

decision := {"allow": true, "reason": "matching ai-eng-warden SHIP"} if {
	supported
	input.operation == "git.commit"
	input.gate == "ai-eng-warden"
	ai_eng_warden_enrolled
	valid_ai_eng_warden_ship
}

decision := {
	"allow": false,
	"reason": sprintf("no ai-eng-warden SHIP for staged tree %s", [input.subject_key]),
} if {
	supported
	input.operation == "git.commit"
	input.gate == "ai-eng-warden"
	ai_eng_warden_enrolled
	not valid_ai_eng_warden_ship
}

# --- verification-gate (LIA-524) -- purely additive, structurally identical to the code-review
# triple above (git-tree subject, `latest` index, no backend -- CC's own `run_verification_gate`
# is single-marker, not multi-backend, so mirroring that shape exactly is the faithful port).

verification_gate_enrolled if data.warden_attestations.config.enforced_repos[input.repo_id].verification_gate_enabled

valid_verification_ship if {
	id := data.warden_attestations.latest[input.repo_id]["verification-gate"][input.subject_key]
	att := data.warden_attestations.records[id]
	att.schema_version == 1
	att.repo_id == input.repo_id
	att.gate == "verification-gate"
	att.subject.key == input.subject_key
	att.verdict == "SHIP"
}

decision := {"allow": true, "reason": "repo not verification-gate-enrolled"} if {
	supported
	input.operation == "git.commit"
	input.gate == "verification-gate"
	not verification_gate_enrolled
}

decision := {"allow": true, "reason": "matching verification-gate SHIP"} if {
	supported
	input.operation == "git.commit"
	input.gate == "verification-gate"
	verification_gate_enrolled
	valid_verification_ship
}

decision := {
	"allow": false,
	"reason": sprintf("no verification-gate SHIP for staged tree %s", [input.subject_key]),
} if {
	supported
	input.operation == "git.commit"
	input.gate == "verification-gate"
	verification_gate_enrolled
	not valid_verification_ship
}

# --- Plan-review gate (LIA-523) -- purely additive. `enrolled`, `valid_ship`, and the three
# "git.commit" decision bodies above are byte-unchanged; this section never reads or writes
# `enforced_repos[repo_id].enabled`. Session-bound (not git-tree-bound): a plan-reviewer SHIP
# approves a session's reviewed intent, not a specific diff snapshot -- the same reasoning
# Claude Code's own plan-review gate uses (LIA-516 deliberately disabled diff-hash staleness for
# this role). `plan_review_enabled` is a second, independent enrollment switch alongside
# `enabled` -- neither implies the other.

plan_review_enrolled if data.warden_attestations.config.enforced_repos[input.repo_id].plan_review_enabled

default plan_review_ttl_seconds := 7200

plan_review_ttl_seconds := t if {
	t := data.warden_attestations.config.plan_review_ttl_seconds
}

# Session-scoped attestations have no diff to bind to, so freshness is time-based instead:
# expired (issued_at older than the TTL) is treated the same as "no attestation" -- blocks with
# a re-attestation message, never falls through to an implicit allow.
valid_plan_review_ship if {
	id := data.warden_attestations.latest[input.repo_id]["plan-review"][input.session_id]
	att := data.warden_attestations.records[id]
	att.schema_version == 1
	att.repo_id == input.repo_id
	att.gate == "plan-review"
	att.subject.kind == "session"
	att.subject.session_id == input.session_id
	att.verdict == "SHIP"
	issued_ns := time.parse_rfc3339_ns(att.issued_at)
	time.now_ns() - issued_ns < (plan_review_ttl_seconds * 1000000000)
}

decision := {"allow": true, "reason": "repo not plan-review-enrolled"} if {
	supported
	input.operation == "file.write"
	not plan_review_enrolled
}

decision := {"allow": true, "reason": "matching plan-review SHIP"} if {
	supported
	input.operation == "file.write"
	input.gate == "plan-review"
	plan_review_enrolled
	valid_plan_review_ship
}

decision := {
	"allow": false,
	"reason": sprintf("no valid (non-expired) plan-review SHIP for session %s", [input.session_id]),
} if {
	supported
	input.operation == "file.write"
	input.gate == "plan-review"
	plan_review_enrolled
	not valid_plan_review_ship
}

# --- attestation-verify cutover (LIA-530) -- once main-attestation-backstop is active, this IS
# the sole non-bypassable gate on main (bypass_actors: [], git-level-hard-backstop-design.md
# ~:95-112) -- confirmed decision, not advisory. An ALLOW from the CC-mirror path means "a
# claude-backend code-reviewer SHIP was found AND Hermes has no opinion for this tree" -- an
# explicit Hermes REVISE/BLOCK always wins over a CC mirror (never overridden). Does NOT verify
# gpt/glm co-gate backends -- permanent, by-design limitation, disclosed here and in the allow
# reason string. No signing, no runner isolation -- same-host trust, same accepted-risk framing
# as git-level-hard-backstop-design.md §3.3. A DENY is authoritative-to-block; an ALLOW means
# "evidence found," never "fully reviewed." **DOES NOT ACTIVATE UNTIL LIA-539 (the credential-
# separation implementation) lands -- LIA-531 merged as DESIGN ONLY and does not clear this gate
# on its own; confirmed live via LIA-539's own Linear description, not via
# git-level-hard-backstop-design.md §5, which is stale on this specific point as of this writing
# (still cites LIA-531 alone -- needs its own follow-up, out of scope here).**

cc_supported if {
	input.contract_version == 1
	data.warden_cc_attestations.schema_version == 1
	data.warden_cc_attestations.generation == input.expected_cc_generation
}

valid_cc_mirrored_ship if {
	id := data.warden_cc_attestations.latest_by_backend[input.repo_id]["code-reviewer"][input.subject_key]["claude"]
	att := data.warden_cc_attestations.records[id]
	att.schema_version == 1
	att.repo_id == input.repo_id
	att.gate == "code-reviewer"
	att.subject.key == input.subject_key
	att.backend == "claude"
	att.verdict == "SHIP"
	att.queued_at   # CC-only field (issue_if_newer sets it; Hermes's issue() never does) --
	                # the real mis-targeted-document discriminator. Existence check: Rego's `if`
	                # fails on undefined; the schema types this an integer >= 0, so no
	                # legitimate value (including 0) is falsy here.
	cc_supported
}

hermes_path_ok if {
	supported
	valid_ship
}

# Existence-only check, deliberately not re-using valid_ship's SHIP-specific re-checks -- ANY
# fresh Hermes record for this subject (SHIP, REVISE, BLOCK, or COULD_NOT_RUN) means Hermes has
# an opinion, and that opinion is authoritative; the CC path never second-guesses it.
hermes_record_exists if {
	id := data.warden_attestations.latest[input.repo_id]["code-review"][input.subject_key]
	data.warden_attestations.records[id]
}

cc_path_ok if {
	supported
	not hermes_record_exists
	valid_cc_mirrored_ship
}

decision := {"allow": true, "reason": "matching code-review SHIP (Hermes-native)"} if {
	input.operation == "attestation.verify"
	input.gate == "code-review"
	hermes_path_ok
}

decision := {"allow": true, "reason": "matching code-reviewer SHIP (Claude Code native, claude backend only -- gpt/glm not verified, permanent limitation)"} if {
	input.operation == "attestation.verify"
	input.gate == "code-review"
	not hermes_path_ok   # deliberate defense-in-depth, provably redundant with cc_path_ok's own
	                      # `not hermes_record_exists` given valid_ship and hermes_record_exists
	                      # resolve the identical index lookup -- kept for clarity at zero cost,
	                      # never remove `not hermes_record_exists` from cc_path_ok on the mistaken
	                      # belief that THIS line alone still protects against Hermes-record
	                      # override (mutation-verified, opa-warden-attestations-v1.md Phase 4).
	cc_path_ok
}

decision := {
	"allow": false,
	"reason": sprintf("no SHIP found for %s (Hermes-native or Claude-Code-mirrored; or an explicit non-SHIP Hermes verdict exists; or OPA snapshot stale/unsupported)", [input.subject_key]),
} if {
	input.operation == "attestation.verify"
	input.gate == "code-review"
	not hermes_path_ok
	not cc_path_ok
}
