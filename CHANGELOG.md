# Changelog

Notable user- and operator-visible changes are recorded here. Development notes
and evidence belong in [DEVLOG.md](DEVLOG.md); decision rationale belongs in ADRs
(none yet; the first arrives with the alerting slice).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Playbook baseline documents: `README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`,
  `DEVLOG.md`, `TESTING.md`.
- `backend/tests/test_docs.py` enforces the documentation contract (required files,
  one H1, `CLAUDE.md` within 120 lines, internal links resolve, no machine-local paths,
  `postMerge` hook never pushes a schema).

### Fixed

- The Replit `postMerge` hook no longer runs `pnpm --filter db push`, which pushed an
  empty Drizzle schema at the backend's PostgreSQL database. It only reinstalls
  workspace dependencies; the backend migrates itself at startup.
- `replit.md` no longer tells operators to `cd frontend` (that directory is not a
  workspace package) or that the deploy build "runs migrations".
