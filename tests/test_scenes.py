"""Driving `linux-wallpaperengine` for Wallpaper Engine scenes.

The command line is checked rather than run: starting the engine puts a
wallpaper on somebody's screen, and the argument order carries meaning --
`--screen-root` must precede `--bg`, because its own help says the following
options apply to the preceding screen.

Finding a *foreign* engine is the other half, and it is the one that matters
most here. The machine this was written on runs Noctalia's own
`linux-wallpaperengine-controller` plugin, whose engine had been rendering the
desktop for two and a half hours. Two programs driving one output is two
programs fighting over one wallpaper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.wallpaper import scenes
from wall_in_one.wallpaper.scenes import SceneRenderer


def command(**options: object) -> list[str]:
    return SceneRenderer(**options).command("12345")  # type: ignore[arg-type]


# -- what gets run --------------------------------------------------------


def test_an_output_is_named_before_the_background() -> None:
    """The order is the meaning: options apply to the preceding screen."""
    arguments = command(output="DP-2")
    assert arguments.index("--screen-root") < arguments.index("--bg")
    assert arguments[arguments.index("--screen-root") + 1] == "DP-2"
    assert arguments[arguments.index("--bg") + 1] == "12345"


def test_with_no_output_the_scene_is_positional() -> None:
    """Which is how its own help spells "everywhere"."""
    arguments = command()
    assert "--screen-root" not in arguments
    assert arguments[-1] == "12345"


def test_scaling_and_clamp_follow_the_screen_they_apply_to() -> None:
    arguments = command(output="DP-2", scaling="fill", clamp="border")
    assert arguments.index("--screen-root") < arguments.index("--scaling")
    assert arguments.index("--scaling") < arguments.index("--bg")


def test_a_scene_starts_silent() -> None:
    """A wallpaper that makes noise is a surprise, the same as for video."""
    assert "--silent" in command()


def test_a_volume_is_passed_when_it_is_not_silent() -> None:
    arguments = command(silent=False, volume=40)
    assert "--silent" not in arguments
    assert arguments[arguments.index("--volume") + 1] == "40"


def test_pausing_when_covered_is_the_default_and_is_expressed_by_omission() -> None:
    """The flag is `--no-fullscreen-pause`, so pausing is what you get by
    saying nothing."""
    assert "--no-fullscreen-pause" not in command()
    assert "--no-fullscreen-pause" in command(pause_when_covered=False)


def test_a_screenshot_run_carries_a_delay() -> None:
    """Scenes animate in, and the first frame is the least representative one."""
    arguments = SceneRenderer().command("12345", screenshot=Path("/tmp/x.png"))
    assert arguments[arguments.index("--screenshot") + 1] == "/tmp/x.png"
    assert int(arguments[arguments.index("--screenshot-delay") + 1]) > 0


def test_the_layer_is_the_one_niri_wants() -> None:
    """Its own help names `background` for niri, to pair with the
    `place-within-backdrop` rule; `bottom` clones the wallpaper into every
    workspace in the overview."""
    assert scenes.DEFAULT_LAYER == "background"
    assert command()[command().index("--layer") + 1] == "background"


def test_a_scene_with_no_id_is_refused() -> None:
    with pytest.raises(scenes.SceneError):
        SceneRenderer().start("   ")


# -- somebody else's engine ----------------------------------------------


def fake_proc(root: Path, processes: dict[int, list[str]]) -> Path:
    """A `/proc` with the command lines given, for the finder to read."""
    for pid, arguments in processes.items():
        entry = root / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments) + b"\0")
    (root / "not-a-pid").mkdir()
    return root


def test_an_engine_holding_an_output_is_found(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        {2388: ["/nix/store/x/bin/linux-wallpaperengine", "--screen-root", "eDP-1", "--bg", "1"]},
    )
    assert scenes.running_elsewhere("eDP-1", proc) == (2388,)


def test_an_engine_on_a_different_output_is_not_a_conflict(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        {2388: ["linux-wallpaperengine", "--screen-root", "eDP-1", "--bg", "1"]},
    )
    assert scenes.running_elsewhere("DP-2", proc) == ()


def test_an_engine_previewing_in_a_window_owns_no_output(tmp_path: Path) -> None:
    """So it is not something to refuse over: it is not on anybody's desktop."""
    proc = fake_proc(tmp_path, {99: ["linux-wallpaperengine", "12345"]})
    assert scenes.running_elsewhere("", proc) == ()
    assert scenes.running_elsewhere("eDP-1", proc) == ()


def test_other_programs_are_not_mistaken_for_the_engine(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        {
            1: ["/lib/systemd/systemd", "--user"],
            2: ["mpvpaper", "--layer", "background", "ALL", "clip.mp4"],
            3: ["grep", "linux-wallpaperengine"],
        },
    )
    assert scenes.running_elsewhere("", proc) == ()


def test_a_proc_that_cannot_be_read_finds_nothing(tmp_path: Path) -> None:
    assert scenes.running_elsewhere("", tmp_path / "absent") == ()


def test_an_unreadable_process_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    """A process can exit between the listing and the read."""
    (tmp_path / "1234").mkdir()
    proc = fake_proc(
        tmp_path,
        {2388: ["linux-wallpaperengine", "--screen-root", "eDP-1", "--bg", "1"]},
    )
    assert scenes.running_elsewhere("eDP-1", proc) == (2388,)


# -- a scene as a library item --------------------------------------------


def scene_item(workshop_id: str = "2149140853", still: Path | None = None) -> MediaItem:
    return MediaItem(
        path=Path("/steam/workshop/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=1,
        mtime=0,
        scene=workshop_id,
        paired_still=still,
    )


def test_a_scene_counts_as_a_moving_wallpaper() -> None:
    assert scene_item().is_moving
    assert Kind.SCENE.moves
    assert not Kind.STILL.moves


def test_a_scene_with_no_still_cannot_be_shown_with_dynamics_off() -> None:
    """The same rule videos follow: there is nothing to put on screen."""
    assert scene_item().playback_path(dynamics_enabled=False) is None
    assert scene_item(still=Path("/w/a.png")).playback_path(dynamics_enabled=False) == Path(
        "/w/a.png"
    )


def test_the_filter_treats_a_scene_as_moving(tmp_path: Path) -> None:
    """The split somebody makes is moving-versus-still; which renderer draws
    the moving one is not a distinction they were asking about."""
    from wall_in_one.library.filter import Kinds

    assert Kinds.VIDEOS.accepts(Kind.SCENE)
    assert not Kinds.STILLS.accepts(Kind.SCENE)


def test_a_scene_is_refused_when_the_app_does_not_own_the_renderer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default. Somebody else's engine is probably drawing the desktop."""
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", lambda *a, **k: None)
    from wall_in_one.wallpaper.applier import Applier, ApplyError

    applier = Applier(_QuietRenderer(), own_scene_renderer=False)  # type: ignore[arg-type]
    with pytest.raises(ApplyError) as caught:
        applier.apply(scene_item(still=tmp_path / "a.png"), dynamics_enabled=True)
    assert "not set to drive" in str(caught.value)


def test_a_scene_is_refused_when_another_engine_holds_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two programs driving one output is two programs fighting over one
    wallpaper, and the loser is whichever the user was looking at."""
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", lambda *a, **k: None)
    monkeypatch.setattr("wall_in_one.wallpaper.scenes.running_elsewhere", lambda _o: (2388,))
    from wall_in_one.wallpaper.applier import Applier, ApplyError

    applier = Applier(_QuietRenderer(), "eDP-1", own_scene_renderer=True)  # type: ignore[arg-type]
    with pytest.raises(ApplyError) as caught:
        applier.apply(scene_item(still=tmp_path / "a.png"), dynamics_enabled=True)
    assert "already running" in str(caught.value)
    assert "2388" in str(caught.value)


def test_the_still_still_reaches_the_screen_when_a_scene_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refusal leaves the scene's representative up rather than nothing."""
    applied: list[Path] = []
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper",
        lambda path, connector=None: applied.append(Path(path)),
    )
    from wall_in_one.wallpaper.applier import Applier, ApplyError

    applier = Applier(_QuietRenderer(), own_scene_renderer=False)  # type: ignore[arg-type]
    with pytest.raises(ApplyError):
        applier.apply(scene_item(still=tmp_path / "a.png"), dynamics_enabled=True)
    assert applied == [tmp_path / "a.png"]


class _QuietRenderer:
    """Stands in for mpvpaper, which a scene test has no business starting."""

    def __init__(self) -> None:
        self.stops = 0

    def start(self, video: Path) -> None: ...

    def stop(self) -> None:
        self.stops += 1
