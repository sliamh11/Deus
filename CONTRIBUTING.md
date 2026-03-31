# Contributing to Deus

## Source Code Changes

**Accepted:** Bug fixes, security fixes, simplifications, reducing code.

**Not accepted:** Features, capabilities, compatibility, enhancements. These should be skills.

## Skills

A [skill](https://code.claude.com/docs/en/skills) is a markdown file in `.claude/skills/` that teaches Claude Code how to transform a Deus installation.

A PR that contributes a skill should not modify any source files.

Your skill should contain the **instructions** Claude follows to add the feature — not pre-built code. See `/add-telegram` for a good example.

### Why?

Every user should have clean and minimal code that does exactly what they need. Skills let users selectively add features to their fork without inheriting code for features they don't want.

### Testing

Test your skill by running it on a fresh clone before submitting.

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup, key files, and service management.

## Reporting Issues

Use [GitHub Issues](https://github.com/sliamh11/Deus/issues) for bug reports and feature requests.

## Security

See [SECURITY.md](SECURITY.md) for reporting security vulnerabilities.
