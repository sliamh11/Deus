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
	"gate": "code-review",
}

# --- fixtures for backend_verdict/backend_verdict_map (migrated-gate facts) --------------

subject_migrated := "git-tree:sha1:5555555555555555555555555555555555555555"

migrated_input(gate, required_backends) := {
	"contract_version": 1,
	"enforcement_point": "claude_code.pre_tool_use",
	"operation": "git.commit",
	"repo_id": repo_a,
	"subject_key": subject_migrated,
	"expected_generation": 5,
	"gate": gate,
	"required_backends": required_backends,
}

attestations_with_backend := object.union(base_attestations, {
	"records": object.union(base_attestations.records, {
		"att-claude-ship": {
			"id": "att-claude-ship", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_migrated, "digest": {"algorithm": "sha1", "value": "5555555555555555555555555555555555555555"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-04T00:00:00Z", "reason": "ok",
		},
		"att-gpt-could-not-run": {
			"id": "att-gpt-could-not-run", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_migrated, "digest": {"algorithm": "sha1", "value": "5555555555555555555555555555555555555555"}},
			"verdict": "COULD_NOT_RUN", "backend": "gpt",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@gpt"},
			"issued_at": "2026-08-04T00:00:00Z", "reason": "infra failure",
		},
		"att-wrong-subject": {
			"id": "att-wrong-subject", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": "git-tree:sha1:6666666666666666666666666666666666666666", "digest": {"algorithm": "sha1", "value": "6666666666666666666666666666666666666666"}},
			"verdict": "SHIP", "backend": "glm",
			"issuer": {"kind": "manual", "reviewer_id": "code-reviewer@glm"},
			"issued_at": "2026-08-04T00:00:00Z", "reason": "SHIP for a DIFFERENT subject, misfiled under subject_migrated below",
		},
	}),
	"latest_by_backend": {
		repo_a: {
			"code-reviewer": {
				subject_migrated: {
					"claude": "att-claude-ship",
					"gpt": "att-gpt-could-not-run",
					# defense-in-depth fixture: latest_by_backend points at a record whose OWN
					# subject.key differs -- must still yield an absent/undefined backend_verdict.
					"glm": "att-wrong-subject",
				},
			},
		},
	},
})

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

test_deny_code_review_query_with_gate_omitted if {
	# Simulates the pre-Hermes-fix input shape (no "gate" field at all) -- real Hermes
	# traffic always sends "gate": "code-review" post-fix, but this documents/regression-
	# tests that an omitted gate falls to the default deny, not a crash or a leaked allow.
	inp := {k: v | some k, v in base_input(subject_reviewed); k != "gate"}
	not decision.allow with input as inp with data.warden_attestations as base_attestations
}

# --- backend_verdict / backend_verdict_map (migrated-gate facts) ----------------------

test_backend_verdict_map_reports_per_backend_raw_verdicts if {
	inp := migrated_input("code-reviewer", ["claude", "gpt"])
	m := backend_verdict_map with input as inp with data.warden_attestations as attestations_with_backend
	m.claude == "SHIP"
	m.gpt == "COULD_NOT_RUN"
}

test_backend_verdict_absent_when_no_attestation_exists if {
	inp := migrated_input("code-reviewer", ["claude", "glm2"])
	m := backend_verdict_map with input as inp with data.warden_attestations as attestations_with_backend
	not m.glm2
}

test_backend_verdict_defense_in_depth_subject_mismatch if {
	# att-wrong-subject is indexed under subject_migrated in latest_by_backend, but its OWN
	# subject.key field points at a DIFFERENT tree -- the record-level re-check must reject
	# it, the same defense-in-depth principle test_deny_record_gate_mismatch_defense_in_depth
	# already enforces for `decision`/`valid_ship`, applied here to the new fact instead.
	inp := migrated_input("code-reviewer", ["glm"])
	m := backend_verdict_map with input as inp with data.warden_attestations as attestations_with_backend
	not m.glm
}

test_backend_verdict_map_allows_unenrolled_repo_regardless_of_gate if {
	inp := object.union(migrated_input("code-reviewer", ["claude"]), {"repo_id": repo_unenrolled})
	decision.allow with input as inp with data.warden_attestations as attestations_with_backend
}

test_backend_verdict_empty_when_opa_generation_stale if {
	inp := object.union(migrated_input("code-reviewer", ["claude"]), {"expected_generation": 999})
	m := backend_verdict_map with input as inp with data.warden_attestations as attestations_with_backend
	not m.claude
}

test_decision_never_fires_for_migrated_gate_when_enrolled if {
	# Confirms the mental-exclusivity trace: for a migrated gate + enrolled repo, none of
	# the three "code-review" decision bodies fire -- the shim queries backend_verdict_map
	# instead, never `decision`, for this case. `decision` here falls to the file's own
	# default (deny), which is expected and not something the shim relies on for this path.
	inp := migrated_input("code-reviewer", ["claude"])
	not decision.allow with input as inp with data.warden_attestations as attestations_with_backend
	decision.reason == "guardrails policy produced no valid decision" with input as inp with data.warden_attestations as attestations_with_backend
}

# --- Plan-review gate (LIA-523) ------------------------------------------------------

session_reviewed := "sess-reviewed-abc"
session_expired := "sess-expired-def"
session_unknown := "sess-never-attested"

now_fixed_ns := time.parse_rfc3339_ns("2026-08-08T12:00:00Z")

plan_review_input(session_id) := {
	"contract_version": 1,
	"enforcement_point": "hermes.pre_tool_call",
	"operation": "file.write",
	"repo_id": repo_a,
	"session_id": session_id,
	"expected_generation": 5,
	"gate": "plan-review",
}

# repo_a with code-review UNTOUCHED (still enabled: true from base_attestations) plus the new,
# additive plan_review_enabled switch and two session-bound records -- proves the two
# enrollment surfaces coexist without disturbing each other.
attestations_with_plan_review := object.union(base_attestations, {
	"config": {"enforced_repos": {repo_a: object.union(base_attestations.config.enforced_repos[repo_a], {"plan_review_enabled": true})}},
	"records": object.union(base_attestations.records, {
		"att-plan-review-fresh": {
			"id": "att-plan-review-fresh", "schema_version": 1, "repo_id": repo_a, "gate": "plan-review",
			"subject": {"kind": "session", "session_id": session_reviewed},
			"verdict": "SHIP", "issuer": {"kind": "manual", "reviewer_id": "plan-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-08T11:00:00Z", "reason": "12 rounds, all real findings resolved",
		},
		"att-plan-review-expired": {
			"id": "att-plan-review-expired", "schema_version": 1, "repo_id": repo_a, "gate": "plan-review",
			"subject": {"kind": "session", "session_id": session_expired},
			"verdict": "SHIP", "issuer": {"kind": "manual", "reviewer_id": "plan-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-08T09:00:00Z", "reason": "issued 3 hours before now_fixed_ns -- past the default 2h TTL",
		},
	}),
	"latest": object.union(base_attestations.latest, {
		repo_a: object.union(base_attestations.latest[repo_a], {
			"plan-review": {
				session_reviewed: "att-plan-review-fresh",
				session_expired: "att-plan-review-expired",
			},
		}),
	}),
})

test_plan_review_allow_fresh_ship if {
	inp := plan_review_input(session_reviewed)
	decision.allow with input as inp with data.warden_attestations as attestations_with_plan_review with time.now_ns as now_fixed_ns
}

test_plan_review_deny_expired_ttl if {
	# Past the default 7200s TTL -- must NOT silently fall through to allow.
	inp := plan_review_input(session_expired)
	not decision.allow with input as inp with data.warden_attestations as attestations_with_plan_review with time.now_ns as now_fixed_ns
}

test_plan_review_deny_no_attestation_for_session if {
	inp := plan_review_input(session_unknown)
	not decision.allow with input as inp with data.warden_attestations as attestations_with_plan_review with time.now_ns as now_fixed_ns
	decision.reason == sprintf("no valid (non-expired) plan-review SHIP for session %s", [session_unknown]) with input as inp with data.warden_attestations as attestations_with_plan_review with time.now_ns as now_fixed_ns
}

test_plan_review_allow_unenrolled_repo_regardless_of_session if {
	# repo_a's code-review `enabled: true` must NOT imply plan-review enrollment -- the two
	# switches are independent. Use base_attestations (no plan_review_enabled at all).
	inp := plan_review_input(session_unknown)
	decision.allow with input as inp with data.warden_attestations as base_attestations with time.now_ns as now_fixed_ns
}

test_plan_review_custom_ttl_from_config if {
	# A configured plan_review_ttl_seconds of 1 hour makes the (3-hours-old) "expired" fixture
	# ALSO exceed a custom, SHORTER-than-default TTL -- proves the config path is actually
	# read, not just the hardcoded default.
	short_ttl := object.union(attestations_with_plan_review, {
		"config": object.union(attestations_with_plan_review.config, {"plan_review_ttl_seconds": 3600}),
	})
	inp := plan_review_input(session_expired)
	not decision.allow with input as inp with data.warden_attestations as short_ttl with time.now_ns as now_fixed_ns
}

test_plan_review_custom_ttl_extends_validity if {
	# A configured plan_review_ttl_seconds LONGER than the default makes the same 3-hour-old
	# fixture VALID -- proves the custom TTL genuinely overrides the default in both
	# directions, not just narrowing it.
	long_ttl := object.union(attestations_with_plan_review, {
		"config": object.union(attestations_with_plan_review.config, {"plan_review_ttl_seconds": 14400}),
	})
	inp := plan_review_input(session_expired)
	decision.allow with input as inp with data.warden_attestations as long_ttl with time.now_ns as now_fixed_ns
}

test_plan_review_defense_in_depth_gate_mismatch if {
	# A record indexed under "plan-review" in `latest` whose OWN gate field says something
	# else must still deny -- the same defense-in-depth principle valid_ship already enforces.
	tampered := object.union(attestations_with_plan_review, {
		"records": object.union(attestations_with_plan_review.records, {
			"att-plan-review-fresh": object.union(attestations_with_plan_review.records["att-plan-review-fresh"], {"gate": "code-review"}),
		}),
	})
	inp := plan_review_input(session_reviewed)
	not decision.allow with input as inp with data.warden_attestations as tampered with time.now_ns as now_fixed_ns
}

test_plan_review_git_commit_decision_bodies_never_fire_for_file_write if {
	# Mental-exclusivity check mirroring test_decision_never_fires_for_migrated_gate_when_enrolled:
	# a "file.write" operation must never satisfy any of the "git.commit" decision bodies.
	inp := plan_review_input(session_reviewed)
	d := decision with input as inp with data.warden_attestations as attestations_with_plan_review with time.now_ns as now_fixed_ns
	d.reason != "matching code-review SHIP"
	d.reason != "repo not enrolled"
}
