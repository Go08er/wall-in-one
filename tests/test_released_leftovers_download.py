"""The fault harness on the case it was built after: AB-1, in downloads.

`tests/test_download_legacy_staging.py` replays the review's own recipes. Here
the released download is the producer under the harness's generic plans
(`tests.released_faults`): nothing in them knows which step to edit or which
read to fail. A day later this checkout opens the managed folder, as it
ordinarily does, and nothing the released code left has lost a byte.

The control is the same campaign with the consumer from just before the fix
(``59e6a5d``, where git has it): the harness reports the edited media file at
a staging name emptied by the sweep, which is AB-1. A campaign that finds
nothing in the other modules would have found this.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one.providers import download

PRODUCER = r"""
import hashlib
import os
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError

trace_filesystem((file_io, "_rename_noreplace"))
MEDIA = b"validated media bytes"


def operation():
    root = Path(os.environ["HOME"]) / "Wallpapers"
    location = download.MOTIONBGS_LOCATION
    disarm()
    directory, _marker = download.managed_directory(root, location)
    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=directory
    )
    with os.fdopen(descriptor, "wb") as sink:
        sink.write(MEDIA)
    destination = directory / "aurora.mp4"
    provenance = download.encode_sidecar(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "MotionBGS",
            "path": str(Path(os.fspath(destination))),
            "bytes": len(MEDIA),
            "sha256": hashlib.sha256(MEDIA).hexdigest(),
        }
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=len(MEDIA),
        path=staged,
    )
    arm()
    try:
        with transfer:
            download.install(transfer.path, destination, location.sidecar_suffix, provenance)
        print("DONE", flush=True)
    except ProviderError as error:
        print("REFUSED", error, flush=True)
    except OSError as error:
        print("ERROR", error, flush=True)


serve(operation)
"""

#: A day and an hour: past the sweep's wait.
LATER = download.STAGING_MAX_AGE_SECONDS + 3600
#: The commit AB-1 was found on, and the module its fix changed.
BEFORE_THE_FIX = "59e6a5d"
DOWNLOAD = "src/wall_in_one/providers/download.py"


def _prepare(base: Path) -> faults.Tree:
    for value in faults.profile_environment(base).values():
        Path(value).mkdir(parents=True, exist_ok=True)
    (Path(faults.profile_environment(base)["HOME"]) / "Wallpapers").mkdir()
    return faults.tree(base)


def _before_the_fix(tmp_path: Path) -> ModuleType:
    """`providers.download` as it was at `BEFORE_THE_FIX`, loaded beside this one."""
    repository = Path(__file__).resolve().parents[1]
    git = shutil.which("git")
    if git is None:
        raise OSError("git is not available")
    source = subprocess.run(
        [git, "-C", str(repository), "show", f"{BEFORE_THE_FIX}:{DOWNLOAD}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    path = tmp_path / "download_before_ab1.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("wall_in_one.providers.download_before_ab1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _campaign(
    consumer: ModuleType,
    released_source: Path,  # noqa: F811
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool = False,
) -> faults.Campaign:
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)

    def consume(profile: Path) -> Iterator[tuple[str, str]]:
        root = Path(faults.profile_environment(profile)["HOME"]) / "Wallpapers"
        old = time.time() - LATER
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))  # untouched for a day and an hour
        try:
            consumer.managed_directory(root, consumer.MOTIONBGS_LOCATION)
        except consumer.ProviderError as error:
            yield "open the folder a day later", f"REFUSED {error}"
        else:
            yield "open the folder a day later", "opened"

    return faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        (),
        base,
        prepare=_prepare,
        consume=consume,
        full=full,
    )


def test_nothing_a_released_download_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = _campaign(download, released_source, tmp_path, monkeypatch)

    print("\n" + found.summary("download"))
    assert any(
        any(".wall-in-one-media-stage" in name and size > 0 for name, size in state)
        for state in found.states
    ), "no state had media left at a released staging name"
    assert not found.findings, "\n".join(sorted(set(found.findings))[:40])


@faults.full_campaign
def test_nothing_a_released_download_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = _campaign(download, released_source, tmp_path, monkeypatch, full=True)

    print("\n" + found.summary("download, every plan"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:40])


def test_the_same_campaign_finds_ab1_in_the_code_before_its_fix(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. Skipped where git has no history (the gates' copy), or once
    that old module no longer loads against this checkout's other modules."""
    try:
        before = _before_the_fix(tmp_path)
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"no {BEFORE_THE_FIX} to compare against here: {error}")
    except (ImportError, AttributeError) as error:
        pytest.skip(f"{BEFORE_THE_FIX}'s download module no longer loads here: {error}")

    found = _campaign(before, released_source, tmp_path / "campaign", monkeypatch)

    emptied = [
        finding
        for finding in found.findings
        if finding.startswith("LOST") and ".wall-in-one-media-stage" in finding
    ]
    assert emptied, found.summary("download, before the fix")
