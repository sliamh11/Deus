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

# --- attestation-verify cutover (LIA-530) --------------------------------------------

subject_cc_mirrored := "git-tree:sha1:7777777777777777777777777777777777777777"
subject_cc_mistargeted := "git-tree:sha1:8888888888888888888888888888888888888888"
subject_cc_mistargeted_control := "git-tree:sha1:8888888888888888888888888888888888888801"
subject_cc_session_probe := "sess-cc-probe-treated-as-subject-key"
subject_cc_revise := "git-tree:sha1:cccc000000000000000000000000000000000000"
subject_cc_backend_mismatch := "git-tree:sha1:dddd000000000000000000000000000000000000"
subject_cc_gate_mismatch := "git-tree:sha1:eeee000000000000000000000000000000000000"
subject_cc_record_schema_mismatch := "git-tree:sha1:ffff000000000000000000000000000000000000"
subject_cc_record_repo_mismatch := "git-tree:sha1:1010000000000000000000000000000000000000"

base_cc_attestations := {
	"schema_version": 1,
	"generation": 3,
	"config": {"enforced_repos": {}},
	"latest": {},
	"records": {
		"att-cc-ship": {
			"id": "att-cc-ship", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_mirrored, "digest": {"algorithm": "sha1", "value": "7777777777777777777777777777777777777777"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "ok", "queued_at": 1754600000000000000,
		},
		"att-cc-mistargeted": {
			# Hermes-shaped record (NO queued_at) planted under the CC document's own index --
			# the round-3/4 exploit regression fixture (Fix C-revised). One of two deliberately
			# schema-invalid records in this fixture set (the other is
			# att-cc-record-schema-mismatch below, schema_version: 2) -- both exist specifically
			# to prove the record-level defense-in-depth checks reject a malformed/mis-targeted
			# record even if one somehow reached the ledger.
			"id": "att-cc-mistargeted", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_mistargeted, "digest": {"algorithm": "sha1", "value": "8888888888888888888888888888888888888888"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "manual", "reviewer_id": "code-reviewer@claude-sonnet-5"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "Hermes-shaped record erroneously indexed in the CC document -- must be rejected",
		},
		"att-cc-mistargeted-control": {
			# Positive control: byte-identical subject/gate/backend/verdict to att-cc-mistargeted,
			# but WITH queued_at -- proves the deny above is specifically about the missing field.
			"id": "att-cc-mistargeted-control", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_mistargeted_control, "digest": {"algorithm": "sha1", "value": "8888888888888888888888888888888888888801"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "control: same shape, has queued_at", "queued_at": 1754600000000000000,
		},
		"att-cc-session-kind": {
			# Session-kind CC record -- att.subject.key is undefined by construction. Indexed
			# below under a subject_key-looking pointer to simulate an attacker (or a bug) trying
			# to make a session-scoped record satisfy a tree-bound query.
			"id": "att-cc-session-kind", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "session", "session_id": subject_cc_session_probe},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "session-kind record, must not satisfy a tree-bound check", "queued_at": 1754600000000000000,
		},
		"att-cc-override": {
			# A valid CC-mirrored claude SHIP for subject_revised -- the SAME subject
			# base_attestations already has a FRESH Hermes REVISE for (att-3). Fix I's regression
			# fixture: this CC SHIP must NEVER override that fresh Hermes REVISE.
			"id": "att-cc-override", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_revised, "digest": {"algorithm": "sha1", "value": "3333333333333333333333333333333333333333"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "CC SHIP for a tree Hermes already REVISEd -- must not override", "queued_at": 1754600000000000000,
		},
		"att-cc-revise": {
			# Defense-in-depth: verdict != SHIP. Deleting `att.verdict == "SHIP"` from
			# valid_cc_mirrored_ship must fail this test.
			"id": "att-cc-revise", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_revise, "digest": {"algorithm": "sha1", "value": "cccc000000000000000000000000000000000000"}},
			"verdict": "REVISE", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "CC-mirrored REVISE, must never satisfy the SHIP-only check", "queued_at": 1754600000000000000,
		},
		"att-cc-backend-mismatch": {
			# Indexed under the "claude" key below, but the record's OWN backend field says
			# "gpt" -- defense-in-depth, same principle as the file's existing att-wrong-subject
			# fixture. Deleting `att.backend == "claude"` must fail this test.
			"id": "att-cc-backend-mismatch", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_backend_mismatch, "digest": {"algorithm": "sha1", "value": "dddd000000000000000000000000000000000000"}},
			"verdict": "SHIP", "backend": "gpt",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@gpt"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "SHIP for a DIFFERENT backend, misfiled under the claude key below", "queued_at": 1754600000000000000,
		},
		"att-cc-gate-mismatch": {
			# Indexed under the "code-reviewer" gate bucket below, but the record's OWN gate
			# field says "ai-eng-warden". Deleting `att.gate == "code-reviewer"` must fail this.
			"id": "att-cc-gate-mismatch", "schema_version": 1, "repo_id": repo_a, "gate": "ai-eng-warden",
			"subject": {"kind": "git-tree", "key": subject_cc_gate_mismatch, "digest": {"algorithm": "sha1", "value": "eeee000000000000000000000000000000000000"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "ai-eng-warden@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "SHIP for a DIFFERENT gate, misfiled under code-reviewer below", "queued_at": 1754600000000000000,
		},
		"att-cc-record-schema-mismatch": {
			# Record's OWN schema_version says 2 -- doc-level schema_version stays 1 (cc_supported
			# still true), isolating this from the doc-level schema_version test below. Deleting
			# `att.schema_version == 1` from valid_cc_mirrored_ship must fail this test.
			"id": "att-cc-record-schema-mismatch", "schema_version": 2, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_record_schema_mismatch, "digest": {"algorithm": "sha1", "value": "ffff000000000000000000000000000000000000"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "record-level schema_version mismatch, doc-level stays valid", "queued_at": 1754600000000000000,
		},
		"att-cc-record-repo-mismatch": {
			# Record's OWN repo_id points at a DIFFERENT repo than the one it's indexed under
			# below -- defense-in-depth, same principle as the Hermes side's att-4-wrong-gate.
			"id": "att-cc-record-repo-mismatch", "schema_version": 1, "repo_id": repo_unenrolled, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_cc_record_repo_mismatch, "digest": {"algorithm": "sha1", "value": "1010000000000000000000000000000000000000"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "SHIP for a DIFFERENT repo_id, misfiled under repo_a's bucket below", "queued_at": 1754600000000000000,
		},
		"att-cc-coexist-with-hermes": {
			# A fully valid CC-mirrored claude SHIP for subject_reviewed -- the SAME subject
			# base_attestations ALSO has a genuine, fresh Hermes-native SHIP (att-1) for. Unlike
			# att-cc-override (which pairs CC evidence with a Hermes REVISE), this pairs CC
			# evidence with a Hermes SHIP -- the real "both paths have valid evidence" case, which
			# must resolve via the Hermes-native reason specifically, never the CC-mirror one.
			"id": "att-cc-coexist-with-hermes", "schema_version": 1, "repo_id": repo_a, "gate": "code-reviewer",
			"subject": {"kind": "git-tree", "key": subject_reviewed, "digest": {"algorithm": "sha1", "value": "1111111111111111111111111111111111111111"}},
			"verdict": "SHIP", "backend": "claude",
			"issuer": {"kind": "script", "reviewer_id": "code-reviewer@claude"},
			"issued_at": "2026-08-08T00:00:00Z", "reason": "CC SHIP coexisting with a genuine Hermes-native SHIP for the same tree", "queued_at": 1754600000000000000,
		},
	},
	"latest_by_backend": {
		repo_a: {
			"code-reviewer": {
				subject_cc_mirrored: {"claude": "att-cc-ship"},
				subject_cc_mistargeted: {"claude": "att-cc-mistargeted"},
				subject_cc_mistargeted_control: {"claude": "att-cc-mistargeted-control"},
				subject_cc_session_probe: {"claude": "att-cc-session-kind"},
				subject_revised: {"claude": "att-cc-override"},
				subject_cc_revise: {"claude": "att-cc-revise"},
				subject_cc_backend_mismatch: {"claude": "att-cc-backend-mismatch"},
				subject_cc_gate_mismatch: {"claude": "att-cc-gate-mismatch"},
				subject_cc_record_schema_mismatch: {"claude": "att-cc-record-schema-mismatch"},
				subject_cc_record_repo_mismatch: {"claude": "att-cc-record-repo-mismatch"},
				subject_reviewed: {"claude": "att-cc-coexist-with-hermes"},
			},
		},
	},
}

attestation_verify_input(subject_key) := {
	"contract_version": 1,
	"enforcement_point": "github_actions.attestation_verify",
	"operation": "attestation.verify",
	"repo_id": repo_a,
	"subject_key": subject_key,
	"expected_generation": 5,
	"expected_cc_generation": 3,
	"gate": "code-review",
}

# --- allow cases ---------------------------------------------------------

test_attestation_verify_allow_hermes_native if {
	inp := attestation_verify_input(subject_reviewed)
	decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "matching code-review SHIP (Hermes-native)" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

# Doubles as v6's positive control for Fix I (round 5): base_attestations has NO entry at all for
# subject_cc_mirrored under "code-review" -- Hermes genuinely never reviewed this tree -- so this
# also proves the CC path stays reachable when Hermes has no opinion at all, not just when a
# REVISE overrides it (that override case is its own test below).
test_attestation_verify_allow_cc_mirrored_claude_ship if {
	inp := attestation_verify_input(subject_cc_mirrored)
	decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "matching code-reviewer SHIP (Claude Code native, claude backend only -- gpt/glm not verified, permanent limitation)" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_allow_hermes_native_when_cc_document_absent if {
	# data.warden_cc_attestations is never overridden at all here -- the likely early-production
	# shape before any CC mirror has ever been written. The Hermes-native path must still work.
	inp := attestation_verify_input(subject_reviewed)
	decision.allow with input as inp with data.warden_attestations as base_attestations
}

# --- deny cases ------------------------------------------------------------

test_attestation_verify_deny_no_attestation if {
	inp := attestation_verify_input(subject_new)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == sprintf("no SHIP found for %s (Hermes-native or Claude-Code-mirrored; or an explicit non-SHIP Hermes verdict exists; or OPA snapshot stale/unsupported)", [subject_new]) with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_mistargeted_document if {
	inp := attestation_verify_input(subject_cc_mistargeted)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_allow_mistargeted_document_control if {
	inp := attestation_verify_input(subject_cc_mistargeted_control)
	decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_wrong_gate if {
	# None of the three attestation.verify decision bodies' explicit `input.gate == "code-review"`
	# guard can fire when gate == "plan-review" -- falls to the file's own default deny, not a
	# mismatched match (round 2/3's tautology-avoidance check, same principle Fix B established).
	# NOTE: subject_reviewed has a genuine Hermes-native SHIP, so hermes_path_ok is TRUE here
	# regardless of input.gate (valid_ship never reads input.gate) -- this test therefore only
	# discriminates the FIRST decision body's own gate guard, not the CC-mirror body's (that
	# body's `not hermes_path_ok` check would already block it here for an unrelated reason). See
	# test_attestation_verify_deny_wrong_gate_on_cc_path immediately below for that guard.
	inp := object.union(attestation_verify_input(subject_reviewed), {"gate": "plan-review"})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "guardrails policy produced no valid decision" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_wrong_gate_on_cc_path if {
	# Code-reviewer round-1 finding: the test above can never discriminate the CC-mirror decision
	# body's OWN `input.gate == "code-review"` guard, because it always uses a subject where
	# hermes_path_ok is true, which already blocks that body via its `not hermes_path_ok` guard
	# for an unrelated reason -- so a deletion of the CC body's gate check went undetected at
	# PASS 46/46. Using subject_cc_mirrored (no Hermes record at all, so hermes_path_ok is FALSE)
	# isolates the CC-mirror body's own gate guard specifically.
	inp := object.union(attestation_verify_input(subject_cc_mirrored), {"gate": "plan-review"})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "guardrails policy produced no valid decision" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_wrong_gate_with_no_evidence_at_all if {
	# Isolates the FINAL (catch-all) decision body's own `input.gate == "code-review"` guard
	# (:220): with NO evidence anywhere (subject_new) AND a mismatched gate, both `hermes_path_ok`
	# and `cc_path_ok` are already false regardless of that guard, so the two tests above -- which
	# both use a subject with SOME evidence path resolvable -- can't discriminate it (deleting it
	# left the DEFAULT deny's own generic reason string producing the observably-same allow=false
	# either way). Pinning the exact reason string here proves the catch-all body's own gate guard
	# is what keeps the more specific "no SHIP found for..." reason from firing on a wrong-gate,
	# no-evidence query.
	inp := object.union(attestation_verify_input(subject_new), {"gate": "plan-review"})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "guardrails policy produced no valid decision" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_stale_hermes_falls_through_correctly if {
	# Hermes's OWN generation guard fails (expected_generation doesn't match data's generation) --
	# even though a perfectly valid CC-mirrored SHIP exists for this same subject, `cc_path_ok`
	# must NOT silently accept it: `supported` (Hermes's fresh-snapshot guard) is required
	# explicitly, not just "no Hermes SHIP found" (round 4's Fix H regression test).
	inp := object.union(attestation_verify_input(subject_cc_mirrored), {"expected_generation": 4})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_stale_hermes_generation_with_real_ship_present if {
	# Threat-modeler finding: the test above uses a subject with NO Hermes record at all, so it
	# never exercises `supported`'s generation check on a subject that has a genuine Hermes SHIP.
	# subject_reviewed has a real SHIP (att-1) -- with a stale/mismatched expected_generation, the
	# whole Hermes read must be treated as untrustworthy and deny, not silently trust the SHIP it
	# happens to still be able to see through the mismatch.
	inp := object.union(attestation_verify_input(subject_reviewed), {"expected_generation": 4})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_allow_hermes_native_precedence_when_cc_evidence_also_exists if {
	# Code-reviewer finding: no fixture previously had BOTH a fresh Hermes SHIP and a valid
	# CC-mirrored SHIP for the SAME subject -- the normal production state once LIA-534 lands (a
	# commit reviewed natively by both Hermes and Claude Code). subject_reviewed now has both
	# (att-1 Hermes-native, att-cc-coexist-with-hermes CC-mirrored). The Hermes-native path must
	# win, with its OWN reason string -- never the CC-mirror path, even though its evidence is
	# also genuinely valid.
	#
	# Code-reviewer round-2 finding: the decision/reason assertions below are byte-identical to
	# test_attestation_verify_allow_hermes_native's, so they pass identically whether or not
	# att-cc-coexist-with-hermes exists at all -- deleting the fixture left PASS 50/50. The
	# assertion below makes the fixture's own premise load-bearing: it independently confirms
	# valid_cc_mirrored_ship genuinely holds for this subject (i.e. real, valid CC evidence exists
	# and is being deliberately NOT chosen), so removing the fixture now fails THIS test even
	# though `decision` itself is unaffected.
	inp := attestation_verify_input(subject_reviewed)
	valid_cc_mirrored_ship with input as inp with data.warden_cc_attestations as base_cc_attestations
	decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == "matching code-review SHIP (Hermes-native)" with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_bad_contract_version if {
	inp := object.union(attestation_verify_input(subject_reviewed), {"contract_version": 99})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_session_kind_record if {
	inp := attestation_verify_input(subject_cc_session_probe)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_fresh_hermes_revise_not_overridden_by_cc_ship if {
	# Fix I's regression test (round 5, the single highest-value new test): subject_revised has a
	# FRESH Hermes REVISE (att-3) AND a valid CC-mirrored claude SHIP (att-cc-override) for the
	# SAME tree. The explicit Hermes REVISE must win -- deny, never allow via the CC path.
	#
	# Code-reviewer round-2 informational finding: pin that att-cc-override is genuinely valid CC
	# evidence (not just present), so the test's premise -- "real CC evidence exists AND is
	# correctly not used" -- is itself load-bearing, not incidental to fixture drift.
	inp := attestation_verify_input(subject_revised)
	valid_cc_mirrored_ship with input as inp with data.warden_cc_attestations as base_cc_attestations
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

# --- CC-side defense-in-depth ------------------------------------------------------------

test_attestation_verify_deny_cc_non_ship_verdict if {
	inp := attestation_verify_input(subject_cc_revise)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_cc_backend_field_mismatch if {
	inp := attestation_verify_input(subject_cc_backend_mismatch)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_cc_gate_field_mismatch if {
	inp := attestation_verify_input(subject_cc_gate_mismatch)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_cc_generation_mismatch if {
	stale_cc := object.union(base_cc_attestations, {"generation": 99})
	inp := attestation_verify_input(subject_cc_mirrored)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as stale_cc
}

test_attestation_verify_deny_cc_schema_version_mismatch if {
	bad_cc := object.union(base_cc_attestations, {"schema_version": 2})
	inp := attestation_verify_input(subject_cc_mirrored)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as bad_cc
}

test_attestation_verify_deny_cc_record_schema_version_mismatch if {
	inp := attestation_verify_input(subject_cc_record_schema_mismatch)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_deny_cc_record_repo_id_mismatch if {
	inp := attestation_verify_input(subject_cc_record_repo_mismatch)
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

# --- bidirectional isolation -----------------------------------------------------------

test_attestation_verify_bodies_never_fire_for_git_commit if {
	# A valid CC-mirrored SHIP exists for subject_cc_mirrored, but a git.commit query for that
	# same subject must still resolve via git.commit's OWN (unchanged) deny body, never leak
	# through any attestation.verify-only body.
	inp := object.union(base_input(subject_cc_mirrored), {"expected_cc_generation": 3})
	not decision.allow with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	decision.reason == sprintf("no code-review SHIP for staged tree %s", [subject_cc_mirrored]) with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
}

test_attestation_verify_bodies_never_fire_for_file_write if {
	# CC data populated alongside a file.write/plan-review query -- proves it doesn't interfere;
	# the pre-existing plan-review SHIP path must still resolve, with its ORIGINAL reason string.
	inp := plan_review_input(session_reviewed)
	d := decision with input as inp with data.warden_attestations as attestations_with_plan_review with data.warden_cc_attestations as base_cc_attestations with time.now_ns as now_fixed_ns
	d.allow
	d.reason == "matching plan-review SHIP"
}

test_attestation_verify_git_commit_bodies_never_fire_for_attestation_verify if {
	inp := attestation_verify_input(subject_reviewed)
	d := decision with input as inp with data.warden_attestations as base_attestations with data.warden_cc_attestations as base_cc_attestations
	d.reason != "matching code-review SHIP"
	d.reason != "repo not enrolled"
}
