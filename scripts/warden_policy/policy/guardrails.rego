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
	enrolled
	valid_ship
}

decision := {
	"allow": false,
	"reason": sprintf("no code-review SHIP for staged tree %s", [input.subject_key]),
} if {
	supported
	input.operation == "git.commit"
	enrolled
	not valid_ship
}
