#!/usr/bin/env python
"""Build ``tests/cassettes/espn/fan_2026.yaml``, a hand-authored fan.api fixture.

    uv run python scripts/build_fan_cassette.py

The ``leagues discover`` command's fixture (jwulff/fantasy-sports#157). It is
**synthetic**: built from the shape documented in
``docs/memory/espn-401-tells-you-nothing.md`` ("The membership probe") and
``docs/research/05-espn-write-surface.md``, with no live request and no real
credential. A hand-built payload proves the reader handles the shape it was
told about, not that ESPN still sends it; ``docs/testing.md`` records that.

Three things the body deliberately contains, because a test reads them:

* a **football** league (``99``, the invented public league) the account
  manages;
* a **non-football** entry (``1234``, ESPN's public test league id, reused as a
  hockey league: a league id is just a number to this reader) that the account
  does not manage, with a game abbreviation (``fhl``) the sport map knows;
* a **preference that is not type 9** and still carries a ``groups[]`` block.
  A reader that keys on ``groups`` rather than ``type.id`` would return it as
  a third league.

Observed live (2026-10-27, unrecorded): the game abbreviation is
``metaData.entry.abbrev``, uppercase (``"FFL"``), so this fixture uses
uppercase and the reader's lowercasing is exercised. The reader also accepts
``gameAbbrev`` and ``game.abbrev`` as unobserved fallbacks.

**The point of this fixture is its request URI.** The SWID travels in the URL
path as ``%7B<guid>%7D``. The GUID below is obviously fake, and it goes through
the same ``scrub_uri`` / ``scrub_text`` the recording hooks use, so the
committed file holds the pseudonym, never the GUID. The finished file is
scanned, and deleted if anything credential-shaped survived.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from conftest import scan_file, scrub_text, scrub_uri  # noqa: E402

CASSETTE = REPO / "tests" / "cassettes" / "espn" / "fan_2026.yaml"

FAKE_SWID = "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"
SEASON = 2026


def _membership(group_id: int, name: str, entry_id: int, abbrev: str, *, manager: bool) -> dict:
    return {
        "id": f"{abbrev.lower()}-{group_id}",
        "type": {"id": 9, "name": "Fantasy League Manager"},
        "metaData": {
            "entry": {
                "entryId": entry_id,
                "seasonId": SEASON,
                "abbrev": abbrev,
                "groups": [
                    {
                        "groupId": group_id,
                        "groupName": name,
                        "groupManager": manager,
                        "href": f"https://fantasy.espn.com/{abbrev.lower()}/league?leagueId={group_id}",
                    }
                ],
            }
        },
    }


def body() -> dict[str, Any]:
    return {
        "id": FAKE_SWID,
        "preferences": [
            _membership(99, "Synthetic Gridiron", 3, "FFL", manager=True),
            {
                "id": "not-a-membership",
                "type": {"id": 1, "name": "Alerts"},
                "metaData": {
                    "entry": {
                        "entryId": 1,
                        "seasonId": SEASON,
                        "abbrev": "FFL",
                        "groups": [
                            {"groupId": 1234, "groupName": "Not A Membership", "groupManager": True}
                        ],
                    }
                },
            },
            _membership(1234, "Synthetic Ice", 7, "FHL", manager=False),
        ],
        "fantasyData": {"totalFantasyLeagues": 2},
    }


def interaction() -> dict[str, Any]:
    from urllib.parse import quote

    uri = f"https://fan.api.espn.com/apis/v2/fans/{quote(FAKE_SWID, safe='')}?featureFlags=fantasy"
    return {
        "request": {"body": None, "headers": {}, "method": "GET", "uri": scrub_uri(uri)},
        "response": {
            "body": {"string": scrub_text(json.dumps(body(), separators=(",", ":")))},
            "headers": {"Content-Type": ["application/json"]},
            "status": {"code": 200, "message": "OK"},
        },
    }


def main() -> int:
    import yaml

    CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    CASSETTE.write_text(
        yaml.safe_dump({"interactions": [interaction()], "version": 1}, sort_keys=False)
    )
    findings = scan_file(CASSETTE)
    if findings:
        CASSETTE.unlink()
        print("refusing to keep the cassette; credential-shaped text survived:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print(f"wrote {CASSETTE.relative_to(REPO)} ({CASSETTE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
