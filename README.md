# fantasy-sports

An agent-native command-line interface for fantasy sports leagues.

Structured JSON by default, human tables when you're at a terminal. Built so
that both you and an AI agent can drive it — and so it tells you when the
upstream API breaks instead of handing you a stack trace.

**Status:** pre-alpha. Nothing works yet. Design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Why

ESPN has no official fantasy API. The unofficial one changes without notice, and
every previous attempt at tooling around it died the same way: ESPN moved
something, nobody noticed for months, the repo went quiet.

This project treats that as the central design problem rather than an
afterthought. A scheduled canary watches the real API and files an issue the day
it drifts; the CLI reads that signal and tells you whether you need to upgrade,
whether it's a known outage, or whether you've found something new.

## Install

```bash
uv tool install fantasy-sports
```

## Use

```bash
fantasy-sports doctor                       # is everything healthy?
fantasy-sports auth status                  # are my credentials still good?
fantasy-sports standings --league dynasty
fantasy-sports roster --team "Wulff"
fantasy-sports matchups --week 1
fantasy-sports free-agents --pos WR --limit 20
fantasy-sports raw --view mSettings         # escape hatch
```

Add `--output json` (or just pipe it) for machine-readable output. Every payload
is versioned and every error carries a stable machine code.

## Providers

| Provider | Status |
|---|---|
| ESPN | v0.1 target |
| Sleeper | designed for, not implemented |
| Yahoo | designed for, not implemented |

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — the full design and its rationale
- [ADRs](docs/adr/) — individual decision records
- [Research](docs/research/) — background briefs that informed the design

## License

MIT
