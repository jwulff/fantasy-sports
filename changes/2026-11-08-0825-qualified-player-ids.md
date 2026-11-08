# Qualified expert player IDs

## Why

ESPN and FantasyPros numeric IDs overlap: ESPN 15818 identifies Keenan Allen,
while FantasyPros 15818 identifies Blake Countess. Bare-ID ambiguity safely
refused the lookup but stalled Fantasy’s player-news recovery. Closes #187.

## How

Expert news, injuries and projections accept `espn:<id>` and `fantasypros:<id>`.
Qualified selectors consult only that provider’s index and retain position
filtering and negative ESPN defense IDs. Invalid or unknown qualified IDs return
`CONFIG_INVALID` with argument details; they never fall back to names or the
other provider. Names and unambiguous bare IDs remain compatible, and ambiguous
bare IDs recommend the qualified syntax. Output row schemas are unchanged.

Fantasy’s operational ledger will use qualified keys and selectors while
retaining its existing coverage proof and immutable historical receipts.

## Validation

Offline regression coverage reproduces the actual collision across all three
commands, verifies unknown/malformed selectors and position mismatches cannot
switch namespaces, and exercises the CLI and defense IDs. Full offline suite,
coverage, lint, format, dependency/wheel and startup budgets run before shipping.
