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
# itself is reachable) must all fall through to the default deny, not be silently accepted by a
# rule that only checked the individual attestation record's own schema_version.
supported if {
	input.contract_version == 1
	data.warden_attestations.schema_version == 1
	data.warden_attestations.generation == input.expected_generation
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
