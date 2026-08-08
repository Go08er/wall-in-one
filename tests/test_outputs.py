"""Finding the screens, so a wallpaper can be aimed at one.

The single-output case is pinned against niri's real reply, captured from the
development machine. The multi-output cases are recorded JSON, because there
is only one screen here -- which is the honest limit of what these prove.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from wall_in_one.wallpaper import outputs

#: niri 26.04's actual answer on the development machine. The active mode is
#: included because scene capture now needs physical pixels, not only logical
#: compositor coordinates.
REAL_SINGLE = {
    "eDP-1": {
        "name": "eDP-1",
        "make": "BOE",
        "model": "0x0A9B",
        "serial": None,
        "physical_size": [340, 210],
        "current_mode": 1,
        "modes": [
            {"width": 1920, "height": 1200, "refresh_rate": 60000},
            {"width": 2560, "height": 1600, "refresh_rate": 165000},
        ],
        "vrr_supported": True,
        "vrr_enabled": False,
        "logical": {
            "x": 0,
            "y": 0,
            "width": 1706,
            "height": 1066,
            "scale": 1.5,
            "transform": "Normal",
        },
    }
}

TWO = {
    "DP-2": {
        "name": "DP-2",
        "make": "Dell",
        "model": "U2720Q",
        "logical": {"x": 1706, "y": 0, "width": 2560, "height": 1440, "scale": 1.0},
    },
    "eDP-1": {
        "name": "eDP-1",
        "make": "BOE",
        "model": "0x0A9B",
        "logical": {"x": 0, "y": 0, "width": 1706, "height": 1066, "scale": 1.5},
    },
}


def test_the_real_reply_from_this_machine_parses() -> None:
    found = outputs.parse(REAL_SINGLE)

    assert len(found) == 1
    assert found[0].name == "eDP-1"
    assert found[0].make == "BOE"
    assert (found[0].width, found[0].height) == (1706, 1066)
    assert found[0].scale == 1.5
    assert (found[0].physical_width, found[0].physical_height) == (2560, 1600)


def test_two_screens_come_back_in_a_fixed_order() -> None:
    """niri returns an object, and nothing promises the same order twice.

    A display list that reshuffles between openings is worse than one in an
    arbitrary but stable order.
    """
    assert outputs.names(outputs.parse(TWO)) == ("DP-2", "eDP-1")
    reversed_document = dict(reversed(list(TWO.items())))
    assert outputs.names(outputs.parse(reversed_document)) == ("DP-2", "eDP-1")


def test_the_label_always_carries_the_connector() -> None:
    """The connector is the name in the config file and in every renderer.

    A settings list that says "BOE 0x0A9B" while the config says `eDP-1` is a
    puzzle rather than a description.
    """
    label = outputs.parse(REAL_SINGLE)[0].label
    assert label.startswith("eDP-1")
    assert "BOE" in label and "1706x1066" in label


def test_a_screen_with_nothing_but_a_connector_still_has_a_label() -> None:
    assert outputs.parse({"HDMI-A-1": {}})[0].label == "HDMI-A-1"


def test_a_missing_logical_block_is_not_fatal() -> None:
    found = outputs.parse({"DP-1": {"name": "DP-1", "make": "X"}})
    assert found[0].width == 0
    assert found[0].scale == 1.0


def test_the_connector_key_wins_when_the_name_field_is_broken() -> None:
    found = outputs.parse({"DP-3": {"name": 42, "logical": {"width": 800, "height": 600}}})
    assert found[0].name == "DP-3"


def test_entries_that_are_not_objects_are_dropped() -> None:
    found = outputs.parse({"DP-1": "nonsense", "DP-2": {"name": "DP-2"}})
    assert outputs.names(found) == ("DP-2",)


def test_a_reply_that_is_not_an_object_yields_nothing() -> None:
    documents: tuple[object, ...] = ([], "text", 7, None)
    for document in documents:
        assert outputs.parse(document) == ()


def test_absurd_values_are_clamped_rather_than_trusted() -> None:
    found = outputs.parse(
        {"DP-1": {"name": "DP-1", "logical": {"width": -5, "height": True, "scale": 0}}}
    )
    assert (found[0].width, found[0].height) == (0, 0)
    assert found[0].scale == 1.0


def test_the_number_of_screens_is_bounded() -> None:
    crowd = {f"DP-{n}": {"name": f"DP-{n}"} for n in range(200)}
    assert len(outputs.parse(crowd)) == outputs.MAX_OUTPUTS


# -- talking to niri -------------------------------------------------------


def _stub(monkeypatch: pytest.MonkeyPatch, *, stdout: bytes = b"", code: int = 0) -> None:
    monkeypatch.setattr(outputs, "is_available", lambda: True)

    def run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(_command, code, stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)


def test_discover_reads_what_niri_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout=json.dumps(REAL_SINGLE).encode())
    assert outputs.names(outputs.discover()) == ("eDP-1",)


def test_no_niri_means_one_unnamed_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty is what the app did before this module, and means "everywhere".

    Which is exactly right on a compositor that is not niri.
    """
    monkeypatch.setattr(outputs, "is_available", lambda: False)
    assert outputs.discover() == ()


def test_a_failing_niri_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout=b"", code=1)
    assert outputs.discover() == ()


def test_unparsable_output_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, stdout=b"{not json")
    assert outputs.discover() == ()


def test_a_wedged_compositor_does_not_hang_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outputs, "is_available", lambda: True)

    def hang(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(command, outputs.QUERY_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", hang)
    assert outputs.discover() == ()
