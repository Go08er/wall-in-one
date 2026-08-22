"""Display authoring choices that do not need GTK or a compositor."""

from __future__ import annotations

from wall_in_one import display_policy


def test_saved_attached_display_owns_the_palette() -> None:
    choice = display_policy.resolve_theme_source("DP-2", ("eDP-1", "DP-2"))

    assert choice.configured == "DP-2"
    assert choice.effective == "DP-2"
    assert choice.configured_attached is True
    assert choice.is_fallback is False


def test_detached_choice_is_preserved_while_lexical_first_temporarily_wins() -> None:
    choice = display_policy.resolve_theme_source("DP-9", ("eDP-1", "DP-2"))

    assert choice.configured == "DP-9"
    assert choice.effective == "DP-2"
    assert choice.configured_attached is False
    assert choice.is_fallback is True


def test_empty_and_detached_fallback_use_the_lexical_first_live_output() -> None:
    automatic = display_policy.resolve_theme_source("", ("DP-2", "eDP-1"))
    detached = display_policy.resolve_theme_source("DP-9", ("DP-2", "eDP-1"))

    assert automatic.effective == "DP-2"
    assert automatic.is_fallback is False
    assert detached.effective == "DP-2"
    assert detached.configured == "DP-9"


def test_no_output_does_not_invent_a_palette_owner() -> None:
    choice = display_policy.resolve_theme_source("DP-9", ())

    assert choice.configured == "DP-9"
    assert choice.effective == ""
    assert choice.is_fallback is True
