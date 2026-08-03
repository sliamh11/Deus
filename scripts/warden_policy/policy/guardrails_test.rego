package deus.wardens

import future.keywords.if

# --- fixtures -----------------------------------------------------------
# NOTE: every test overrides the scoped `data.warden_attestations` path, never the whole `data`
# root -- overriding the whole root with a value derived from a package-level rule (base_data)
# creates a circular dependency in OPA's static analysis (base_data lives under
# data.deus.wardens, which the override would also be replacing).

repo_a := "git-common-dir-sha256:aaaa000000000000000000000000000000000000000000000000000000000"
repo_unenrolled := "git-common-dir-sha256:bbbb000000000000000000000000000000000000000000000000000000000"
subject_reviewed := "git-tree:sha1:1111111111111111111111111111111111111111"
subject_new := "git-tree:sha1:2222222222222222222222222222222222222222"
subject_revised := "git-tree:sha1:3333333333333333333333333333333333333333"

base_attestations := {
	"schema_version": 1,
	"generation": 5,
	"config": {"enforced_repos": {repo_a: {"enabled": true, "enrolled_at": "2026-08-03T00:00:00Z"}}},
	"records": {
		"att-1": {
			"id": "att-1", "schema_version": 1, "repo_id": repo_a, "gate": "code-review",
			"subject": {"kind": "git-tree", "key": subject_reviewed, "digest": {"algorithm": "sha1", "value": "1111111111111111111111111111111111111111"}},
			"verdict": "SHIP", "issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-03T00:00:00Z", "reason": "ok",
		},
		"att-2": {
			"id": "att-2", "schema_version": 1, "repo_id": repo_a, "gate": "code-review",
			"subject": {"kind": "git-tree", "key": subject_revised, "digest": {"algorithm": "sha1", "value": "3333333333333333333333333333333333333333"}},
			"verdict": "SHIP", "issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-02T00:00:00Z", "reason": "an earlier SHIP that was later revised",
		},
		"att-3": {
			"id": "att-3", "schema_version": 1, "repo_id": repo_a, "gate": "code-review",
			"subject": {"kind": "git-tree", "key": subject_revised, "digest": {"algorithm": "sha1", "value": "3333333333333333333333333333333333333333"}},
			"verdict": "REVISE", "issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-03T00:00:00Z", "reason": "found a bug after all",
		},
		"att-4-wrong-gate": {
			"id": "att-4-wrong-gate", "schema_version": 1, "repo_id": repo_a, "gate": "some-other-gate",
			"subject": {"kind": "git-tree", "key": "git-tree:sha1:4444444444444444444444444444444444444444", "digest": {"algorithm": "sha1", "value": "4444444444444444444444444444444444444444"}},
			"verdict": "SHIP", "issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-03T00:00:00Z", "reason": "SHIP for a different gate, misfiled under code-review in latest below",
		},
	},
	"latest": {
		repo_a: {
			"code-review": {
				subject_reviewed: "att-1",
				# att-3 (REVISE) supersedes att-2 (SHIP) for the same subject -- latest must point
				# at the REVISE, and the older SHIP for the same subject must never be reachable.
				subject_revised: "att-3",
				# defense-in-depth fixture: latest["code-review"] points at a record whose OWN
				# gate field says something else -- must still deny (finding from adversarial review).
				"git-tree:sha1:4444444444444444444444444444444444444444": "att-4-wrong-gate",
			},
		},
	},
}

base_input(subject_key) := {
	"contract_version": 1,
	"enforcement_point": "hermes.pre_tool_call",
	"operation": "git.commit",
	"repo_id": repo_a,
	"subject_key": subject_key,
	"expected_generation": 5,
}

# --- allow cases ---------------------------------------------------------

test_allow_matching_ship if {
	decision.allow with input as base_input(subject_reviewed) with data.warden_attestations as base_attestations
}

test_allow_unenrolled_repo_ignores_missing_attestation if {
	inp := object.union(base_input(subject_new), {"repo_id": repo_unenrolled})
	decision.allow with input as inp with data.warden_attestations as base_attestations
}

# --- deny cases -----------------------------------------------------------

test_deny_no_attestation if {
	not decision.allow with input as base_input(subject_new) with data.warden_attestations as base_attestations
	decision.reason == sprintf("no code-review SHIP for staged tree %s", [subject_new]) with input as base_input(subject_new) with data.warden_attestations as base_attestations
}

test_deny_latest_is_revise_even_though_an_older_ship_exists if {
	not decision.allow with input as base_input(subject_revised) with data.warden_attestations as base_attestations
}

test_deny_record_gate_mismatch_defense_in_depth if {
	inp := base_input("git-tree:sha1:4444444444444444444444444444444444444444")
	not decision.allow with input as inp with data.warden_attestations as base_attestations
}

test_deny_unknown_contract_version if {
	inp := object.union(base_input(subject_reviewed), {"contract_version": 99})
	not decision.allow with input as inp with data.warden_attestations as base_attestations
}

test_deny_unknown_root_schema_version if {
	att := object.union(base_attestations, {"schema_version": 2})
	not decision.allow with input as base_input(subject_reviewed) with data.warden_attestations as att
}

test_deny_stale_opa_generation_mismatch if {
	# input.expected_generation (the adapter's locked local read) doesn't match data's generation
	# (OPA's loaded snapshot) -- simulates OPA serving stale data after a failed/ambiguous PUT.
	inp := object.union(base_input(subject_reviewed), {"expected_generation": 4})
	not decision.allow with input as inp with data.warden_attestations as base_attestations
}

test_deny_malformed_store_missing_records_key if {
	att := {
		"schema_version": 1, "generation": 5,
		"config": {"enforced_repos": {repo_a: {"enabled": true, "enrolled_at": "2026-08-03T00:00:00Z"}}},
		"records": {},
		"latest": {},
	}
	not decision.allow with input as base_input(subject_reviewed) with data.warden_attestations as att
}

test_deny_non_commit_operation_falls_to_default if {
	inp := object.union(base_input(subject_reviewed), {"operation": "something.else"})
	not decision.allow with input as inp with data.warden_attestations as base_attestations
}
