# FantasyPros refuses an argument with a body, not a status you can trust alone

**Found:** 2026-10-16, building #128. **Applies to:** `sources/fantasypros.py`,
the commands over it (#129), and anything that classifies an upstream answer by
HTTP status.

The issue's capture (2026-10-16 00:50Z) saw `consensus-rankings?position=XX`
answer **200** with `{"message": "Invalid Position", …}` and no data. A probe
through the finished client an hour later, same endpoint, same bad value, got
**400** with `{"message": "Invalid Position", "parameter": "position",
"valid_format": "…"}`. Same refusal, two statuses, one morning.

So the client classifies on the *body* for both: a JSON object carrying a
string `message` and none of the endpoint's data is `CONFIG_INVALID` with
`kind="argument"` and the message verbatim, whether the status was 200 or 4xx.
A 4xx *without* such a body stays unclassifiable and lands on
`PROVIDER_UNAVAILABLE` (R12). Do not "fix" this by keying on 400 alone; the
200 shape is real and was the first one seen.

Two more things the shapes told us that the issue could not:

- **`type` on consensus rankings is not always `"Weekly PPR"`.** QB answered
  `"Weekly"` — scoring does not apply to the position. Do not parse it.
- **`count` is the list's length, not the pool's.** `players` says 8,564;
  `?player=<fpid>` says 1. Truncation is signalled by `tier`/`limit`, never by
  a short `count`.

Related: [[no-code-for-a-bad-argument]], [[espn-401-tells-you-nothing]],
[[cache-redaction-and-tag-classes]]
