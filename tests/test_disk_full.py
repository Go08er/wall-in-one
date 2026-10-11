"""No room on the disk, or over quota, at each place this version writes.

This checkout is the producer, in a child, under one fault at a time
(`tests.released_faults`): ENOSPC (and in full EDQUOT) at a write before a
byte lands, at a write after half its bytes have landed, and at the changes
and syncs around it (a temporary made, a name linked or renamed into place, a
folder made). Then, with room again, this checkout does what it ordinarily
does next, as the startup probes do.

What must hold: no file at a public name is left short or unreadable (a store
or a record still parses, the media and the still are whole); nothing that was
ever public loses a byte; every path a message names exists; and the next
operations go through with no repair by hand.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_removals as removals
from tests import test_released_leftovers_stills as stills_probe
from tests import test_released_leftovers_stores as stores
from tests import test_released_leftovers_template as templates
from tests import test_released_round_trip as trips
from tests import test_released_round_trip_more as more
from wall_in_one import config, tidy
from wall_in_one.library import stills
from wall_in_one.theme import noctalia

LATER_SECONDS = 25 * 3600

#: A settings save over the settings already there.
SETTINGS_SAVE = r"""
import os
from dataclasses import replace
from pathlib import Path

from wall_in_one import config, file_io

trace_filesystem((file_io, "_rename_noreplace"))


def operation():
    root = Path(os.environ["HOME"]) / "Wallpapers"
    disarm()
    settings = config.Settings(roots=(root,), scan_workshop=False)
    saved = config.save(settings)
    had(saved)
    arm()
    try:
        config.save(replace(settings, scan_workshop=True))
        print("DONE", flush=True)
    except Exception as error:
        print("REFUSED", type(error).__name__, error, flush=True)


serve(operation)
"""


def _whole(entry: faults.Entry) -> str:
    """Why a file at a public name is not whole, or nothing."""
    name = entry.path.name
    try:
        if name.endswith(".json"):
            json.loads(entry.data)
        elif name.endswith(".toml"):
            tomllib.loads(entry.data.decode("utf-8"))
        elif name == "aurora.mp4" and entry.data != removals.MEDIA:
            return "is not the whole media"
        elif name.endswith(".png") and not entry.data.startswith(stills_probe.PNG):
            return "is not a whole image"
    except ValueError as error:
        return f"does not parse ({error})"
    return ""


def _sound(base: Path) -> str:
    """Every file at a public name, as the producer left it, is whole."""
    short = [
        f"{entry.path.relative_to(base)} {why}"
        for entry in faults.tree(base).entries
        if not any(part.startswith(".") for part in entry.path.relative_to(base).parts)
        and entry.size
        and (why := _whole(entry))
    ]
    return f"FINDING published short or unreadable: {short}" if short else "whole"


def _settings_again(base: Path) -> Iterator[tuple[str, str]]:
    try:
        settings = config.load_strict(require_present=True)
        config.save(settings)
        yield "load and save the settings", "saved"
    except (config.ConfigError, OSError) as error:
        yield "load and save the settings", f"FINDING the settings can't be used: {error}"


def _cases() -> dict[str, tuple[str, tuple[str, ...], Callable[[Path], faults.Tree]]]:
    cases = {
        f"store-{store}": (stores.PRODUCER, (store, "valid", "second"), stores._prepare)
        for store in ("playlists", "displays", "schedules", "favourites", "pairings", "ui_prefs")
    }
    return {
        **cases,
        "settings": (SETTINGS_SAVE, (), downloads._prepare),
        "download": (downloads.PRODUCER, (), downloads._prepare),
        "still": (stills_probe.PRODUCER, ("again",), stills_probe._prepare),
        "removal": (trips.REMOVAL, ("remove",), removals._prepare),
        "trash": (trips.REMOVAL, ("trash",), removals._prepare),
        "tidy": (more.TIDY_UP, (), downloads._prepare),
        "template-install": (templates.PRODUCER, ("install",), templates._prepare),
        "template-uninstall": (templates.PRODUCER, ("uninstall",), templates._prepare),
    }


#: What a gate runs; the rest only in full.
GATE = ("store-playlists", "settings", "download", "removal", "template-install")


def _campaign(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, full: bool) -> None:
    script, arguments, prepare = _cases()[case]
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)

    def frame(_video: Path, target: Path, _seek: float, **_keywords: object) -> str:
        Path(target).write_bytes(stills_probe.PNG + b"this version's frame")
        return ""

    monkeypatch.setattr(stills, "_run", frame)
    monkeypatch.setattr(stills, "is_available", lambda: True)

    exempt: Callable[[faults.Entry, faults.Tree], bool]
    exempt = lambda _entry, _before: False  # noqa: E731
    superseded: Callable[[Path, bytes], bool] = lambda _relative, _data: False  # noqa: E731
    if case.startswith("store-"):
        consumer = stores._Consumer(case.removeprefix("store-"))
        then, superseded = consumer.consume, consumer.superseded
    elif case == "settings":
        then = _settings_again
        exempt = lambda entry, _before: entry.path.name == "settings.toml"  # noqa: E731
    elif case in ("download", "tidy"):
        then = trips._opened_a_day_later
        # Known, by design: this version reclaims its own never-public names.
        exempt = lambda entry, _before: trips._is_new_name(  # noqa: E731
            entry.path, trips._profile_of(entry.path)
        )
    elif case == "still":
        then = stills_probe._consume
    elif case in ("removal", "trash"):
        remover = removals._Consumer("remove" if case == "removal" else "trash")
        then = remover.consume

        def exempt(entry: faults.Entry, before: faults.Tree) -> bool:
            # A removal that replay finished takes the download record with
            # it from its own claim folder too, where this version keeps it
            # while a removal is under way (the startup probes' consumer was
            # written for 0.2.2, which has no such claim).
            return remover.exempt(entry, before) or (
                ".wall-in-one-removal-" in str(entry.path)
                and not remover.media(trips._profile_of(entry.path)).exists()
            )
    else:
        then = templates._consume

    def consume(base: Path) -> Iterator[tuple[str, str]]:
        yield "as the full disk left it", _sound(base)
        yield from then(base)

    found = faults.campaign(
        monkeypatch,
        trips.SOURCE,
        script,
        arguments,
        tmp_path,
        prepare=prepare,
        consume=consume,
        superseded=superseded,
        exempt=exempt,
        plans=lambda trace: faults.disk_full(trace, full=full),
        edited_after=lambda _plan: False,
        producer_says=True,
        full=full,
    )
    print("\n" + found.summary(f"no room: {case}"))
    refused = sum(count for said, count in found.outcomes.items() if "REFUSED" in said)
    print(f"steps that refused once there was room again: {refused}")
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


@pytest.mark.parametrize("case", GATE)
def test_a_full_disk_publishes_nothing_short_and_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _campaign(case, tmp_path, monkeypatch, full=False)


@faults.full_campaign
@pytest.mark.parametrize("case", sorted(_cases()))
def test_a_full_disk_or_quota_publishes_nothing_short_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _campaign(case, tmp_path, monkeypatch, full=True)
