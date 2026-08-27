# Architecture Decision Records

Individual decisions extracted from `docs/ARCHITECTURE.md`. The architecture doc
is the narrative; these are the citable, individually-supersedable records.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-python-as-implementation-language.md) | Python as the implementation language | Accepted |
| [0002](0002-normalize-shape-not-semantics.md) | Normalize shape, not semantics | Accepted |
| [0003](0003-command-registry-cli-and-mcp-as-projections.md) | Commands are a typed registry; CLI and MCP are projections | Accepted |
| [0004](0004-versioned-output-contract-and-error-taxonomy.md) | Versioned output contract and error taxonomy | Accepted |
| [0005](0005-health-system-canary-manifest-client-check.md) | Health system — canary, manifest, client check | Accepted |
| [0006](0006-read-only-v01-gated-writes-later.md) | Read-only v0.1; writes gated later | Accepted |
| [0007](0007-client-error-reporting-via-user-credential.md) | Client error reporting uses the operator's own `gh` credential | **Proposed** |

Use `0000-template.md` for new records.
