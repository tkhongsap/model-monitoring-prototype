---
name: Agent task
about: A shaped, one-PR task for a coding agent, with the proof it must return
title: ""
labels: []
---

## Outcome
<!-- An end state, not an activity: "a count=0 window is stored and the cursor advances", not "fix the runner". -->

## Context
<!-- Links only: related issues and PRs, the plan under changes/<date>-<slug>/, the files this touches. -->

## Authoritative source
<!-- What settles "correct". Default: docs/MONITORING-CONTRACT.md (v1.1); it wins over code and stored observations. Name the section. -->

## Scope
**In:**
**Out (do not touch):**

## Done criteria
<!-- Each one names its check: a pytest node id, a command and expected output, a route and expected response. "Looks right" does not count. -->
- [ ]
- [ ]

## Verification (evidence to attach to the PR)
- Required checks from TESTING.md, with output: `.venv/bin/python -m pytest -q -m "not slow"` from `backend/`; `pnpm run typecheck` from the root; the row of the "Change-specific proof" table that applies.
- The real thing, driven: <route response from a local `uvicorn app.main:app` / screenshot of the dashboard page / `scripts/migrate.py` run twice>
- Report each done criterion as met, unmet or untested. Report `build:live` and `check:strict-live` (Linux only), a real producer, Langfuse and a live Claude judge as unavailable, never as passed.

## Tests
- Add or update: <behaviour that needs a test in `backend/tests/` which fails without the change>
- Marker: fast (default, every local run) / `slow` (DEMO-FULL bake, CI and full suite only)
- No test that cannot fail, and none that asserts the implementation instead of the behaviour.

## Risk and review tier
- Undone by a git revert alone? yes / no (no for: a migration in `backend/app/db.py`, cursor or observation writes, a deploy)
- Tier: low (docs only: agent review) / normal (agent review plus one human) / high (schema, cursors or stored observations, secrets, authorization, `scripts/deploy-*.sh`, CI workflows: human review of every line)
- Merging is always the owner's call (CLAUDE.md "Ask first").

## Guardrails
- Never: <e.g. rewrite `live_observations.payload` or `content_sha256`, change contract version `"1.1"`, add a dependency, loosen a health band or the strict-live bundle check>
- Stop and ask if: <e.g. the contract and the code disagree, a done criterion cannot be checked, the scope needs to grow>

## PR
- One branch, one small PR for this issue; Conventional Commits; link this issue.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md`; update `CHANGELOG.md` and `DEVLOG.md`; do not merge.
- A fresh-context reviewer checks it. Answer each review comment by changing the code or saying why not.

## Process fix (after it merges)
- If the agent had to be corrected: which test, check or line of context would have made this one-shot? File it as a separate issue.
