# Preserve status-less trade acceptance drops

Dragon Slayers transactions failed because ESPN returned a nonpending EXECUTE
TRADE_ACCEPT with collateral DROP items and no status. The existing metadata-only
exception deliberately did not match real player items.

Recognize only this observed acceptance shape, pass an explicit unknown status
to espn-api, and retain the player items in normalized output. Do not infer that
ESPN supplied EXECUTED, mutate the cached response, or broaden the exception to
other malformed transactions. Six offline shape regressions cover preservation
and refusal. Full offline suite: 2,850 passed, 20 opt-in live tests skipped.

Refs #189.
