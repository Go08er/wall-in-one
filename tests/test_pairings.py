"""Pairings: the bundle every library item resolves to.

The behaviour worth defending is that the common case costs nothing and the
customized case is durable. Everything below is one of those two, or the seam
between them: a default that improves must reach every item nobody has spoken
for, and must reach none of the items where it would overrule somebody.

Nothing here touches the user's directories. `XDG_STATE_HOME` is redirected and
every path is under `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wall_in_one.library import pairing, pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.pairings import (
    Identity,
    Medium,
    Pairing,
    PairingError,
    PalettePolicy,
    Store,
)


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


def item(path: Path, kind: Kind = Kind.STILL) -> MediaItem:
    return MediaItem(path=path, kind=kind, size=1, mtime=0)


def png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 32)
    return path


# -- identity -------------------------------------------------------------


def test_a_still_and_a_video_at_the_same_path_are_different_identities() -> None:
    """They cannot both exist, but the key has to carry the medium anyway --
    a Workshop scene will be identified by its id, not by a path at all."""
    path = Path("/w/thing")
    assert Identity.of(item(path, Kind.STILL)) != Identity.of(item(path, Kind.VIDEO))


def test_an_identity_survives_the_round_trip() -> None:
    original = Identity(medium=Medium.VIDEO, source="/w/clip.mp4")
    assert Identity.parse(original.key) == original


def test_a_source_containing_a_colon_survives() -> None:
    """Paths may contain colons, so the key splits once and only once."""
    original = Identity(medium=Medium.STILL, source="/w/od:d/name:with:colons.png")
    assert Identity.parse(original.key) == original


@pytest.mark.parametrize("key", ["", "still", "still:", ":/w/a.png", "nebula:1", "nonsense"])
def test_an_unreadable_key_is_dropped_rather_than_guessed(key: str) -> None:
    """A medium this build does not know is not ours to interpret, and not
    ours to mangle. `scene:` used to be the example here; it is a real medium
    now, which is what the design anticipated."""
    assert Identity.parse(key) is None


def test_a_scene_is_keyed_by_its_workshop_id_not_its_directory() -> None:
    """A reinstall moves the directory. Losing the still somebody chose
    because Steam unpacked it somewhere else would be the whole reason a
    record is keyed `medium:source` rather than by a filename."""
    scene = MediaItem(
        path=Path("/steam/workshop/content/431960/2149140853"),
        kind=Kind.SCENE,
        size=1,
        mtime=0,
        scene="2149140853",
    )
    identity = Identity.of(scene)
    assert identity.key == "scene:2149140853"
    assert Identity.parse(identity.key) == identity


# -- the default bundle ---------------------------------------------------


def test_a_still_pairs_with_itself(tmp_path: Path) -> None:
    picture = png(tmp_path / "a.png")
    bundle = pairings.synthesize(item(picture))
    assert bundle.still == picture
    assert bundle.motion is None
    assert not bundle.is_moving


def test_a_video_pairs_with_the_still_the_conventions_find(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    sibling = png(tmp_path / "clip-still.png")
    bundle = pairings.synthesize(item(clip, Kind.VIDEO), roots=[tmp_path])
    assert bundle.motion == clip
    assert bundle.still == sibling


def test_a_video_with_no_still_anywhere_has_none(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    bundle = pairings.synthesize(item(clip, Kind.VIDEO), roots=[tmp_path])
    assert bundle.still is None
    assert bundle.is_moving


def test_a_default_bundle_is_not_customized(tmp_path: Path) -> None:
    assert not pairings.synthesize(item(png(tmp_path / "a.png"))).customized


# -- choosing ------------------------------------------------------------


def test_a_chosen_still_wins_over_the_convention(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    png(tmp_path / "clip-still.png")
    chosen = png(tmp_path / "chosen.png")

    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), chosen)

    assert store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still == chosen


def test_a_choice_outlives_the_process(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    chosen = png(tmp_path / "chosen.png")
    target = tmp_path / "pairings.json"

    Store(path=target).choose_still(item(clip, Kind.VIDEO), chosen)

    reopened = Store.open(target)
    assert reopened.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still == chosen


def test_only_customizations_are_written(tmp_path: Path) -> None:
    """A record exists because somebody made a choice. Writing the defaults
    too would mean a better default could never reach anyone."""
    target = tmp_path / "pairings.json"
    store = Store(path=target)
    store.choose_still(item(png(tmp_path / "a.png")), png(tmp_path / "b.png"))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert len(written["pairings"]) == 1


def test_a_better_default_reaches_an_item_nobody_chose_for(tmp_path: Path) -> None:
    """The whole point of storing only choices. A still appearing next to a
    video later must be picked up without anyone doing anything."""
    clip = video(tmp_path / "clip.mp4")
    store = Store(path=tmp_path / "pairings.json")
    assert store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still is None

    generated = png(pairing.still_directory(tmp_path) / "clip.png")
    assert store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still == generated


def test_a_better_default_does_not_overrule_a_choice(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    chosen = png(tmp_path / "chosen.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), chosen)

    png(pairing.still_directory(tmp_path) / "clip.png")

    assert store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still == chosen


def test_a_chosen_still_that_is_not_there_falls_back_and_says_so(tmp_path: Path) -> None:
    """A drive that is not mounted this morning is not somebody changing their
    mind, so the choice is kept and the item stays showable."""
    clip = video(tmp_path / "clip.mp4")
    sibling = png(tmp_path / "clip-still.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), tmp_path / "elsewhere" / "gone.png")

    bundle = store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path])
    assert bundle.still == sibling
    assert bundle.override_missing
    assert bundle.customized
    assert store.get(Identity.of(item(clip, Kind.VIDEO))) is not None


def test_resetting_returns_an_item_to_the_default(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    sibling = png(tmp_path / "clip-still.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), png(tmp_path / "chosen.png"))

    assert store.reset(item(clip, Kind.VIDEO)) is True
    assert store.reset(item(clip, Kind.VIDEO)) is False
    assert store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path]).still == sibling


def test_clearing_the_still_keeps_the_rest_of_the_customization(tmp_path: Path) -> None:
    picture = png(tmp_path / "a.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_palette(item(picture), PalettePolicy(kind=pairings.KEEP))
    store.choose_still(item(picture), None)
    assert store.resolve(item(picture)).palette.keeps_palette


def test_choosing_a_palette_leaves_a_chosen_still_alone(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    chosen = png(tmp_path / "chosen.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), chosen)
    store.choose_palette(item(clip, Kind.VIDEO), PalettePolicy("builtin", "Nord"))
    bundle = store.resolve(item(clip, Kind.VIDEO), roots=[tmp_path])
    assert bundle.still == chosen
    assert (bundle.palette.kind, bundle.palette.name) == ("builtin", "Nord")


def test_a_deleted_wallpaper_loses_its_record(tmp_path: Path) -> None:
    """Records outlive a missing file on purpose. Not one we destroyed."""
    picture = png(tmp_path / "a.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_palette(item(picture), PalettePolicy(kind=pairings.KEEP))
    assert store.forget_identity(Identity.of(item(picture))) is True
    assert len(store) == 0


# -- applying over a library ---------------------------------------------


def test_a_still_spent_representing_a_video_leaves_the_rotation(tmp_path: Path) -> None:
    clip = video(tmp_path / "clip.mp4")
    sibling = png(tmp_path / "clip-still.png")
    standalone = png(tmp_path / "holiday.png")

    kept = pairings.apply(
        [item(clip, Kind.VIDEO), item(sibling), item(standalone)], roots=[tmp_path]
    )

    assert [entry.path for entry in kept] == [clip, standalone]
    assert kept[0].paired_still == sibling


def test_choosing_a_different_still_frees_the_old_one(tmp_path: Path) -> None:
    """The reason this cannot be an overlay on an already-paired library: the
    still the convention had spent has to come back into the rotation."""
    clip = video(tmp_path / "clip.mp4")
    sibling = png(tmp_path / "clip-still.png")
    chosen = png(tmp_path / "chosen.png")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(clip, Kind.VIDEO), chosen)

    kept = store.apply([item(clip, Kind.VIDEO), item(sibling), item(chosen)], roots=[tmp_path])

    by_path = {entry.path: entry for entry in kept}
    assert chosen not in by_path
    assert sibling in by_path
    assert by_path[clip].paired_still == chosen


def test_two_videos_may_share_one_still(tmp_path: Path) -> None:
    shared = png(tmp_path / "shared.png")
    first = video(tmp_path / "one.mp4")
    second = video(tmp_path / "two.mp4")
    store = Store(path=tmp_path / "pairings.json")
    store.choose_still(item(first, Kind.VIDEO), shared)
    store.choose_still(item(second, Kind.VIDEO), shared)

    kept = store.apply(
        [item(first, Kind.VIDEO), item(second, Kind.VIDEO), item(shared)], roots=[tmp_path]
    )

    assert [entry.path for entry in kept] == [first, second]
    assert all(entry.paired_still == shared for entry in kept)


def test_applying_nothing_is_not_an_error(tmp_path: Path) -> None:
    assert pairings.apply([], roots=[tmp_path]) == ()


# -- the file ------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"pairings": "not a list"}', "null", '{"nope": 1}'],
    ids=["empty", "garbage", "array", "wrong-type", "null", "no-key"],
)
def test_an_unreadable_file_is_no_customizations_rather_than_a_crash(
    tmp_path: Path, content: str
) -> None:
    target = tmp_path / "pairings.json"
    target.write_text(content, encoding="utf-8")
    assert pairings.load(target) == {}


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"pairings": [{"identity": "still:/w/a.png"}]}), encoding="utf-8")
    link = tmp_path / "pairings.json"
    link.symlink_to(real)
    assert pairings.load(link) == {}


def test_one_bad_record_costs_only_that_record(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    target.write_text(
        json.dumps(
            {
                "pairings": [
                    {"identity": "still:/w/a.png", "still": "/w/chosen.png"},
                    {"identity": "nonsense"},
                    "not even an object",
                    {"identity": "still:/w/b.png", "still": "relative/no.png"},
                ]
            }
        ),
        encoding="utf-8",
    )
    records = pairings.load(target)
    assert set(records) == {"still:/w/a.png", "still:/w/b.png"}
    # The relative path was dropped, but the record it was in survives.
    assert records["still:/w/b.png"].still is None


def test_an_unrecognised_version_is_still_read(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    target.write_text(
        json.dumps({"version": 99, "pairings": [{"identity": "still:/w/a.png"}]}),
        encoding="utf-8",
    )
    assert set(pairings.load(target)) == {"still:/w/a.png"}


def test_the_write_is_a_single_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pairings.json"
    picture = png(tmp_path / "a.png")
    store = Store(path=target)
    store.choose_palette(item(picture), PalettePolicy("builtin", "first"))
    observed: list[str] = []
    real_replace = os.replace

    def watch(source: object, destination: object) -> None:
        observed.append(target.read_text(encoding="utf-8"))
        assert Path(str(source)).parent == target.parent
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", watch)
    store.choose_palette(item(picture), PalettePolicy("builtin", "second"))
    assert "first" in observed[0]


def test_a_failed_write_leaves_no_debris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_source: object, _destination: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(PairingError) as caught:
        pairings.save(
            {"still:/w/a.png": Pairing(identity=Identity(Medium.STILL, "/w/a.png"))},
            tmp_path / "pairings.json",
        )
    assert caught.value.kind == "local-io"
    assert list(tmp_path.iterdir()) == []


def test_a_broken_file_is_moved_aside_rather_than_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    target.write_text("not json but somebody's choices", encoding="utf-8")
    store = Store.open(target)
    assert store.fault is not None
    store.choose_palette(item(png(tmp_path / "a.png")), PalettePolicy(kind=pairings.KEEP))
    kept = target.with_name(target.name + pairings.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "not json but somebody's choices"


def test_the_default_file_lives_beside_the_favourites(state_home: Path) -> None:
    assert pairings.state_path().parent == state_home / "wall-in-one"


# -- the palette policy ---------------------------------------------------


def test_the_default_policy_is_adaptive() -> None:
    assert PalettePolicy().is_adaptive
    assert not PalettePolicy().keeps_palette


def test_adaptive_asks_noctalia_for_wallpaper_colours() -> None:
    """ "Adaptive" and "generated from this wallpaper with m3-tonal-spot" are
    the same request, so the generator the user chose is the name."""
    selection = PalettePolicy().selection("m3-fruit-salad")
    assert selection is not None
    assert (selection.source, selection.name) == ("wallpaper", "m3-fruit-salad")


def test_adaptive_can_pin_its_own_generator() -> None:
    selection = PalettePolicy(pairings.ADAPTIVE, "muted").selection("m3-tonal-spot")

    assert selection is not None
    assert (selection.source, selection.name) == ("wallpaper", "muted")


def test_unknown_adaptive_generator_falls_back_safely() -> None:
    policy = PalettePolicy(pairings.ADAPTIVE, "not-a-generator")

    assert policy.adaptive_scheme("soft") == "soft"


def test_keeping_the_palette_asks_for_nothing() -> None:
    """The one policy that is not a palette. There is nothing to send."""
    assert PalettePolicy(kind=pairings.KEEP).selection("m3-tonal-spot") is None


@pytest.mark.parametrize("source", ["builtin", "community", "custom"])
def test_each_noctalia_source_is_passed_through(source: str) -> None:
    selection = PalettePolicy(source, "Nord").selection("m3-tonal-spot")
    assert selection is not None
    assert (selection.source, selection.name) == (source, "Nord")


def test_a_named_source_with_no_name_asks_for_nothing() -> None:
    """`color-scheme-set builtin ''` is not a request, it is a mistake."""
    assert PalettePolicy("builtin", "").selection("m3-tonal-spot") is None


def test_an_unknown_source_asks_for_nothing_rather_than_guessing() -> None:
    assert PalettePolicy("something-new", "x").selection("m3-tonal-spot") is None


@pytest.mark.parametrize(
    ("policy", "encoded"),
    [
        (PalettePolicy(), "adaptive"),
        (PalettePolicy(kind=pairings.KEEP), "keep"),
        (PalettePolicy("builtin", "Nord"), "builtin:Nord"),
        (PalettePolicy("community", "Osaka jade"), "community:Osaka jade"),
    ],
)
def test_a_policy_survives_the_wire_form(policy: PalettePolicy, encoded: str) -> None:
    assert policy.encode() == encoded
    assert PalettePolicy.decode(encoded) == policy


def test_a_name_containing_a_colon_survives() -> None:
    original = PalettePolicy("custom", "mine:v2")
    assert PalettePolicy.decode(original.encode()) == original


@pytest.mark.parametrize("raw", ["", "   ", None, 7, ":", ":name"])
def test_an_unusable_policy_reads_as_adaptive(raw: object) -> None:
    """Which is what every wallpaper did before policies existed."""
    assert PalettePolicy.decode(raw).is_adaptive


def test_a_source_this_build_does_not_know_survives_a_round_trip() -> None:
    """It cannot be applied -- `selection` refuses it -- but a build that
    predates a new Noctalia source must not silently rewrite the record."""
    policy = PalettePolicy.decode("nebula:something")
    assert (policy.kind, policy.name) == ("nebula", "something")
    assert policy.encode() == "nebula:something"


@pytest.mark.parametrize("mode", list(pairings.Mode))
def test_a_mode_survives_the_round_trip(tmp_path: Path, mode: pairings.Mode) -> None:
    target = tmp_path / "pairings.json"
    picture = png(tmp_path / "a.png")
    store = Store(path=target)
    store.choose_palette(item(picture), PalettePolicy("builtin", "Nord", mode))
    assert Store.open(target).resolve(item(picture)).palette.mode is mode


def test_a_record_that_says_nothing_about_mode_keeps_the_current_one(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    target.write_text(
        json.dumps({"pairings": [{"identity": "still:/w/a.png", "palette": "builtin:Nord"}]}),
        encoding="utf-8",
    )
    assert pairings.load(target)["still:/w/a.png"].palette.mode is pairings.Mode.KEEP


def test_an_unreadable_mode_keeps_the_current_one(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    target.write_text(
        json.dumps({"pairings": [{"identity": "still:/w/a.png", "mode": "purple"}]}),
        encoding="utf-8",
    )
    assert pairings.load(target)["still:/w/a.png"].palette.mode is pairings.Mode.KEEP


def test_keeping_the_mode_is_not_written_out(tmp_path: Path) -> None:
    """The common case leaves no trace, so a hand-edited file stays readable.

    Checked on the parsed key rather than as a substring: `tmp_path` carries
    the test's own name, which contains "mode", and the substring version
    passed for the wrong reason.
    """
    target = tmp_path / "pairings.json"
    Store(path=target).choose_palette(item(png(tmp_path / "a.png")), PalettePolicy("builtin", "N"))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert "mode" not in written["pairings"][0]
