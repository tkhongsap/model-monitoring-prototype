# Architecture Decision Records

An ADR records a decision that changes who is responsible for something, what the
monitor promises operators, or how it is deployed and kept running. File names use
`NNNN-short-decision.md`; status values are `Proposed`, `Accepted`, `Deprecated` or
`Superseded`. An accepted decision is a historical record: supersede it with a new ADR
rather than rewriting why it was made.

Start from the playbook template (`templates/adr.md` in
`tkhongsap-ai-engineering-playbook`); the sections are Context, Decision criteria,
Options considered, Decision, Consequences, Validation and rollback, Sources.

## Index

| ADR | Status | Decision |
|---|---|---|
| [0001](0001-alert-ownership.md) | Accepted | The RAI team owns alert triage through the monitor's webhook channel; producers are not paged |
