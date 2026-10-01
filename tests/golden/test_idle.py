"""Open a stored profile, idle through one health sync, close: what was written?

Only the writes in :func:`first_start_writes` may appear, each with its
reason and a check of its content; a second identical run must write nothing
at all. Every file below the sandbox home counts, dotfiles and the
``.wall-in-one-removal-*`` evidence included.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden import harness
from tests.golden.harness import Allowance, Change, FakeRuntime, Profile
from tests.golden.sandbox import (
    STATE,
    STORE_FILES,
    Golden,
    decorate,
    read_json,
    runtime_document,
    write_json,
)
from wall_in_one import cli, deployed_upgrade_transaction, runtime_config
from wall_in_one.library import (
    displays,
    favourites,
    pairings,
    playlists,
    removals,
    schedules,
    state_file,
)
from wall_in_one.session import QUICK_CHOICE_ID

# -- the fixture's own contract -------------------------------------------------------


def test_fixture_has_the_shapes_the_plan_requires(golden: Golden) -> None:
    """The fixture keeps what made the real profile hard (plan §3, Release 1.4)."""
    state = golden.profile.app_state
    lists = read_json(state / "playlists.json")["playlists"]
    assert any(entry["id"] == QUICK_CHOICE_ID for entry in lists)
    assert len([entry for entry in lists if entry["id"] != QUICK_CHOICE_ID]) >= 2
    rules = read_json(state / "schedules.json")["rules"]
    assert any(
        rule["playlist"] == QUICK_CHOICE_ID and rule.get("enabled") is False for rule in rules
    ), "a disabled schedule rule must point at quick-choice"
    assert len(read_json(state / "displays.json")["displays"]) >= 2
    assert read_json(state / "pairings.json")["pairings"]
    assert (state / "palette.json").is_file()
    noctalia_settings = tomllib.loads(
        (golden.profile.state_home / "noctalia" / "settings.toml").read_text()
    )
    registration = noctalia_settings["theme"]["templates"]["user"]["wall-in-one"]
    assert registration["input_path"] == str(state / "palette.json.tmpl"), (
        "Noctalia must still reference the stale, non-content-addressed template name"
    )
    assert (state / "palette.json.tmpl").is_file()
    assert read_json(state / "pending-removals.json") == {"version": 1, "removals": []}
    for marker in ("deployed-upgrade-v1.json", "deployed-capture-adoption-v1.json"):
        assert (state / marker).is_file()
    assert (state / "runtime.toml").is_file()
    assert (golden.profile.app_config / "settings.toml").is_file()
    removal_settings = list(golden.profile.app_config.glob(".wall-in-one-removal-*/entry"))
    assert removal_settings, "a removal folder must hold the pre-upgrade settings copy"
    assert "roots = []" in removal_settings[0].read_text()


def test_relocated_deployed_evidence_is_accepted_as_complete(golden: Golden) -> None:
    """Materializing re-seals the markers; this build must read them as done."""
    found = deployed_upgrade_transaction.probe()
    assert found.status == "complete", found.detail


def _all_media(profile: Profile) -> list[dict[str, Any]]:
    document = runtime_document(profile)
    (fallback,) = (
        entry
        for entry in document["playlists"]
        if entry["id"] == runtime_config.FALLBACK_PLAYLIST_ID
    )
    entries = fallback.get("entries", [])
    assert isinstance(entries, list)
    return entries


def test_relocation_keeps_path_derived_names_in_step(golden: Golden) -> None:
    """All-media ids and generated still names follow the sandbox paths."""
    videos = [entry for entry in _all_media(golden.profile) if "motion" in entry]
    assert videos
    for entry in videos:
        motion = Path(entry["motion"])
        assert motion.is_relative_to(golden.profile.home)
        assert entry["id"] == runtime_config.entry_id_for_source(motion)
        assert Path(entry["still"]).is_file(), entry["still"]


# -- idle round trip -------------------------------------------------------------------


def _video_under_test(profile: Profile) -> Path:
    """The first video in All media: the one the fake runtime reports as failing."""
    for entry in _all_media(profile):
        if "motion" in entry:
            return Path(entry["motion"])
    pytest.skip("the profile has no video to report as failing")


def _report_failure(runtime: FakeRuntime, video: Path) -> tuple[str, str]:
    reason, source = "renderer rejected this wallpaper", "automatic-apply"
    runtime.taboo.append(
        {
            "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
            "entry_id": runtime_config.entry_id_for_source(video),
            "reason": reason,
            "source": source,
            "durable": False,
            "observed_config_epoch": 1,
        }
    )
    return reason, source


def _without_taboo(document: dict[str, Any], motion: str | None) -> dict[str, Any]:
    for playlist in document.get("playlists", []):
        for entry in playlist.get("entries", []):
            if motion is not None and entry.get("motion") == motion:
                entry.pop("taboo", None)
    return document


def first_start_writes(profile: Profile, failed: tuple[Path, str, str] | None) -> list[Allowance]:
    """The complete whitelist for opening a stored profile on a new machine."""
    motion = profile.to_original(str(failed[0])) if failed else None

    def runtime_is_the_same_program(change: Change) -> None:
        assert change.before is not None and change.before.content is not None
        assert change.after is not None and change.after.content is not None
        before = harness.runtime_document_semantics(change.before.content.decode(), profile)
        after = harness.runtime_document_semantics(change.after.content.decode(), profile)
        if failed is not None:
            marked = [
                entry
                for playlist in after["playlists"]
                for entry in playlist.get("entries", [])
                if entry.get("motion") == motion
            ]
            assert marked and all(
                entry.get("taboo") == {"reason": failed[1], "source": failed[2]} for entry in marked
            ), "the reported failure is not compiled as a taboo on every entry of that video"
            after = _without_taboo(after, motion)
            before = _without_taboo(before, motion)
        assert after == before, "the recompiled runtime.toml differs in meaning"

    def pairings_gained_exactly_one_marker(change: Change) -> None:
        assert failed is not None
        assert change.before is not None and change.before.content is not None
        assert change.after is not None and change.after.content is not None
        before = json.loads(change.before.content)
        after = json.loads(change.after.content)
        identity = f"video:{failed[0]}"
        marker = {"state": "borked", "reason": failed[1], "source": failed[2]}
        expected = {key: value for key, value in before.items() if key != "pairings"}
        assert {key: value for key, value in after.items() if key != "pairings"} == expected
        old = {record["identity"]: record for record in before["pairings"]}
        new = {record["identity"]: record for record in after["pairings"]}
        assert set(new) == set(old) | {identity}, "records were added or lost"
        for key, record in new.items():
            if key == identity:
                gained = {name: value for name, value in record.items() if name != "health"}
                assert gained == old.get(key, {"identity": identity})
                assert record["health"] == marker
            else:
                assert record == old[key], f"untouched record {key} changed"

    allowed = [
        Allowance(
            f"{STATE}/runtime.toml",
            frozenset({"modified", "rewritten"}),
            "first start on a machine that did not compile it: the renderer program "
            "paths are this machine's, so the document and its config_generation are "
            "recompiled. The meaning must be identical.",
            runtime_is_the_same_program,
        )
    ]
    if failed is not None:
        allowed.append(
            Allowance(
                f"{STATE}/pairings.json",
                frozenset({"modified"}),
                "the 30 s health sync persists the runtime's failure report as a health "
                "marker on that one wallpaper's record; that is its only job",
                pairings_gained_exactly_one_marker,
            )
        )
    return allowed


def test_no_allowance_may_touch_evidence() -> None:
    """Dotfiles and removal folders are pre-upgrade copies: never on a whitelist."""
    profile = Profile(Path("/x"), Path("/x"), Path("/x/home"), harness.FIXTURE_HOME, Path("/r"))
    for failed in (None, (Path("/x/home/a.mp4"), "r", "s")):
        for allowance in first_start_writes(profile, failed):
            name = allowance.pattern.rsplit("/", 1)[-1]
            assert not name.startswith("."), allowance.pattern
            assert ".wall-in-one-removal-" not in allowance.pattern


def _decorate_every_store(profile: Profile) -> None:
    """What a newer release would write: unknown keys at the top and on every record."""
    for name in STORE_FILES:
        target = profile.app_state / name
        if target.is_file():
            document = read_json(target)
            decorate(document)
            write_json(target, document)


@pytest.mark.parametrize("scenario", ["quiet", "one-failure", "one-failure-newer-keys"])
def test_idle_round_trip_writes_only_what_is_whitelisted(any_golden: Golden, scenario: str) -> None:
    """``one-failure-newer-keys``: the health marker write carries every unknown key."""
    profile = any_golden.profile
    failed: tuple[Path, str, str] | None = None
    if scenario.startswith("one-failure"):
        video = _video_under_test(profile)
        failed = (video, *_report_failure(any_golden.runtime, video))
    if scenario.endswith("newer-keys"):
        _decorate_every_store(profile)

    before = harness.snapshot(profile.home)
    first = harness.run_idle()
    after_first = harness.snapshot(profile.home)
    assert first.library_size > 0
    assert first.skipped == 0
    assert first.service_prepare == 0
    assert first.gui_compile in ("changed", "unchanged"), first.gui_compile
    assert first.health_sync == 0
    assert first.health_sync_on_stop == 0
    assert first.newer_version_files == ()
    assert first.unknown_settings == ()
    assert first.repair_paused == ()
    harness.check_changes(harness.diff(before, after_first), first_start_writes(profile, failed))

    # Steady state: the same build, the same inputs, nothing written at all.
    second = harness.run_idle()
    harness.check_changes(harness.diff(after_first, harness.snapshot(profile.home)), ())
    assert second == first
    assert any_golden.processes == []
    assert "status" in any_golden.runtime.verbs


# -- a newer file opened by this build: read-only, nothing rewritten ----------------------

STORE_MODULES: Final[dict[str, Any]] = {
    "pairings.json": pairings,
    "playlists.json": playlists,
    "schedules.json": schedules,
    "displays.json": displays,
    "favourites.json": favourites,
    "pending-removals.json": removals,
}


@pytest.mark.parametrize("scenario", ["quiet", "one-failure"])
@pytest.mark.parametrize("filename", STORE_FILES)
def test_idle_with_a_newer_store_file_writes_nothing_at_all(
    golden: Golden, filename: str, scenario: str
) -> None:
    """A newer release's version bump, opened by this build: reported, compiled from
    nothing, written to never. Not even the first-start runtime.toml rebase:
    compilation refuses a newer store, so the last-known-good document stays.
    """
    profile = golden.profile
    if scenario == "one-failure":
        _report_failure(golden.runtime, _video_under_test(profile))
    target = profile.app_state / filename
    document = read_json(target)
    document["version"] = STORE_MODULES[filename].FORMAT_VERSION + 1
    decorate(document)
    write_json(target, document)

    before = harness.snapshot(profile.home)
    run = harness.run_idle()
    harness.check_changes(harness.diff(before, harness.snapshot(profile.home)), ())

    assert run.newer_version_files == (filename,)
    assert run.service_prepare == 0, "the service must still start on the last-known-good config"
    assert run.gui_compile.startswith("refused:"), run.gui_compile
    assert "newer version" in run.gui_compile
    # The health sync reads authoring only when the runtime reported something.
    expected_sync = 1 if scenario == "one-failure" else 0
    assert (run.health_sync, run.health_sync_on_stop) == (expected_sync, expected_sync)
    assert golden.processes == []
    store = STORE_MODULES[filename].Store.open()
    try:
        assert store.fault_kind == state_file.NEWER_VERSION, store.fault
    finally:
        if isinstance(store, removals.Store):
            store.close()


# -- settings.toml with a key from a newer build -----------------------------------------


def test_idle_with_an_unknown_settings_key_never_writes_settings(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read-only settings: the service starts on the known keys, the file stays.

    runtime.toml may be (re)compiled, but only to exactly what the known keys
    produce: removing the unknown key afterwards must leave it current. No
    ui.toml appears either; idle never creates it.
    """
    profile = golden.profile
    settings = profile.app_config / "settings.toml"
    settings.write_bytes(settings.read_bytes() + b"ui_glass_frost = 0.3\n")
    before = harness.snapshot(profile.home)

    run = harness.run_idle()
    # Idle attempts no settings write at all, so not even the settings lock
    # (which a refused write would take) may be touched.
    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)), first_start_writes(profile, None)
    )
    assert run.unknown_settings == ("ui_glass_frost",)
    assert run.service_prepare == 0
    assert run.gui_compile in ("changed", "unchanged"), run.gui_compile
    assert "ui_glass_frost" in capsys.readouterr().err

    compiled = (profile.app_state / "runtime.toml").read_bytes()
    text = settings.read_text().replace("ui_glass_frost = 0.3\n", "")
    settings.write_text(text)
    assert cli.main(["--write-config"]) == 0
    assert "already current" in capsys.readouterr().out
    assert (profile.app_state / "runtime.toml").read_bytes() == compiled


# -- damaged settings.toml on a profile with a completed upgrade ---------------------------

DAMAGED_SETTINGS: Final[dict[str, Callable[[str], str]]] = {
    "malformed": lambda text: text + "opacity = [\n",
    "invalid": lambda text: text.replace("opacity = 0.40", "opacity = 7.5"),
}


@pytest.mark.parametrize("damage", DAMAGED_SETTINGS, ids=list(DAMAGED_SETTINGS))
def test_damaged_settings_refuse_to_start_and_write_nothing(golden: Golden, damage: str) -> None:
    """The fixture carries a completed deployed-upgrade marker, which once let a
    damaged settings.toml through silently. Now the pre-GTK gate and both
    headless writers stop with EX_CONFIG, and not one byte changes."""
    profile = golden.profile
    settings = profile.app_config / "settings.toml"
    original = settings.read_text()
    damaged = DAMAGED_SETTINGS[damage](original)
    assert damaged != original
    settings.write_text(damaged)
    before = harness.snapshot(profile.home)

    gate = cli._run_graphical_startup_upgrade(require_legacy_safe=False, retry=None)
    service = cli.main(["--service-startup-prepare"])
    write = cli.main(["--write-config"])

    assert (gate, service, write) == (cli.EXIT_CONFIG,) * 3
    harness.check_changes(harness.diff(before, harness.snapshot(profile.home)), ())
    assert golden.processes == []
