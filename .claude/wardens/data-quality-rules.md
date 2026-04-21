# Data Quality Warden Rules

Review auto-memory files for retrieval quality. A file's `description:` frontmatter is the primary embedding source for semantic search — weak descriptions cause retrieval misses.

## Rules

### description-quality
Every auto-memory file's `description:` must contain the key terms a user would search for. If the description uses abbreviations, project codenames, or indirect references without the common search terms, flag it.

**Bad:** "Approved PH headline, tone direction" (missing "product hunt", "launch")
**Good:** "Product Hunt launch plan — approved headline, directory submission wave, launch-day strategy"

### description-length
Descriptions should be 15-40 words. Under 10 words lacks discriminative signal. Over 50 words dilutes the embedding.

### description-vs-body
If the file body contains important terms not in the description, flag the gap. The description should be a query-friendly summary of the body's key concepts.

### name-field
Every file must have a `name:` field that serves as a human-readable title. Names should be concise (3-8 words) and descriptive.

### type-field
Must be one of: `feedback`, `project`, `reference`, `user`. No other values.

### stale-project-state
Project files with "DONE" or "COMPLETED" in description or body should either be archived (moved to ARCHIVE/) or have their description updated to reflect completion status so queries like "active projects" don't retrieve them.

## Output Format

For each file reviewed:
```
[PASS|WARN|FAIL] <filename>
  <rule-id>: <explanation>
  Suggested fix: <concrete rewrite if applicable>
```

## When to Run

- After bulk reindex-external operations
- After adding 5+ new auto-memory files
- Quarterly as part of anti-erosion audit
- When retrieval benchmarks show recall regression
