# A cassette scrubber that reads bytes it cannot decode is a no-op

**Found:** 2026-09-05, building U13 (#12). **Applies to:** U9, U7, and anything
that records ESPN traffic.

The body scrubber that redacts SWID GUIDs echoed in ESPN responses is a regex
over the recorded body. There are two ways for that body to arrive as opaque
bytes, and in both of them the regex matches nothing, the recording looks
clean, and the credential is on disk.

**1. Compression.** `requests` sends `Accept-Encoding: gzip, deflate`, so ESPN
answers gzipped. vcrpy records the bytes it received — still compressed —
unless `decode_compressed_response=True` is set. With that flag, vcrpy's
`decode_response` runs *before* `before_record_response` (composed in
`vcr/config.py:_build_before_record_response`), so the scrubber sees plain
text. Without it, the scrubber runs against a gzip stream and finds nothing.
The flag is in `build_vcr_config()` in `tests/conftest.py`. **Do not remove
it**, and do not assume a passing scrub test proves anything if the fixture it
was written against was uncompressed.

`decode_response` only knows gzip, deflate, and brotli. A future
`Content-Encoding` it does not recognise (zstd) is returned untouched.

**2. `!!binary`.** When a body is not UTF-8 decodable, vcrpy's YAML serializer
writes it as a base64 `!!binary` scalar (`serializers/compat.py` gives up
quietly on `UnicodeDecodeError`). A grep-based credential scan — the obvious
implementation, and the one the research brief in
`docs/research/04-python-cli-packaging.md` §4 suggests as a pre-commit hook —
cannot see through base64. `scan_file()` therefore parses cassette YAML and
scans decoded `bytes` scalars separately from the text pass.

Both holes are closed by the same rule: **a body that cannot be read cannot be
proven clean.** `_scrub_body_value()` raises `UnscrubbableResponseError` rather
than recording bytes it could not decode. If a recording session hits that,
the answer is to find out what encoding arrived — not to catch the exception.

Related trap, cheaper: vcrpy's `filter_headers` takes bare names *or*
`(name, replacement)` tuples. A bare name **deletes** the header. A cassette
with no `Cookie` line is indistinguishable from one recorded without
credentials, so nobody can tell from the fixture whether the scrub ever ran.
Always use the tuple form.
