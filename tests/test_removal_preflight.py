"""Delete and Trash refuse up front when their metadata cleanup cannot run.

After the file is moved or unlinked, cleanup removes the wallpaper from the
favourites, pairings and playlists. A store this build cannot change -- saved
by a newer version, or unreadable -- used to be discovered only then: the
file was already gone, its records stayed, a pending removal could not
finish, and the message ended with the store's "Nothing was changed." Now the
removal is refused before the journal intent and before the file is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from tests.test_control import _commands, _downloaded, _immediate, _on_disk
from tests.test_session import FakeRenderer, _download_authority
from wall_in_one import config, paths
from wall_in_one.library import favourites, manage, pairings, playlists, removals, state_file
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.session import RemovalResult, Session
from wall_in_one.wallpaper.applier import Applier

STORES: dict[str, ModuleType] = {
    "pairings": pairings,
    "playlists": playlists,
    "favourites": favourites,
}
ORIGINAL = b"the wallpaper itself"


class _Profile:
    """One removable wallpaper, its stores at their own paths, and a Session."""

    def __init__(self, tmp_path: Path, *, trash: bool) -> None:
        self.trash = trash
        self.root = tmp_path / "wallpapers"
        self.root.mkdir()
        self.state = tmp_path / "state-files"
        self.state.mkdir()
        self.source = self.root / "paper.png"
        self.source.write_bytes(ORIGINAL)
        if trash:
            self.item = MediaItem(
                self.source, Kind.STILL, len(ORIGINAL), 1, ownership=Ownership.USER
            )
        else:
            # A managed download, the kind Delete unlinks for good.
            authority = self.source.with_name(self.source.name + ".wallhaven.json")
            authority.write_text(json.dumps(_download_authority(self.source)), encoding="utf-8")
            self.item = MediaItem(self.source, Kind.STILL, len(ORIGINAL), 1)

    def path(self, module: ModuleType) -> Path:
        return self.state / str(module.STATE_FILENAME)

    def session(self) -> Session:
        session = Session(
            replace(config.Settings(), roots=(self.root,)).validated(),
            applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
            favourite_store=favourites.Store.open(self.path(favourites)),
            pairing_store=pairings.Store.open(self.path(pairings)),
            playlist_store=playlists.Store.open(self.path(playlists)),
            removal_store=removals.Store.open(self.path(removals)),
        )
        session.adopt_library(Library(roots=(self.root,), items=(self.item,)))
        return session

    def remove(self, session: Session) -> RemovalResult:
        return session.prepare_removal_plan(self.item, trash=self.trash).run()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A library root, as tests/test_control.py's ctl tests use it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "wallpapers"
    root.mkdir()
    return root


def _newer(module: ModuleType) -> bytes:
    """What a newer release could have written: a version this build does not know."""
    document = {"version": module.FORMAT_VERSION + 1, "from_the_future": True}
    return json.dumps(document).encode()


@pytest.fixture(params=[True, False], ids=["trash", "delete"])
def profile(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Profile:
    trash = bool(request.param)
    if not trash:
        monkeypatch.setattr(manage, "_is_managed_on_disk", lambda _path, **_keywords: True)
    return _Profile(tmp_path, trash=trash)


def _assert_untouched(profile: _Profile) -> None:
    assert profile.source.read_bytes() == ORIGINAL, "the wallpaper is still in place"
    journal = removals.Store.open(profile.path(removals))
    try:
        assert journal.records == (), "no removal intent was written"
        assert not journal.operation_is_active()
    finally:
        journal.close()
    trash = paths.data_home() / "Trash"
    moved = list(trash.rglob("paper*")) if trash.exists() else []
    assert moved == [], "nothing reached the trash"


@pytest.mark.parametrize("store", STORES)
def test_a_newer_store_refuses_the_removal_before_the_file_is_touched(
    profile: _Profile, store: str
) -> None:
    module = STORES[store]
    newer = _newer(module)
    profile.path(module).write_bytes(newer)
    session = profile.session()
    try:
        result = profile.remove(session)
    finally:
        session.shutdown()

    assert not result.committed
    assert result.physical is None
    assert result.error_kind == state_file.NEWER_VERSION
    message = result.error_message
    assert module.STATE_FILENAME in message and "newer version" in message
    assert "The wallpaper was left where it is." in message
    assert "with the version that saved that file" in message
    assert "Nothing was changed" not in message
    assert profile.path(module).read_bytes() == newer, "the newer file is never rewritten"
    _assert_untouched(profile)


def test_a_newer_file_that_appeared_after_opening_is_found_by_the_fresh_read(
    profile: _Profile,
) -> None:
    """The plan reads each store again on its worker; the live Store's view may be old."""
    session = profile.session()
    try:
        plan = session.prepare_removal_plan(profile.item, trash=profile.trash)
        profile.path(pairings).write_bytes(_newer(pairings))
        result = plan.run()
    finally:
        session.shutdown()
    assert not result.committed
    assert result.error_kind == state_file.NEWER_VERSION
    _assert_untouched(profile)


def test_an_unreadable_store_refuses_too(profile: _Profile) -> None:
    profile.path(favourites).write_text("{ not json", encoding="utf-8")
    session = profile.session()
    try:
        result = profile.remove(session)
    finally:
        session.shutdown()
    assert not result.committed
    assert result.error_kind == "invalid-state"
    assert favourites.STATE_FILENAME in result.error_message
    assert "Repair or restore that file" in result.error_message
    assert profile.path(favourites).read_text(encoding="utf-8") == "{ not json"
    _assert_untouched(profile)


def test_every_blocked_store_is_named(profile: _Profile) -> None:
    profile.path(pairings).write_bytes(_newer(pairings))
    profile.path(playlists).write_text("{ not json", encoding="utf-8")
    session = profile.session()
    try:
        result = profile.remove(session)
    finally:
        session.shutdown()
    assert result.error_kind == state_file.NEWER_VERSION
    assert pairings.STATE_FILENAME in result.error_message
    assert playlists.STATE_FILENAME in result.error_message
    _assert_untouched(profile)


def test_the_same_profile_with_current_stores_is_removed(profile: _Profile) -> None:
    """The control: nothing else about the fixture stops the removal."""
    session = profile.session()
    try:
        result = profile.remove(session)
    finally:
        session.shutdown()
    assert result.committed, result.error_message
    assert result.cleanup_failures == ()
    assert not profile.source.exists()


def test_a_newer_store_that_appears_after_the_check_is_worded_as_after_the_fact(
    profile: _Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow race left: the file is gone, so no message may say nothing changed."""
    newer = _newer(pairings)
    real_commit = Session.commit_removal

    def newer_appears_first(session: Session, *args: object, **kwargs: object) -> object:
        profile.path(pairings).write_bytes(newer)
        return real_commit(session, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Session, "commit_removal", newer_appears_first)
    session = profile.session()
    try:
        result = profile.remove(session)
    finally:
        session.shutdown()
    assert result.committed
    assert not profile.source.exists()
    (failure,) = result.cleanup_failures
    assert failure.startswith("pairing: pairings.json was saved by a newer version")
    assert "it may still list this wallpaper" in failure
    assert "Nothing was changed" not in " ".join(result.cleanup_failures)
    assert "Nothing was changed" not in manage.metadata_cleanup_note(result.cleanup_failures)


@pytest.mark.parametrize("store", STORES)
def test_ctl_remove_in_the_headless_service_refuses_up_front_too(sandbox: Path, store: str) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    module = STORES[store]
    commands, app = _commands(sandbox, [item])
    target = (
        sandbox / "favourites.json"
        if module is favourites
        else paths.app_state_dir() / module.STATE_FILENAME
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    newer = _newer(module)
    target.write_bytes(newer)

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert not response.ok
    assert response.kind == state_file.NEWER_VERSION
    assert "The wallpaper was left where it is." in response.message
    assert path.is_file(), "no physical operation may start"
    assert app.forgotten == []
    assert target.read_bytes() == newer
    assert app.session.removal_journal.records == ()
