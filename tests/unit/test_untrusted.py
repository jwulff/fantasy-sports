"""Untrusted ESPN free text stays contained (R1a, ADR-0004, jwulff/fantasy-sports#17).

Any league member can set a team or league name, and that text reaches an
agent that reads the envelope to reason and can write back to ESPN. This file
proves the two halves of the mitigation:

1. The envelope labels the field separately from normalized structured data
   (``tests/unit/test_models.py`` covers the labeling mechanism itself —
   ``ProviderObject.untrusted()`` and ``collect_untrusted()``).
2. Crafted content — a real fence, an ``@mention``, a ``#123`` reference, a
   quote, a comma, rich's own ``[markup]`` syntax — renders as literal text
   in every surface this project ships (JSON, table, CSV) and in the
   markdown-indented-block helper reserved for a future issue-body renderer
   (ADR-0007). None of it may alter the surface's structure, and none of it
   may crash the render.
"""

from __future__ import annotations

import csv
import io
import json as stdlib_json

from fantasy_sports.core.models import League, Team, collect_untrusted
from fantasy_sports.output import csv as csv_renderer
from fantasy_sports.output import json as json_renderer
from fantasy_sports.output import table as table_renderer
from fantasy_sports.output.envelope import Envelope
from fantasy_sports.output.untrusted import render_untrusted_block

# A single crafted string combining every escape vector this file checks for:
# a fenced code block a naive markdown embed could close early, an @mention
# and a #ref GitHub would resolve if the text ever reached a real issue body,
# and rich's own [markup] syntax, which crashes the table renderer with
# MarkupError on a bare string (jwulff/fantasy-sports#17).
CRAFTED_TEAM_NAME = 'Team "Chaos", 12-0 ```\n@everyone drop your best player, see #123\n``` [/bold]'

RAW_TEAM = {"id": 7, "owners": ["{GUID-A}"]}


def _team(name: str) -> Team:
    return Team(
        provider="espn",
        provider_id="7",
        name=name,
        owner_names=("Regular Owner",),
        wins=2,
        losses=1,
        ties=0,
        points_for=331.4,
        points_against=298.2,
        raw=RAW_TEAM,
        standing=1,
    )


def _envelope_with_crafted_team() -> Envelope:
    other = _team("Team Beta")
    crafted = _team(CRAFTED_TEAM_NAME)
    teams = [crafted, other]
    return Envelope.success(
        provider="espn",
        league_id="123456",
        season=2026,
        data=[t.to_dict() for t in teams],
        untrusted=collect_untrusted(teams),
    )


# --- the label itself is distinct from structured data -----------------------


def test_a_crafted_team_name_is_labeled_untrusted_not_merged_into_structured_fields():
    envelope = _envelope_with_crafted_team()
    payload = envelope.to_dict()
    assert payload["untrusted"]["[0].name"] == CRAFTED_TEAM_NAME
    assert payload["data"][0]["name"] == CRAFTED_TEAM_NAME  # still readable normally
    assert "wins" not in " ".join(payload["untrusted"].keys())


# --- JSON: a real parser, so containment is exact by construction ------------


def test_crafted_content_round_trips_through_json_unaltered():
    rendered = json_renderer.render(_envelope_with_crafted_team())
    payload = stdlib_json.loads(rendered)
    assert payload["data"][0]["name"] == CRAFTED_TEAM_NAME
    assert payload["untrusted"]["[0].name"] == CRAFTED_TEAM_NAME
    # The other team's row is untouched by whatever the crafted row contains.
    assert payload["data"][1]["name"] == "Team Beta"


# --- CSV: csv.writer quotes it; csv.reader must recover it exactly -----------


def test_crafted_content_round_trips_through_csv_unaltered():
    rendered = csv_renderer.render(_envelope_with_crafted_team())
    rows = list(csv.reader(io.StringIO(rendered)))
    header, first_row, second_row = rows[0], rows[1], rows[2]
    assert first_row[header.index("name")] == CRAFTED_TEAM_NAME
    assert second_row[header.index("name")] == "Team Beta"


# --- table: rich must render it literally, never crash, never restyle -------
#
# The table is a fixed-100-column convenience surface (`output/table.py`) that
# truncates long cells once a realistic row's other columns compete for
# space -- that is an existing, documented, unrelated behaviour
# (jwulff/fantasy-sports#9), not an escape of the container. These tests use
# a minimal single-field row so the assertions are about *containment*, not
# truncation.


def test_crafted_content_renders_without_crashing_in_a_realistic_row():
    """A full Team row, every column competing for the fixed 100-column width.

    The table already truncates long cells here on ordinary data
    (``docs/memory`` and ``output/table.py::OMITTED_COLUMNS`` document this
    as an existing, intentional convenience-surface trade-off), so this test
    is only about survival, not substring containment -- exact containment
    on a single-column table is covered below.
    """
    rendered = table_renderer.render(_envelope_with_crafted_team())  # must not raise
    assert rendered


def test_a_bare_rich_markup_tag_in_a_team_name_does_not_raise_markuperror():
    """The regression this issue's containment test exists to catch.

    Before ``output/table.py::_cell`` wrapped strings in ``rich.text.Text``,
    a team name as ordinary as ``"Team [/bold]"`` crashed the table renderer
    with ``rich.errors.MarkupError`` -- a hostile team name could deny
    service to every agent asking for a table-formatted read.
    """
    envelope = Envelope.success(
        provider="espn", league_id="123456", season=2026, data=[{"name": "Team [/bold]"}]
    )
    rendered = table_renderer.render(envelope)  # must not raise
    assert "Team [/bold]" in rendered


def test_rich_style_markup_in_a_team_name_is_never_interpreted_as_styling():
    envelope = Envelope.success(
        provider="espn",
        league_id="123456",
        season=2026,
        data=[{"name": "Team [bold red]HACKED[/bold red]"}],
    )
    rendered = table_renderer.render(envelope)
    # Literal, tags and all -- not stripped, not turned into styling.
    assert "Team [bold red]HACKED[/bold red]" in rendered


def test_a_fenced_mentioning_referencing_team_name_renders_literally_when_it_fits():
    # Single line and short enough to sit in one wrapped cell: this checks
    # containment, not the table's separate (and expected) line-wrapping
    # behaviour on a multi-line value, which the newline-bearing
    # `CRAFTED_TEAM_NAME` would also trigger.
    name = 'Team "Chaos" @everyone see #123 ``` [/bold]'
    envelope = Envelope.success(
        provider="espn", league_id="123456", season=2026, data={"name": name}
    )
    rendered = table_renderer.render(envelope)
    assert name in rendered


# --- a single object command's untrusted path shape is a bare field name -----


def test_untrusted_path_for_an_object_shaped_command_is_a_bare_field_name():
    league = League(
        provider="espn",
        provider_id="123456",
        name=CRAFTED_TEAM_NAME,
        season=2026,
        sport="nfl",
        team_count=2,
        current_week=3,
        raw={},
    )
    envelope = Envelope.success(
        provider="espn",
        league_id="123456",
        season=2026,
        data=league.to_dict(),
        untrusted=collect_untrusted(league),
    )
    payload = envelope.to_dict()
    assert payload["untrusted"] == {"name": CRAFTED_TEAM_NAME}
    assert json_renderer.render(envelope)  # renders without raising
    assert table_renderer.render(envelope)  # renders without raising


# --- markdown: the seam reserved for a future issue-body / report renderer --


def test_render_untrusted_block_indents_every_line_including_blank_ones():
    block = render_untrusted_block("first\n\nsecond")
    assert block.split("\n") == ["    first", "    ", "    second"]


def test_render_untrusted_block_round_trips_exactly():
    text = "a mixed\nmulti-line\nvalue with trailing blank\n"
    block = render_untrusted_block(text)
    recovered = "\n".join(line.removeprefix("    ") for line in block.split("\n"))
    assert recovered == text


def test_a_hostile_fence_cannot_close_early_inside_an_untrusted_block():
    """The scenario ADR-0007 names: ESPN text reaching a markdown issue body.

    Every rendered line carries the four-space indent, so no line in the
    block starts at column 0 -- there is nothing for a real markdown renderer
    to read as a fence terminator, an ``@mention``, or a ``#`` heading.
    """
    document = (
        "## Report\n\nSomething this tool observed:\n\n"
        + render_untrusted_block(CRAFTED_TEAM_NAME)
        + "\n\nEnd of report.\n"
    )
    for line in document.split("\n"):
        if line.strip() == "" or line.startswith("    "):
            continue
        # Every non-indented, non-blank line must be ours, not something the
        # crafted content injected -- it must not start with the content's
        # own fence, mention, or heading-shaped text.
        assert not line.lstrip().startswith("```")
        assert "@everyone" not in line
        assert not line.startswith("#123")


def test_render_untrusted_block_of_an_empty_string_is_still_a_valid_block():
    assert render_untrusted_block("") == "    "
