"""The dry run is the guarantee, held to what really happens.

Codex 0.3.0 round 3 (R-1, R-2, R-3) showed that predicting from memory what
the scanner will find doesn't converge. A change that can take away what a
playlist plays is now made for pretend first, on the authoring worker: the
real scan of what it leaves, the real cleanup on copies of the stores, and
the real compiler (`wall_in_one.dry_run`). These tests hold that mechanism,
not a prediction, to the real outcome: for every fixture of rounds 2 and 3,
what the dry run says the change leaves (the library, the pairings,
playlists and favourites, and what the compiler builds or why it can't) is
exactly what the change, really made, leaves, as a fresh real scan finds it.
A removal is really run (`RemovalPlan.run`) from the inputs its dry run
had; a source change is really scanned (`Session.prepare_scan`).

That the application refuses Codex's scenarios, and allows the ones that
must stay allowed, is in ``test_ui_codex030_round3``; those fail on 7d5665c.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_adopted_removal import _case as _adoption_case
from tests.whole_document import not_in_the_document
from wall_in_one import config, dry_run, runtime_config
from wall_in_one.library import adopted, pairing, playlists, scan, stills, workshop
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.pairings import Pairing
from wall_in_one.session import Session


def _file(path: Path, body: bytes = b"placeholder bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _capture(root: Path, name: str) -> Path:
    return _file(pairing.still_directory(root) / f"{name}.png")


def _scene(steam: Path, identifier: str) -> Path:
    """An installed Wallpaper Engine scene, as Steam lays one out."""
    directory = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / identifier
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Field", "file": "scene.json"}), encoding="utf-8"
    )
    (directory / "scene.pkg").write_bytes(b"\x00" * 16)
    return directory


def _video(path: Path) -> MediaItem:
    return MediaItem(path, Kind.VIDEO, 1, 1)


# -- the application's own state, over the profile's files -----------------------------------


class _Patcher:
    """`monkeypatch.setattr` for a child process, which simply exits after."""

    def setattr(self, target: object, name: str, value: object) -> None:
        setattr(target, name, value)


class Harness:
    """The application's state over the profile's files: a Session, really scanned.

    `test_ui_codex030_round4` runs the same scenarios through the real
    application with its own harness.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch | _Patcher) -> None:
        self.monkeypatch = monkeypatch
        self.made: list[Session] = []

    def __call__(self, roots: tuple[Path, ...], steam: Path | None = None) -> Session:
        if steam is not None:
            self.monkeypatch.setattr(workshop, "steam_roots", lambda *_a, **_k: (steam,))
        settings = config.Settings(roots=roots, scan_workshop=steam is not None).validated()
        session = Session(settings, owns_playback=False)
        self.made.append(session)
        session.refresh(mutate_removals=False)
        return session

    def rescan(self, session: Session, roots: tuple[Path, ...]) -> None:
        """The library folders changed to ``roots``, and scanned."""
        session.update_settings(
            replace(session.settings, roots=roots).validated(), rescan_library=False
        )
        session.refresh(mutate_removals=False)

    def close(self) -> None:
        for session in self.made:
            session.shutdown()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    harness = Harness(monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


def _scheduled(session: Session, name: str, sources: list[Path]) -> playlists.Playlist:
    made = session.playlists.create(name)
    for source in sources:
        session.playlists.add(made.id, source)
    session.schedules.add(made.id)
    return session.playlists.find(made.id)


@dataclass(frozen=True, slots=True)
class Outcome:
    """Everything a change leaves, in the dry run's terms."""

    library: Library
    records: dict[str, Pairing]
    playlists: tuple[playlists.Playlist, ...]
    favourites: frozenset[Path]
    compiled: str
    compile_error: str
    #: Of the real outcome only: what the compiler built that document
    #: without (`tests.whole_document`). The dry run's own word for it is its
    #: refusals, so this takes no part in comparing the two.
    built_without: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def of(cls, verdict: dry_run.Verdict) -> Outcome:
        return cls(
            verdict.library,
            dict(verdict.records_after),
            verdict.playlists_after,
            verdict.favourites_after,
            verdict.compiled,
            verdict.compile_error,
        )


def _real_outcome(settings: config.Settings) -> Outcome:
    """What the profile on disk is now, as the application starting afresh finds it."""
    fresh = Session(settings, owns_playback=False)
    try:
        library = fresh.prepare_scan().run()
        fresh.adopt_library(library, reconcile_workshop=False)
        try:
            compiled, error = runtime_config.render(settings, fresh), ""
        except runtime_config.RuntimeConfigError as failure:
            compiled, error = "", str(failure)
        return Outcome(
            library,
            dict(fresh.pairings.records),
            fresh.playlists.all(),
            fresh.favourites.paths,
            compiled,
            error,
            tuple(not_in_the_document(settings, fresh)) if compiled else (),
        )
    finally:
        fresh.shutdown()


def _remove_for_real(
    session: Session, path: Path, monkeypatch: pytest.MonkeyPatch | _Patcher
) -> tuple[dry_run.Verdict, Outcome]:
    """Remove ``path`` through the real removal, refused or not; its dry run, and the outcome.

    The removal's own dry run is recorded and then let through, so the real
    thing goes ahead from exactly the inputs its dry run had.
    """
    item = session.library.find(path)
    assert item is not None, path
    seen: list[dry_run.Verdict] = []
    real = dry_run.removal

    def recorded(*arguments: Any, **keywords: Any) -> dry_run.Verdict:
        verdict = real(*arguments, **keywords)
        seen.append(verdict)
        return replace(verdict, refusals=())

    monkeypatch.setattr(dry_run, "removal", recorded)
    result = session.prepare_removal_plan(item, trash=True).run()
    assert result.committed, result.error_message
    assert not result.error_message and not result.cleanup_failures
    assert len(seen) == 1
    outcome = _real_outcome(session.settings)
    # What the application installs: the removal's own scan, after the commit.
    assert result.rescanned == outcome.library
    return seen[0], outcome


def _change_sources_for_real(
    session: Session, after: config.Settings
) -> tuple[dry_run.Verdict, Outcome]:
    verdict = dry_run.sources(
        session.settings,
        after,
        library=session.library,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        favourite_store=session.favourites,
        uses=dry_run.Uses.of(session),
    )
    return verdict, _real_outcome(after)


# -- the fixtures ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scenario:
    session: Session
    #: A path to remove, or the settings to save.
    change: Path | config.Settings
    #: Whether the change must be refused, and words the refusal must hold.
    refused: bool
    words: tuple[str, ...] = ()


def _tree(sessions: Harness, tmp_path: Path) -> tuple[Session, dict[str, Path]]:
    """Round 2's: two folders, a Steam library, and every way a wallpaper gets its still."""
    a, b, steam = tmp_path / "lib" / "A", tmp_path / "lib" / "B", tmp_path / "steam"
    paths = {
        "forest": _file(a / "forest.png"),
        "v1": _file(b / "v1.mp4"),  # chosen still in the other folder
        "v2": _file(a / "v2.mp4"),  # sibling
        "v2-still": _file(a / "v2-still.png"),
        "v3": _file(b / "v3.mp4"),  # capture under A only
        "v4": _file(b / "v4.mp4"),  # captures under both: the first root's wins
        "v5": _file(b / "v5.mp4"),  # sidecar naming a picture under A
        "side": _file(a / "side.png"),
        "v6": _file(b / "v6.mp4"),  # chosen still missing: the capture under B instead
        "v7": _file(b / "v7.mp4"),  # nothing at all
        "dawn": _file(b / "dawn.png"),
    }
    _capture(a, pairing.automatic_still_stem(paths["v3"]))
    _capture(a, pairing.automatic_still_stem(paths["v4"]))
    _capture(b, pairing.automatic_still_stem(paths["v4"]))
    _capture(b, pairing.automatic_still_stem(paths["v6"]))
    paths["v5"].with_name(paths["v5"].name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({pairing.SIDECAR_STILL_KEY: str(paths["side"])}), encoding="utf-8"
    )
    paths["scene"] = _scene(steam, "123")
    _capture(a, "123")  # the scene's capture, under A
    session = sessions((a, b), steam)
    session.pairings.choose_still(_video(paths["v1"]), paths["forest"])
    session.pairings.choose_still(_video(paths["v6"]), tmp_path / "gone.png")
    session.refresh(mutate_removals=False)
    moving = [item.path for item in session.library.items if item.is_moving]
    assert len(moving) == 8, "seven videos (v7 with no still at all) and the scene"
    _scheduled(session, "Everything", moving)
    one = session.playlists.create("Forest")
    session.playlists.add(one.id, paths["v1"])
    session.favourites.add(paths["forest"])
    return session, paths


def _tree_change(change: str) -> Callable[..., Scenario]:
    def build(sessions: Harness, tmp_path: Path) -> Scenario:
        session, paths = _tree(sessions, tmp_path)
        a, b = session.settings.roots
        if change == "drop-workshop":
            return Scenario(session, replace(session.settings, scan_workshop=False), refused=False)
        # Unused, "Forest" holds only v1: it may not be left holding nothing that plays.
        forest = "\u201cForest\u201d would hold only wallpapers playback can\u2019t use: v1.mp4 "
        if change.startswith("roots:"):
            roots = {"roots:BA": (b, a), "roots:B": (b,), "roots:A": (a,)}[change]
            leaves_v1 = change == "roots:A"
            return Scenario(
                session,
                replace(session.settings, roots=roots),
                refused=leaves_v1,
                words=(forest + "would no longer be in the library",) if leaves_v1 else (),
            )
        removes_its_still = change == "forest"
        return Scenario(
            session,
            paths[change],
            refused=removes_its_still,
            words=(forest + "plays through forest.png, which this removes",)
            if removes_its_still
            else (),
        )

    return build


def _q1a(sibling: bool) -> Callable[..., Scenario]:
    """Round 2, Q-1 A: deleting the image a video plays through."""

    def build(sessions: Harness, tmp_path: Path) -> Scenario:
        root = tmp_path / "lib" / "A"
        video = _file(root / "rain.mp4")
        still = _file(root / "forest.png")
        if sibling:
            _file(root / "rain-still.png")
        session = sessions((root,))
        session.pairings.choose_still(_video(video), still)
        session.refresh(mutate_removals=False)
        _scheduled(session, "Evening", [video])
        return Scenario(
            session,
            still,
            refused=not sibling,
            words=()
            if sibling
            else (
                "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play: "
                "rain.mp4 plays through forest.png, which this removes",
            ),
        )

    return build


def _q1b(sessions: Harness, tmp_path: Path) -> Scenario:
    """Round 2, Q-1 B: scene C stays in the Workshop; its capture is under folder A."""
    a, b, steam = tmp_path / "lib" / "A", tmp_path / "lib" / "B", tmp_path / "steam"
    _file(b / "dawn.png")
    _capture(a, "123")
    scene = _scene(steam, "123")
    session = sessions((a, b), steam)
    _scheduled(session, "Scenes", [scene])
    return Scenario(
        session,
        replace(session.settings, roots=(b,)),
        refused=True,
        words=(
            "\u201cScenes\u201d (used by 1 schedule rule) would have nothing left to play: "
            "123 plays through 123.png, which would no longer be its still",
        ),
    )


def _chosen_outside(sessions: Harness, tmp_path: Path) -> Scenario:
    """Allowed: the scan attaches a chosen still wherever it is."""
    a, b = tmp_path / "lib" / "A", tmp_path / "lib" / "B"
    still = _file(a / "forest.png")
    video = _file(b / "rain.mp4")
    session = sessions((a, b))
    session.pairings.choose_still(_video(video), still)
    session.refresh(mutate_removals=False)
    _scheduled(session, "Evening", [video])
    return Scenario(session, replace(session.settings, roots=(b,)), refused=False)


def _alternate(sessions: Harness, tmp_path: Path) -> Scenario:
    """Allowed: the playlist has another wallpaper to play."""
    root = tmp_path / "lib" / "A"
    video = _file(root / "rain.mp4")
    still = _file(root / "forest.png")
    dawn = _file(root / "dawn.png")
    session = sessions((root,))
    session.pairings.choose_still(_video(video), still)
    session.refresh(mutate_removals=False)
    _scheduled(session, "Evening", [video, dawn])
    return Scenario(session, still, refused=False)


def _r1(sessions: Harness, tmp_path: Path) -> Scenario:
    """Codex R-1: the adoption's own folder replaced by the folder above it."""
    case = _adoption_case(tmp_path / "lib", managed=False)
    case.session.shutdown()
    session = sessions((case.root,))
    found = session.library.find(case.source)
    assert found is not None and found.paired_still == case.capture, "the adoption is in force"
    _scheduled(session, "Adopted", [case.source])
    return Scenario(
        session,
        replace(session.settings, roots=(case.root.parent,)),
        refused=True,
        words=(
            "\u201cAdopted\u201d (used by 1 schedule rule) would have nothing left to play: "
            "clip.mp4 plays through clip.png, which would no longer be its still",
        ),
    )


def _r2(sessions: Harness, tmp_path: Path) -> Scenario:
    """Codex R-2: removing the source whose adopted capture another video plays through."""
    case = _adoption_case(tmp_path / "lib", managed=False)
    case.session.shutdown()
    other = _file(case.root / "personal" / "other.mp4")
    # Scanning the folder above, the adoption is not in force: its capture is a picture.
    session = sessions((case.root.parent,))
    assert session.library.find(case.capture) is not None
    session.pairings.choose_still(_video(other), case.capture)
    sessions.rescan(session, (case.root,))
    assert session.library.adopted_stills == ((case.source, case.capture),)
    kept = session.library.find(other)
    assert kept is not None and kept.paired_still == case.capture
    _scheduled(session, "Other", [other])
    return Scenario(
        session,
        case.source,
        refused=True,
        words=(
            "\u201cOther\u201d (used by 1 schedule rule) would have nothing left to play: "
            "other.mp4 plays through clip.png, which this removes",
        ),
    )


def _r3(alternate: bool) -> Callable[..., Scenario]:
    """Codex R-3: the real file removed, its alias through a linked folder chosen."""

    def build(sessions: Harness, tmp_path: Path) -> Scenario:
        walls = tmp_path / "lib" / "real" / "walls"
        rain = _file(walls / "rain.mp4")
        forest = _file(walls / "forest.png")
        dawn = _file(walls / "dawn.png")
        via = tmp_path / "lib" / "via"
        via.symlink_to(tmp_path / "lib" / "real", target_is_directory=True)
        alias = via / "walls" / "forest.png"
        session = sessions((walls, via / "walls"))
        assert session.library.find(alias) is not None, "both spellings are in the library"
        session.pairings.choose_still(_video(rain), alias)
        session.refresh(mutate_removals=False)
        _scheduled(session, "Alias", [rain, dawn] if alternate else [rain])
        return Scenario(
            session,
            forest,
            refused=not alternate,
            words=()
            if alternate
            else (
                "\u201cAlias\u201d (used by 1 schedule rule) would have nothing left to play: "
                "rain.mp4 plays through forest.png, which this removes",
            ),
        )

    return build


def _hard_link(sessions: Harness, tmp_path: Path) -> Scenario:
    """A file's other hard link stays when one name goes; a symlink to that name goes."""
    root = tmp_path / "lib" / "A"
    a = _file(root / "a.png")
    b = root / "sub" / "b.png"
    b.parent.mkdir()
    os.link(a, b)
    (root / "link.png").symlink_to(a)
    video = _file(root / "rain.mp4")
    session = sessions((root,))
    session.pairings.choose_still(_video(video), b)
    session.refresh(mutate_removals=False)
    _scheduled(session, "Linked", [video])
    return Scenario(session, a, refused=False)


def _s2(sessions: Harness, tmp_path: Path) -> Scenario:
    """Codex S-2: a library folder that is a Workshop item's own folder, Workshop scanning on.

    The root walk finds the video first, as the user's own; Workshop
    discovery finds the same path again, and must not bring it back.
    """
    steam = tmp_path / "steam"
    installed = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / "123"
    installed.mkdir(parents=True)
    (installed / "project.json").write_text(
        json.dumps({"type": "video", "file": "clip.mp4", "title": "Clip"}), encoding="utf-8"
    )
    clip = _file(installed / "clip.mp4")
    _file(installed / "clip-still.png")
    dawn = _file(tmp_path / "lib" / "A" / "dawn.png")
    session = sessions((installed, dawn.parent), steam)
    found = session.library.find(clip)
    assert found is not None and found.provider == "local", "the root walk wins"
    _scheduled(session, "Evening", [clip, dawn])
    return Scenario(session, clip, refused=False)


def _s4(sessions: Harness, tmp_path: Path) -> Scenario:
    """Codex S-4: what the removal deletes doesn't count against the walk's budget."""
    root = tmp_path / "lib" / "A"
    a, b, _c = (_file(root / name) for name in ("a.png", "b.png", "c.png"))
    sessions.monkeypatch.setattr(scan, "MAX_ENTRIES_EXAMINED", 3)
    session = sessions((root,))
    assert {item.path for item in session.library.items} == {a, b}, "c is past the budget"
    _scheduled(session, "Evening", [b])
    return Scenario(session, a, refused=False)


SCENARIOS: dict[str, Callable[..., Scenario]] = {
    **{
        f"tree-{change}": _tree_change(change)
        for change in (
            "roots:BA",
            "roots:B",
            "roots:A",
            "drop-workshop",
            "forest",
            "side",
            "v2-still",
            "v4",
            "v1",
            "dawn",
        )
    },
    "q1a-only-still": _q1a(sibling=False),
    "q1a-with-sibling": _q1a(sibling=True),
    "q1b-scene-capture-folder": _q1b,
    "chosen-still-outside-the-roots": _chosen_outside,
    "alternate-wallpaper": _alternate,
    "r1-adoption-root-to-parent": _r1,
    "r2-adopted-capture": _r2,
    "r3-linked-ancestor-alias": _r3(alternate=False),
    "r3-alias-with-an-alternate": _r3(alternate=True),
    "hard-link": _hard_link,
    "s2-workshop-overlap": _s2,
    "s4-walk-budget": _s4,
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_dry_run_is_what_the_change_really_leaves(
    name: str,
    sessions: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = SCENARIOS[name](sessions, tmp_path)
    session = scenario.session
    if isinstance(scenario.change, Path):
        verdict, real = _remove_for_real(session, scenario.change, monkeypatch)
        assert not scenario.change.exists()
    else:
        verdict, real = _change_sources_for_real(session, scenario.change)

    assert Outcome.of(verdict) == real
    assert bool(verdict.refusals) == scenario.refused, verdict.refusals
    for words in scenario.words:
        assert any(words in refusal for refusal in verdict.refusals), verdict.refusals
    # The compiler builds either way now, without what can't play: a change
    # the dry run refuses is no longer one that would have stopped publication.
    assert real.compiled and not real.compile_error
    if scenario.refused:
        # What the guard is for: the change really leaves a document built without
        # something (a rule left out, a playlist none of whose entries resolves),
        # or a used playlist with nothing to play.
        assert real.built_without or any(
            "would have nothing left to play" in refusal for refusal in verdict.refusals
        )
    else:
        assert not real.built_without, real.built_without


def test_a_removal_names_no_deleted_file_in_what_it_leaves(
    sessions: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-3's publication: allowed, and no spelling of the deleted still is left anywhere."""
    scenario = _r3(alternate=True)(sessions, tmp_path)
    assert isinstance(scenario.change, Path)
    forest = scenario.change
    alias = tmp_path / "lib" / "via" / "walls" / "forest.png"
    verdict, _real = _remove_for_real(scenario.session, forest, monkeypatch)
    for item in verdict.library.items:
        assert item.path not in {forest, alias}
        assert item.paired_still not in {forest, alias}
    assert str(forest) not in verdict.compiled
    assert str(alias) not in verdict.compiled


# -- the source's locks, from the dry run through the commit ---------------------------------

#: What a person reads when a removal can't take its file's lifecycle locks in time.
BUSY = "it is being downloaded or captured right now; try again in a moment"


def _held_elsewhere(item: MediaItem) -> bool:
    """Whether another thread finds ``item``'s source lifecycle lock taken right now."""
    found: list[bool] = []

    def probe() -> None:
        try:
            with stills.source_lifecycle_lock(item):
                found.append(False)
        except stills.LifecycleBusy:
            found.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return found == [True]


def test_a_removal_is_checked_and_committed_under_its_sources_locks(
    sessions: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex S-1's boundary: nothing can publish for the source between its dry run and commit."""
    scenario = _alternate(sessions, tmp_path)
    assert isinstance(scenario.change, Path)
    item = scenario.session.library.find(scenario.change)
    assert item is not None
    monkeypatch.setattr(stills, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)
    during: list[bool] = []
    check = dry_run.removal

    def checked(*arguments: Any, **keywords: Any) -> dry_run.Verdict:
        during.append(_held_elsewhere(item))
        return check(*arguments, **keywords)

    monkeypatch.setattr(dry_run, "removal", checked)
    result = scenario.session.prepare_removal_plan(item, trash=True).run()

    assert result.committed, result.error_message
    assert during == [True], "held while it was checked"
    assert not _held_elsewhere(item), "and let go once committed"


def test_a_removal_that_cant_take_its_locks_says_so_without_naming_them(
    sessions: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A capture or download holding the file: refused in words; the lock's path is logged."""
    scenario = _alternate(sessions, tmp_path)
    assert isinstance(scenario.change, Path)
    item = scenario.session.library.find(scenario.change)
    assert item is not None
    monkeypatch.setattr(stills, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with stills.source_lifecycle_lock(item):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(5)
        with caplog.at_level(logging.WARNING, logger=stills.__name__):
            result = scenario.session.prepare_removal_plan(item, trash=True).run()
    finally:
        release.set()
        holder.join()

    assert not result.committed and scenario.change.exists()
    assert result.error_kind == "busy"
    assert result.error_message == f"forest was not removed: {BUSY}."
    assert ".lock" not in result.error_message
    assert ".lock" in caplog.text, "the lock's path is in the log"


# -- the exclusion ---------------------------------------------------------------------------


def test_the_exclusion_is_by_identity_whatever_the_spelling(tmp_path: Path) -> None:
    real = _file(tmp_path / "real" / "walls" / "forest.png")
    (tmp_path / "via").symlink_to(tmp_path / "real", target_is_directory=True)
    alias = tmp_path / "via" / "walls" / "forest.png"
    link = tmp_path / "link.png"
    link.symlink_to(real)
    twin = tmp_path / "twin.png"
    os.link(real, twin)
    other = _file(tmp_path / "other.png")

    gone = pairing.Exclusion.of([real])
    assert gone.removes(real) and gone.removes(alias) and gone.removes(link)
    assert not gone.removes(twin), "a hard link's other name stays"
    assert not gone.removes(other)
    assert not pairing.Exclusion.of([link]).identities, "a symlink is not deleted as a file"
    assert not pairing.Exclusion.of([tmp_path / "real"]).identities, "nor is a folder"
    with pairing.excluding(gone):
        assert not pairing.present(alias) and pairing.present(twin)
    assert pairing.present(alias), "only inside the dry run"


def test_a_reserved_entry_is_gone_whatever_comes_to_occupy_it(tmp_path: Path) -> None:
    """Codex S-1: a capture the removal reserves counts as deleted before it exists."""
    video = _file(tmp_path / "lib" / "A" / "rain.mp4")
    capture = pairing.still_directory(tmp_path / "lib" / "A") / "rain-capture.png"
    capture.parent.mkdir(parents=True)
    gone = pairing.Exclusion.of([video, capture])
    assert gone, "the capture's directory is there, so its entry is known"
    _file(capture)  # published after the snapshot
    assert gone.removes(capture)
    with pairing.excluding(gone):
        assert not pairing.present(capture)
    assert not gone.removes(_file(capture.with_name("another.png")))


#: Run in a child: a fresh user and mount namespace, so a bind mount is private to it and
#: goes with it. Prints ``SKIP: ...`` where namespaces aren't allowed (a build sandbox).
_IN_A_NAMESPACE = """
import os, sys
uid, gid = os.getuid(), os.getgid()
try:
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    with open("/proc/self/setgroups", "w") as handle:
        handle.write("deny")
    with open("/proc/self/uid_map", "w") as handle:
        handle.write(f"0 {uid} 1")
    with open("/proc/self/gid_map", "w") as handle:
        handle.write(f"0 {gid} 1")
except OSError as error:
    print(f"SKIP: no user and mount namespace here: {error}")
    sys.exit(0)
from tests.test_dry_run import _bind_alias_child
_bind_alias_child()
"""


def _bind_mount(source: Path, target: Path) -> None:
    """``mount --bind source target``, with ``/`` made private first (nothing propagates out)."""
    libc = ctypes.CDLL(None, use_errno=True)
    ms_bind, ms_rec, ms_private = 4096, 16384, 1 << 18
    if libc.mount(None, b"/", None, ms_rec | ms_private, None) != 0:
        raise OSError(ctypes.get_errno(), "could not make / private")
    if libc.mount(os.fsencode(source), os.fsencode(target), None, ms_bind, None) != 0:
        raise OSError(ctypes.get_errno(), f"could not bind {source} at {target}")


def _bind_alias_child() -> None:
    """Codex S-3, inside the namespace: R bind-mounted at B, both roots; rain plays B/forest.png.

    Prints one JSON object: what was found at each stage, or why it stopped.
    """
    base = Path(os.environ["WIO_BIND_BASE"])
    real, mounted = base / "lib" / "R", base / "lib" / "B"
    rain = _file(real / "rain.mp4")
    forest = _file(real / "forest.png")
    mounted.mkdir(parents=True)
    if os.environ["WIO_BIND_HARD_LINK"] == "1":
        os.link(forest, base / "elsewhere.png")  # a surviving name, outside the roots
    found: dict[str, object] = {}
    try:
        _bind_mount(real, mounted)
    except OSError as error:
        print(f"SKIP: no bind mount here: {error}")
        return
    alias = mounted / "forest.png"
    harness = Harness(_Patcher())
    try:
        here, there = real.stat(), mounted.stat()
        found["same_directory"] = (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino)
        found["links"] = forest.stat().st_nlink
        found["excluded"] = pairing.Exclusion.of([forest]).removes(alias)
        session = harness((real, mounted))
        found["indexed"] = session.library.find(alias) is not None
        session.pairings.choose_still(_video(rain), alias)
        session.refresh(mutate_removals=False)
        _scheduled(session, "Bind alias", [rain])
        item = session.library.find(forest)
        assert item is not None
        verdict = dry_run.removal(
            item,
            settings=session.settings,
            library=session.library,
            roots=session.library.roots,
            adopted_authority=None,
            favourite_store=session.favourites,
            pairing_store=session.pairings,
            playlist_store=session.playlists,
            uses=dry_run.Uses.of(session),
        )
        found["refusals"] = list(verdict.refusals)
        seen, real_outcome = _remove_for_real(session, forest, _Patcher())
        found["conforms"] = Outcome.of(seen) == real_outcome
        found["alias_gone"] = not alias.exists()
        found["published_missing"] = _missing(real_outcome.compiled)
    except Exception as error:
        found["error"] = f"{type(error).__name__}: {error}"
    finally:
        harness.close()
    print(json.dumps(found))


def _missing(document: str) -> list[str]:
    """Every ``still`` and ``motion`` path ``document`` (runtime.toml) names that isn't there."""
    named: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in {"still", "motion"} and isinstance(inner, str):
                    named.append(inner)
                else:
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(tomllib.loads(document) if document else {})
    return [path for path in named if not Path(path).exists()]


@pytest.mark.parametrize("hard_link", [False, True], ids=["one-link", "another-hard-link"])
def test_a_bind_mounted_alias_of_a_deleted_entry_is_gone_too(
    hard_link: bool, tmp_path: Path
) -> None:
    """Codex S-3: the entry B/forest.png reaches is R's, whatever else links to the file.

    With one link, identity alone already covers it (the control); with
    another hard link elsewhere, only the entry tells the alias from the
    surviving name. Refused either way, and what the removal really leaves
    is what the dry run said.
    """
    repository = Path(__file__).resolve().parents[1]
    environment = dict(
        os.environ,
        WIO_BIND_BASE=str(tmp_path),
        WIO_BIND_HARD_LINK="1" if hard_link else "0",
        PYTHONPATH=os.pathsep.join([str(repository), *sys.path]),
    )
    completed = subprocess.run(
        [sys.executable, "-c", _IN_A_NAMESPACE],
        env=environment,
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = completed.stdout.strip().splitlines()
    assert completed.returncode == 0 and output, completed.stderr
    if output[-1].startswith("SKIP: "):
        pytest.skip(output[-1].removeprefix("SKIP: "))
    found = json.loads(output[-1])
    assert "error" not in found or "refusals" in found, found
    assert found["same_directory"], "a bind-mounted directory is the same directory"
    assert found["links"] == (2 if hard_link else 1)
    assert found["indexed"], "both spellings are in the library"
    assert found["excluded"], "the alias reaches the deleted entry"
    assert found["refusals"] == [
        "\u201cBind alias\u201d (used by 1 schedule rule) would have nothing left to play: "
        "rain.mp4 plays through forest.png, which this removes"
    ]
    assert "error" not in found, found["error"]
    assert found["conforms"], "the dry run is what the removal really leaves"
    assert found["alias_gone"]
    assert found["published_missing"] == []


# -- no side effects -------------------------------------------------------------------------


def _snapshot(*tops: Path) -> dict[str, tuple[int, int, str]]:
    """Every entry under ``tops``: mode, size and content (or link target), and mtime."""
    seen: dict[str, tuple[int, int, str]] = {}
    for top in tops:
        for directory, folders, files in os.walk(top):
            for name in (*folders, *files):
                path = Path(directory) / name
                status = path.lstat()
                if stat.S_ISLNK(status.st_mode):
                    body = os.readlink(path)
                elif stat.S_ISREG(status.st_mode):
                    body = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    body = ""
                seen[str(path)] = (status.st_mode, status.st_mtime_ns, body)
    return seen


@pytest.mark.parametrize("name", ["r2-adopted-capture", "r3-linked-ancestor-alias", "tree-forest"])
def test_a_dry_run_changes_nothing(name: str, sessions: Harness, tmp_path: Path) -> None:
    """Byte for byte: the profile, the library folders and the Steam library."""
    scenario = SCENARIOS[name](sessions, tmp_path)
    session = scenario.session
    assert isinstance(scenario.change, Path)
    item = session.library.find(scenario.change)
    assert item is not None
    plan = session.prepare_removal_plan(item, trash=True)
    authority = (
        adopted.authority_for(item.path, capture=plan.adopted_capture, verify_contents=True)
        if plan.adopted_capture is not None
        else None
    )
    assert (authority is not None) == (name == "r2-adopted-capture")
    before = _snapshot(tmp_path)
    records = dict(session.pairings.records)
    entries = session.playlists.all()

    verdict = dry_run.removal(
        item,
        settings=session.settings,
        library=session.library,
        roots=plan.roots,
        adopted_authority=authority,
        favourite_store=session.favourites,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        uses=dry_run.Uses.of(session),
    )
    assert verdict.refusals, "each of these is refused"
    sources = dry_run.sources(
        session.settings,
        replace(session.settings, roots=session.settings.roots[:1]),
        library=session.library,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        favourite_store=session.favourites,
        uses=dry_run.Uses.of(session),
    )
    assert sources.library.roots == session.settings.roots[:1]

    assert _snapshot(tmp_path) == before
    assert dict(session.pairings.records) == records, "nor the stores it was given"
    assert session.playlists.all() == entries
