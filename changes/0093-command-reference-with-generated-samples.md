# A command reference whose samples cannot drift (#93)

The CLI launched publicly on 2026-09-12 with a README that explained the
design well and showed almost none of what the commands return. A reader
deciding whether to install, or an agent deciding whether to call, had to
run `roster` or provoke an error to find out what either looked like. The
ask was first-class docs: every command's output visible before installing,
in every output mode, with the whole error contract — and not hand-typed,
because a hand-typed sample is a sample that is wrong by the second release.

## What changed

- **`docs/commands.md` is the reference.** Every command `--help` lists
  (`auth login|logout|status`, `doctor`, `league info`, `teams`,
  `standings`, `roster`, `matchups`, `box-scores`, `free-agents`,
  `transactions`, `raw`) has a section: purpose, the options that matter,
  and the JSON envelope. The global options each have a worked example, the
  three output modes are shown with real output, `--no-raw` is shown before
  and after, and piping is demonstrated by piping. Every code in the
  taxonomy has its exact stderr shape, its exit status, and a real or
  provoked example; usage errors (exit 2) are shown as the prose they are.
  The README links every command by name, keeps Install / First run /
  Credentials, replaces its two hand-written envelopes with generated ones,
  and gains a **Using it from an agent** section: stdout/stderr split, exit
  codes, `--no-raw`, `--fresh` versus the cache, `data_as_of`, reading
  `error.code` and `remediation`, and a worked retry loop.

- **`scripts/render_readme_samples.py` generates every sample.** Three
  sources, and every sample header names its own:
  - *live* — ESPN's public test league `1234`/`2018`, the only real league
    whose output may be committed (`docs/testing.md` §6). The CLI insists on
    two cookies before it sends anything, so the generator sets two
    placeholder values in the environment; ESPN serves a public league
    without reading them. **No secret is needed to regenerate**, and the
    README now says so.
  - *replayed* — the invented league `99`/`2026` from
    `tests/cassettes/espn/synthetic_2026.yaml`, served through the same
    `RecordedEspn` transport stub the unit tests use, for `box-scores` and
    `free-agents` (ESPN refuses both before 2019 — the refusal itself is a
    live sample, and a good `NOT_AVAILABLE` one). The `auth` commands run
    here too, against an in-memory Keychain, with the prompt answered by the
    script (SWID deliberately without its braces, so `repaired` is
    non-empty).
  - *synthetic* — `AUTH_EXPIRED`, `PROVIDER_UNAVAILABLE`, `RATE_LIMITED`,
    and `SCHEMA_DRIFT` are built from the exception classes in
    `core/errors.py` with the messages and details the adapter uses, and
    rendered by the output layer. Nobody can safely provoke those four.

  Long output is trimmed to valid JSON with a visible `…` and a count for
  every cut; every top-level envelope key survives. Samples land in
  `docs/samples/` with an `index.json` (command, source, league, exit,
  stream, format, trimmed, note) and are spliced between
  `<!-- sample: NAME -->` / `<!-- /sample -->` markers — chosen over
  includes because GitHub renders no includes, and the whole point is that
  the page reads on GitHub.

- **`tests/unit/test_readme_samples.py` holds the line, offline.** Every
  JSON sample parses, carries `"schema": "fantasy-sports/v1"`, and has the
  full envelope key set; every error sample names a taxonomy code and the
  index records that code's exit status; every code in `ErrorCode` has at
  least one sample; no sample names a league outside `{1234, 99}`; and both
  docs are byte-identical to what splicing the committed samples produces,
  so a hand edit inside a marker block or to a sample file fails CI.
  `test_readme.py` now scans `docs/commands.md`'s command lines against the
  registry alongside the README's.

## Decisions worth recording

- **A separate `docs/commands.md`, not a longer README.** The reference is
  nearly 3,000 lines with samples; the README is included in the wheel's
  metadata and counts against the 256 KB budget. The README keeps a table linking
  every command and the agent section, which is what an agent gets pointed
  at first.
- **In-process, not subprocess, for the samples.** `cli.app.run(argv)` is
  the console script's own path after the fast path, and capturing stdout
  into a buffer is exactly what a pipe looks like to the format detector —
  so a sample with no `--output` emits JSON for the same reason it would
  under `| jq`. Two things use the real console script: `--help`, which
  lives on the fast path, and the pipe demonstration, which pipes for real
  into `head`. `sys.argv[0]` is set to `fantasy-sports` for the duration so
  typer's usage lines name the right program.
- **The README now carries a Python fence, and `ruff format` formats
  Markdown code blocks.** `docs/` is excluded on purpose
  (`docs/memory/ruff-format-rewrites-markdown.md`); the README is not, so the
  agent-loop snippet is kept ruff-clean rather than widening the exclude.
- **Nothing private, by construction.** Every command runs against a
  throwaway XDG tree with `PYTHON_KEYRING_BACKEND` pointed at the null
  backend, so the developer's real Keychain is never read — if it were, the
  `AUTH_MISSING` sample would come back as a success and the generator would
  refuse to write. The only rewrite applied to captured output is the
  sandbox path back to `~/.config` and `~/.cache`.

## What documenting turned up

Left for follow-up issues rather than fixed here (the issue's out-of-scope
rule):
error envelopes from read commands carry `provider: null, league_id: null,
season: null` even after the profile resolved; `NOT_AVAILABLE` reports
`details.season` and `details.week` as strings; `points_for` renders float
artifacts (`1402.7200000000003`); `LEAGUE_NOT_FOUND` for a missing config
file and `CONFIG_INVALID` for a broken one both carry `remediation: null`;
`raw --filter`'s own remediation suggests `{"players":{"limit":50}}`, which
ESPN answers with a 400 that then classifies as a *retryable*
`PROVIDER_UNAVAILABLE`; and the synthetic box-score fixture's stat rows are
for scoring period 13 while the interaction serves period 1, so every
replayed player reads `0.0` points (the sample says so).
