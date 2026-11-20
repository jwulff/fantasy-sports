# Do not apply cookie age warnings to API keys

The 30-day threshold is an unverified heuristic for ESPN cookies. It cannot
establish whether a FantasyPros API key has expired, and telling someone to
re-extract a cookie is misleading for a key. The FantasyPros credential opts
out through its credential specification. Its recorded age remains visible,
but freshness is unknown; ESPN cookie expiry and age warnings are unchanged.
