"""Agent-native exit code constants for Deus Python CLIs.

Cross-references docs/decisions/error-discipline.md (TS four-class taxonomy):
  UserError  -> USAGE_ERROR(2), NOT_FOUND(3)
  FatalError -> AUTH_ERROR(4), INTERNAL_ERROR(5)
  ABSTAIN(1) is CLI-specific: "no result, not an error" -- no TS equivalent.
  CONFLICT(6) is CLI-specific: "a precondition collided with concurrent state"
    (e.g. another session already owns this checkout) -- no TS equivalent, like ABSTAIN.
"""

SUCCESS = 0
ABSTAIN = 1
USAGE_ERROR = 2
NOT_FOUND = 3
AUTH_ERROR = 4
INTERNAL_ERROR = 5
CONFLICT = 6
RATE_LIMIT = 7
