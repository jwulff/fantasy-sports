"""The registry is the single source of truth for what commands exist."""

from __future__ import annotations

import pytest

from fantasy_sports.commands import REGISTRY, CommandSpec, groups, in_group, register, top_level


@pytest.fixture
def clean_registry():
    saved = dict(REGISTRY)
    REGISTRY.clear()
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(saved)


def test_registry_starts_empty_until_the_read_commands_land():
    assert REGISTRY == {}, "commands land with jwulff/fantasy-sports#9"


def test_register_keys_by_full_invocation(clean_registry):
    register(
        CommandSpec(name="status", summary="Report credential age.", handler="m:f", group="auth")
    )
    assert "auth status" in clean_registry
    assert groups() == ["auth"]
    assert [s.name for s in in_group("auth")] == ["status"]
    assert top_level() == []


def test_top_level_commands_have_no_group(clean_registry):
    register(CommandSpec(name="standings", summary="Show standings.", handler="m:f"))
    assert [s.name for s in top_level()] == ["standings"]
    assert groups() == []


def test_duplicate_registration_is_rejected(clean_registry):
    spec = CommandSpec(name="teams", summary="List teams.", handler="m:f")
    register(spec)
    with pytest.raises(ValueError, match="already registered"):
        register(spec)


def test_handler_resolves_lazily():
    spec = CommandSpec(name="x", summary="s", handler="fantasy_sports.commands:groups")
    assert spec.resolve() is groups


def test_a_malformed_handler_is_rejected_at_resolve_time():
    with pytest.raises(ValueError, match="module:function"):
        CommandSpec(name="x", summary="s", handler="no_colon_here").resolve()
