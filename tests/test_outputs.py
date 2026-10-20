"""Finding the screens, so a wallpaper can be aimed at one.

The wallpaper service is the source of truth: it reads the compositor's
standard Wayland output protocols and publishes what it found in its status as
``outputs`` (``service/src/outputs.rs``, ``docs/display-discovery.md``). These
tests pin how that snapshot is read back, how GTK's monitors stand in for it in
the window, and that a process with neither gets nothing rather than an error.
The service's own discovery is tested on the Rust side, over the wire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from wall_in_one import paths
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.wallpaper import outputs

#: A status row as the service publishes it, for a 2560x1600 panel at 1.5.
LAPTOP_ROW: dict[str, object] = {
    "name": "eDP-1",
    "enabled": True,
    "make": "BOE",
    "model": "0x0A9B",
    "x": 0,
    "y": 0,
    "width": 1706,
    "height": 1066,
    "scale": 1.5,
    "mode_width": 2560,
    "mode_height": 1600,
    "refresh_millihz": 165000,
    "physical_width_mm": 340,
    "physical_height_mm": 210,
}
MONITOR_ROW: dict[str, object] = {
    **LAPTOP_ROW,
    "name": "DP-2",
    "make": "Dell",
    "model": "U2720Q",
    "x": 1706,
    "width": 2560,
    "height": 1440,
    "scale": 1.0,
    "mode_width": 2560,
    "mode_height": 1440,
    "refresh_millihz": 59951,
}
#: A head wlr-output-management reports as switched off: no mode, no layout.
OFF_ROW: dict[str, object] = {
    "name": "HDMI-A-1",
    "enabled": False,
    "make": "LG",
    "model": "27GL850",
    "x": 0,
    "y": 0,
    "width": 0,
    "height": 0,
    "scale": 1.0,
    "mode_width": 0,
    "mode_height": 0,
    "refresh_millihz": None,
    "physical_width_mm": 600,
    "physical_height_mm": 340,
}


def status(*rows: dict[str, object]) -> dict[str, object]:
    return {"status_version": 2, "displays": [], "outputs": list(rows)}


def test_the_service_snapshot_reads_back_as_outputs() -> None:
    found = outputs.from_status(status(LAPTOP_ROW))

    assert found is not None and len(found) == 1
    laptop = found[0]
    assert laptop.name == "eDP-1"
    assert (laptop.make, laptop.model) == ("BOE", "0x0A9B")
    assert (laptop.width, laptop.height) == (1706, 1066)
    assert laptop.scale == 1.5
    assert (laptop.physical_width, laptop.physical_height) == (2560, 1600)
    assert laptop.refresh_millihz == 165000
    assert laptop.enabled
    assert outputs.unambiguous_refresh_hz(found, "eDP-1") == 165.0


def test_two_screens_come_back_in_a_fixed_order() -> None:
    """Nothing promises the same order twice; a list that reshuffles is worse."""
    forward = outputs.from_status(status(MONITOR_ROW, LAPTOP_ROW))
    backward = outputs.from_status(status(LAPTOP_ROW, MONITOR_ROW))
    assert forward is not None and backward is not None
    assert outputs.names(forward) == outputs.names(backward) == ("DP-2", "eDP-1")


def test_a_switched_off_head_is_listed_labelled_and_never_counted() -> None:
    found = outputs.from_status(status(LAPTOP_ROW, OFF_ROW))
    assert found is not None
    off = next(output for output in found if output.name == "HDMI-A-1")

    assert not off.enabled
    assert off.label == "HDMI-A-1 (LG 27GL850, off)"
    # All outputs share one rate: the switched-off head plays nothing.
    assert outputs.unambiguous_refresh_hz(found) == 165.0


def test_all_outputs_requires_every_refresh_to_be_known_and_equal() -> None:
    same = (
        outputs.Output("DP-1", refresh_millihz=60000),
        outputs.Output("DP-2", refresh_millihz=60000),
    )
    mixed = (same[0], outputs.Output("DP-2", refresh_millihz=165000))
    missing = (same[0], outputs.Output("DP-2"))

    assert outputs.unambiguous_refresh_hz(same) == 60.0
    assert outputs.unambiguous_refresh_hz(mixed) is None
    assert outputs.unambiguous_refresh_hz(missing) is None
    assert outputs.unambiguous_refresh_hz(same, "DP-2") == 60.0
    assert outputs.unambiguous_refresh_hz(same, "HDMI-A-1") is None


def test_implausible_refresh_is_treated_as_unknown() -> None:
    found = outputs.from_status(status({"name": "DP-1", "refresh_millihz": 2_000_000}))
    assert found is not None
    assert found[0].refresh_millihz == 0
    assert outputs.unambiguous_refresh_hz(found) is None


def test_the_label_always_carries_the_connector() -> None:
    """The connector is the name in the config file and in every renderer.

    A settings list that says "BOE 0x0A9B" while the config says `eDP-1` is a
    puzzle rather than a description.
    """
    found = outputs.from_status(status(LAPTOP_ROW))
    assert found is not None
    label = found[0].label
    assert label.startswith("eDP-1")
    assert "BOE" in label and "1706x1066" in label


def test_a_screen_with_nothing_but_a_connector_still_has_a_label() -> None:
    found = outputs.from_status(status({"name": "HDMI-A-1"}))
    assert found is not None
    assert found[0].label == "HDMI-A-1"
    assert found[0].enabled, "a row that does not say otherwise is on"


def test_rows_that_are_not_usable_outputs_are_dropped() -> None:
    rows: list[object] = [
        "nonsense",
        {"name": 42},
        {"name": ""},
        {"name": "Display Port 1"},
        {"name": "DP-1\n--bg"},
        {"name": "x" * (outputs.MAX_NAME_BYTES + 1)},
        {"name": "DP-2"},
    ]
    found = outputs.from_status({"outputs": rows})
    assert found is not None
    assert outputs.names(found) == ("DP-2",)


def test_a_status_without_a_snapshot_is_not_an_empty_one() -> None:
    """An older service has no ``outputs``; one that found nothing yet has ``[]``."""
    documents: tuple[object, ...] = ({}, {"outputs": "eDP-1"}, [], None, "text")
    for document in documents:
        assert outputs.from_status(document) is None
    assert outputs.from_status({"outputs": []}) == ()


def test_absurd_values_are_clamped_rather_than_trusted() -> None:
    found = outputs.from_status(
        status({"name": "DP-1", "width": -5, "height": True, "scale": 0, "mode_width": "2560"})
    )
    assert found is not None
    assert (found[0].width, found[0].height) == (0, 0)
    assert found[0].scale == 1.0
    assert found[0].physical_width == 0


def test_text_the_compositor_sent_is_printable() -> None:
    found = outputs.from_status(status({"name": "DP-1", "make": "Del\tl\n", "model": " U27 "}))
    assert found is not None
    assert (found[0].make, found[0].model) == ("Dell", "U27")


def test_the_number_of_screens_is_bounded() -> None:
    crowd = [{"name": f"DP-{n}"} for n in range(200)]
    found = outputs.from_status({"outputs": crowd})
    assert found is not None
    assert len(found) == outputs.MAX_OUTPUTS


# -- the window's fallback: GTK's monitors -------------------------------------


@dataclass
class Rectangle:
    x: int
    y: int
    width: int
    height: int


class Monitor:
    """Stands in for a `Gdk.Monitor` before GTK 4.14: a whole-number scale."""

    def __init__(
        self,
        connector: str | None,
        geometry: Rectangle,
        *,
        scale_factor: int = 1,
        refresh: int = 60000,
    ) -> None:
        self._connector = connector
        self._geometry = geometry
        self._scale_factor = scale_factor
        self._refresh = refresh

    def get_connector(self) -> str | None:
        return self._connector

    def get_manufacturer(self) -> str:
        return "BOE"

    def get_model(self) -> str:
        return "0x0A9B"

    def get_geometry(self) -> Rectangle:
        return self._geometry

    def get_scale_factor(self) -> int:
        return self._scale_factor

    def get_refresh_rate(self) -> int:
        return self._refresh


class FractionalMonitor(Monitor):
    """GTK 4.14 and later also report the fractional scale."""

    def __init__(self, connector: str, geometry: Rectangle, *, scale: float, **rest: int) -> None:
        super().__init__(connector, geometry, **rest)
        self._scale = scale

    def get_scale(self) -> float:
        return self._scale


def test_gtk_monitors_stand_in_with_their_size_in_pixels() -> None:
    found = outputs.from_monitors(
        [
            FractionalMonitor(
                "eDP-1", Rectangle(0, 0, 1707, 1067), scale=1.5, scale_factor=2, refresh=165004
            )
        ]
    )

    assert len(found) == 1
    laptop = found[0]
    assert laptop.name == "eDP-1"
    assert (laptop.width, laptop.height) == (1707, 1067)
    assert laptop.scale == 1.5, "the fractional scale, not the whole-number one"
    assert (laptop.physical_width, laptop.physical_height) == (2560, 1600)
    assert laptop.refresh_millihz == 165004
    assert (laptop.make, laptop.model) == ("BOE", "0x0A9B")


def test_an_older_gtk_without_fractional_scale_uses_the_whole_number() -> None:
    found = outputs.from_monitors([Monitor("DP-1", Rectangle(0, 0, 1280, 720), scale_factor=2)])
    assert (found[0].physical_width, found[0].physical_height) == (2560, 1440)


def test_a_monitor_gtk_cannot_name_is_left_out() -> None:
    found = outputs.from_monitors(
        [
            Monitor(None, Rectangle(0, 0, 800, 600)),
            object(),
            Monitor("HDMI-A-1", Rectangle(0, 0, 1920, 1080)),
        ]
    )
    assert outputs.names(found) == ("HDMI-A-1",)


def test_a_getter_that_refuses_is_not_fatal() -> None:
    class Refusing(Monitor):
        def get_refresh_rate(self) -> int:
            raise RuntimeError("no mode")

    found = outputs.from_monitors([Refusing("DP-1", Rectangle(0, 0, 800, 600))])
    assert found[0].refresh_millihz == 0


# -- asking the service --------------------------------------------------------


def _answering(
    monkeypatch: pytest.MonkeyPatch, answer: Response | Exception
) -> list[tuple[str, float | None]]:
    asked: list[tuple[str, float | None]] = []

    def send_runtime(verb: str, argument: str | None = None, **keywords: object) -> Response:
        timeout = keywords.get("timeout")
        asked.append((verb, timeout if isinstance(timeout, float) else None))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client, "send_runtime", send_runtime)
    return asked


GTK_VIEW = (outputs.Output("eDP-1", width=1707, height=1067, physical_width=2560),)


def test_the_service_snapshot_wins_over_the_window_view(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _answering(
        monkeypatch, Response.success(json.dumps(status(MONITOR_ROW, LAPTOP_ROW, OFF_ROW)))
    )

    found = outputs.discover(GTK_VIEW)

    assert outputs.names(found) == ("DP-2", "HDMI-A-1", "eDP-1")
    assert asked == [("status", outputs.QUERY_TIMEOUT)], "one bounded status request"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(client.NotRunningError("no instance listening"), id="no-service"),
        pytest.param(client.ControlTimeoutError("timed out after 5s"), id="wedged-service"),
        pytest.param(client.ControlError("connection reset"), id="broken-connection"),
        pytest.param(Response.failure("busy", kind="unavailable"), id="refused"),
        pytest.param(Response.success("{not json"), id="garbled"),
        pytest.param(Response.success(json.dumps({"status_version": 2})), id="older-service"),
        pytest.param(Response.success(json.dumps(status())), id="nothing-found-yet"),
    ],
)
def test_without_a_service_snapshot_the_window_view_is_used(
    monkeypatch: pytest.MonkeyPatch, answer: Response | Exception
) -> None:
    _answering(monkeypatch, answer)
    assert outputs.discover(GTK_VIEW) == GTK_VIEW
    assert outputs.discover() == (), "a process without a window gets nothing"


def test_a_headless_path_with_no_service_running_gets_nothing() -> None:
    """The real client, the isolated runtime directory, no service: no error."""
    assert not Path(paths.runtime_socket_path()).exists()
    assert outputs.ask_service() is None
    assert outputs.discover() == ()


def test_the_docs_say_every_head_switched_off_keeps_the_last_snapshot() -> None:
    """A switched-off head is like an unplugged one only while another head is on (R40-5).

    With every head switched off the service keeps its last successful snapshot
    and reports a discovery error, as control-socket.md's
    ``output_discovery_error`` already said; each place that describes a
    switched-off head says so too.
    """
    docs = Path(__file__).resolve().parents[1] / "docs"
    for name, claim in (
        ("display-discovery.md", "never become a renderer target"),
        ("display-discovery.md", "is treated exactly like an unplugged one"),
        ("runtime-config.md", "is never a target"),
        ("control-socket.md", "is treated exactly like an unplugged one"),
    ):
        text = " ".join((docs / name).read_text(encoding="utf-8").split())
        assert text.count(claim) == 1, (name, claim)
        after = text.split(claim, 1)[1][:600]
        assert "every head switched off" in after, (name, claim)
        if claim != "never become a renderer target":
            assert "output_discovery_error" in after, (name, claim)
