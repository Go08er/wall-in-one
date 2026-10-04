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

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_adopted_removal import _case as _adoption_case
from wall_in_one import config, dry_run, runtime_config
from wall_in_one.library import adopted, pairing, playlists, workshop
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


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Session]]:
    made: list[Session] = []

    def make(roots: tuple[Path, ...], steam: Path | None = None) -> Session:
        """A Session over the profile's stores (as the app's), really scanned."""
        if steam is not None:
            monkeypatch.setattr(workshop, "steam_roots", lambda *_a, **_k: (steam,))
        settings = config.Settings(roots=roots, scan_workshop=steam is not None).validated()
        session = Session(settings, owns_playback=False)
        made.append(session)
        session.refresh(mutate_removals=False)
        return session

    yield make
    for session in made:
        session.shutdown()


def _rescan(session: Session, roots: tuple[Path, ...]) -> None:
    session.update_settings(
        replace(session.settings, roots=roots).validated(), rescan_library=False
    )
    session.refresh(mutate_removals=False)


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
        )
    finally:
        fresh.shutdown()


def _remove_for_real(
    session: Session, path: Path, monkeypatch: pytest.MonkeyPatch
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
    assert result.library_after == seen[0].library, "what the application adopts"
    return seen[0], _real_outcome(session.settings)


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


def _tree(sessions: Callable[..., Session], tmp_path: Path) -> tuple[Session, dict[str, Path]]:
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
    def build(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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

    def build(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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


def _q1b(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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


def _chosen_outside(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
    """Allowed: the scan attaches a chosen still wherever it is."""
    a, b = tmp_path / "lib" / "A", tmp_path / "lib" / "B"
    still = _file(a / "forest.png")
    video = _file(b / "rain.mp4")
    session = sessions((a, b))
    session.pairings.choose_still(_video(video), still)
    session.refresh(mutate_removals=False)
    _scheduled(session, "Evening", [video])
    return Scenario(session, replace(session.settings, roots=(b,)), refused=False)


def _alternate(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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


def _r1(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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


def _r2(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
    """Codex R-2: removing the source whose adopted capture another video plays through."""
    case = _adoption_case(tmp_path / "lib", managed=False)
    case.session.shutdown()
    other = _file(case.root / "personal" / "other.mp4")
    # Scanning the folder above, the adoption is not in force: its capture is a picture.
    session = sessions((case.root.parent,))
    assert session.library.find(case.capture) is not None
    session.pairings.choose_still(_video(other), case.capture)
    _rescan(session, (case.root,))
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

    def build(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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


def _hard_link(sessions: Callable[..., Session], tmp_path: Path) -> Scenario:
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
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_dry_run_is_what_the_change_really_leaves(
    name: str,
    sessions: Callable[..., Session],
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
    if scenario.refused:
        # What the guard is for: the change really leaves a profile the compiler refuses,
        # or a used playlist with nothing to play.
        assert real.compile_error or any(
            "would have nothing left to play" in refusal for refusal in verdict.refusals
        )
    else:
        assert real.compiled and not real.compile_error


def test_a_removal_names_no_deleted_file_in_what_it_leaves(
    sessions: Callable[..., Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
def test_a_dry_run_changes_nothing(
    name: str, sessions: Callable[..., Session], tmp_path: Path
) -> None:
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
