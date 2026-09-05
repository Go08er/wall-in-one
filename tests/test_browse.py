"""The layer between the window and the providers: selection, roots, downloads."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.test_providers_fakes import FakeClient, Reply, png_bytes
from wall_in_one import browse, file_io
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library import owned
from wall_in_one.library.model import Kind
from wall_in_one.providers import base, wallhaven
from wall_in_one.providers.base import (
    DownloadResult,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)


def candidate(
    provider: str = "wallhaven", *, thumbnail: str = "", identifier: str = "ab1234"
) -> WallpaperCandidate:
    return WallpaperCandidate(
        provider=provider,
        identifier=identifier,
        title="a wallpaper",
        kind=Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}",
        thumbnail_url=thumbnail,
    )


def browser(routes: dict[str, Reply | list[Reply]] | None = None, **kwargs: object) -> Browser:
    return Browser(client=FakeClient(routes=routes or {}), **kwargs)  # type: ignore[arg-type]


# -- providers -----------------------------------------------------------


def test_every_provider_is_described_without_a_network(tmp_path: Path) -> None:
    infos = browser(root=tmp_path).available
    assert {info.name for info in infos} == {"motionbgs", "wallhaven"}
    assert all(info.usable for info in infos)


def test_a_provider_is_built_once_and_kept() -> None:
    """The provider owns the search cache, so rebuilding it would drop the cache."""
    engine = browser()
    assert engine.provider("wallhaven") is engine.provider("wallhaven")


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ProviderError) as caught:
        browser().provider("nowhere")
    assert caught.value.kind == "unknown-provider"


# -- the download root ---------------------------------------------------


def test_an_explicit_root_wins(tmp_path: Path) -> None:
    assert browser(root=tmp_path).download_root() == tmp_path


def test_an_unconfigured_root_is_an_error_even_when_noctalia_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection feeds the first-run prompt, never a silent write target."""
    monkeypatch.setattr("wall_in_one.library.scan.default_roots", lambda: (tmp_path,))
    with pytest.raises(ProviderError) as caught:
        browser().download_root()
    assert caught.value.kind == "no-root"
    assert "Settings" in str(caught.value)


def test_reconfiguring_roots_retargets_downloads_and_owned_index(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    engine = browser(root=first, library_roots=(first,))

    old_index = engine.owned
    engine.configure_roots(root=second, library_roots=(second,))

    assert engine.download_root() == second
    assert engine.owned is not old_index


def test_cached_ownership_never_rebuilds_on_the_callers_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = browser()
    snapshot = owned.Index()
    reads: list[object] = []

    def read(roots: object) -> owned.Index:
        reads.append(roots)
        return snapshot

    monkeypatch.setattr(owned, "read", read)
    before = engine.cached_owned
    assert before is None
    assert reads == []
    assert engine.owned is snapshot
    ready = engine.cached_owned
    assert ready is snapshot
    engine.forget_owned()
    invalidated = engine.cached_owned
    assert invalidated is None
    assert len(reads) == 1


def test_library_invalidation_cannot_publish_an_older_ownership_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = browser()
    started = threading.Event()
    release = threading.Event()
    old, current = owned.Index(), owned.Index()
    calls = 0

    def read(_roots: object) -> owned.Index:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
            return old
        return current

    monkeypatch.setattr(owned, "read", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: engine.owned)
        try:
            assert started.wait(2)
            engine.forget_owned()
        finally:
            release.set()
        assert future.result(timeout=2) is current
    assert engine.cached_owned is current
    assert calls == 2


def test_a_download_finishing_after_a_root_change_does_not_pollute_the_new_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    engine = browser(root=first, library_roots=(first,))
    old_index = engine.owned
    wanted = candidate()
    installed = DownloadResult(
        provider="wallhaven",
        identifier=wanted.identifier,
        path=first / "Wall-in-One" / "Wallhaven" / "wallhaven-ab1234.png",
        sidecar=first / "s.json",
        marker=first / "m.json",
        kind=Kind.STILL,
        size=3,
        source_url=wanted.page_url,
        download_url="https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png",
        sha256="0" * 64,
        downloaded_at="2026-01-01T00:00:00Z",
    )

    class Stub:
        def download(self, _candidate: object, root: Path, *, variant: str = "") -> DownloadResult:
            assert root == first
            engine.configure_roots(root=second, library_roots=(second,))
            return installed

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())

    done = engine.download(wanted)

    assert done.root == first
    assert not done.root_current
    assert str(first) in done.describe()
    assert "folders changed mid-download" in done.describe()
    assert not old_index.holds(wanted)
    assert not engine.owned.holds(wanted)


def test_a_download_reports_where_to_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = DownloadResult(
        provider="wallhaven",
        identifier="ab1234",
        path=tmp_path / "Wall-in-One" / "Wallhaven" / "wallhaven-ab1234.png",
        sidecar=tmp_path / "s.json",
        marker=tmp_path / "m.json",
        kind=Kind.STILL,
        size=3 * 1024 * 1024,
        source_url="https://wallhaven.cc/w/ab1234",
        download_url="https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png",
        sha256="0" * 64,
        downloaded_at="2026-01-01T00:00:00Z",
    )

    engine = browser(root=tmp_path)

    class Stub:
        def download(self, _candidate: object, root: Path, *, variant: str = "") -> DownloadResult:
            assert root == tmp_path
            assert variant == "4k"
            return installed

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())

    done = engine.download(candidate(), variant="4k")

    assert isinstance(done, Downloaded)
    assert done.root == tmp_path
    assert done.result.path.name == "wallhaven-ab1234.png"
    assert done.describe() == "downloaded wallhaven-ab1234.png (3.0 MB)"


def _record_download(root: Path, wanted: WallpaperCandidate) -> DownloadResult:
    provider_title = "MotionBGS" if wanted.provider == "motionbgs" else "Wallhaven"
    suffix = ".motionbgs.json" if wanted.provider == "motionbgs" else ".wallhaven.json"
    directory = root / "Wall-in-One" / provider_title
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"wallhaven-{wanted.identifier}.png"
    contents = b"png"
    path.write_bytes(contents)
    fingerprint = file_io.regular_file_fingerprint(path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "goober/wall-in-one",
                "provider": provider_title,
                "id": wanted.identifier,
                "path": str(path),
                "source_page": wanted.page_url,
                "bytes": len(contents),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "media_generation": {
                    "device": fingerprint[0],
                    "inode": fingerprint[1],
                    "bytes": fingerprint[2],
                    "mtime_ns": fingerprint[3],
                    "ctime_ns": fingerprint[4],
                },
            }
        ),
        encoding="utf-8",
    )
    return DownloadResult(
        provider=wanted.provider,
        identifier=wanted.identifier,
        path=path,
        sidecar=sidecar,
        marker=directory / ".marker",
        kind=Kind.STILL,
        size=path.stat().st_size,
        source_url=wanted.page_url,
        download_url="https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png",
        sha256=hashlib.sha256(contents).hexdigest(),
        downloaded_at="2026-01-01T00:00:00Z",
    )


def test_repeat_download_rechecks_provenance_under_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    engine = browser(root=root)
    wanted = candidate(provider="motionbgs")
    calls: list[str] = []

    class Stub:
        def download(
            self, selected: WallpaperCandidate, destination: Path, *, variant: str = ""
        ) -> DownloadResult:
            calls.append(variant)
            return _record_download(destination, selected)

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())

    engine.download(wanted, variant="hd")
    with pytest.raises(ProviderError) as caught:
        # Variant is deliberately not part of ownership: the UI treats one
        # provider/id as one library item, so 4K cannot silently duplicate HD.
        engine.download(wanted, variant="4k")

    assert caught.value.kind == "conflict"
    assert calls == ["hd"]


def test_download_provenance_snapshot_always_includes_its_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root switch cannot mix the new destination with the old inventory."""
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    wanted = candidate()
    existing = _record_download(new_root, wanted)
    # This deliberately models the inconsistent pair the old two-lock read
    # could observe while Settings switched folders: destination B with roots
    # from A.  Browser must still inspect the actual destination under B's
    # provider/id claim before contacting the provider.
    engine = browser(root=new_root, library_roots=(old_root,))
    contacted = False

    def provider(_name: str) -> object:
        nonlocal contacted
        contacted = True
        raise AssertionError("duplicate destination reached the provider")

    monkeypatch.setattr(engine, "provider", provider)

    with pytest.raises(ProviderError) as caught:
        engine.download(wanted)

    assert caught.value.kind == "conflict"
    assert str(existing.path) in str(caught.value)
    assert not contacted


def test_identifier_only_control_candidate_matches_a_bound_motionbgs_sidecar_without_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    wanted = WallpaperCandidate(
        provider="motionbgs",
        identifier="rainy-night",
        title="",
        kind=Kind.VIDEO,
        page_url=browse.source_page_url("motionbgs", "rainy-night"),
    )
    result = _record_download(root, wanted)
    document = json.loads(result.sidecar.read_text(encoding="utf-8"))
    document.pop("id")
    result.sidecar.write_text(json.dumps(document), encoding="utf-8")
    engine = browser(root=root)
    contacted = False

    def provider(_name: str) -> object:
        nonlocal contacted
        contacted = True
        raise AssertionError("legacy duplicate reached the provider")

    monkeypatch.setattr(engine, "provider", provider)

    with pytest.raises(ProviderError) as caught:
        engine.download(wanted, variant="4k")

    assert caught.value.kind == "conflict"
    assert not contacted


def test_an_unbound_predecessor_sidecar_does_not_block_a_fresh_provider_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    wanted = candidate(provider="motionbgs")
    result = _record_download(root, wanted)
    document = json.loads(result.sidecar.read_text(encoding="utf-8"))
    document.pop("media_generation")
    result.sidecar.write_text(json.dumps(document), encoding="utf-8")
    engine = browser(root=root)
    contacted = False

    class Stub:
        def download(
            self, selected: WallpaperCandidate, destination: Path, *, variant: str = ""
        ) -> DownloadResult:
            nonlocal contacted
            del selected, destination, variant
            contacted = True
            raise ProviderError("conflict", "provider found the predecessor path")

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())

    with pytest.raises(ProviderError) as caught:
        engine.download(wanted, variant="4k")

    assert caught.value.kind == "conflict"
    assert contacted


def test_concurrent_download_of_one_candidate_is_nonblocking_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    engine = browser(root=root)
    wanted = candidate()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class Stub:
        def download(
            self, selected: WallpaperCandidate, destination: Path, *, variant: str = ""
        ) -> DownloadResult:
            nonlocal calls
            del variant
            calls += 1
            entered.set()
            assert release.wait(2)
            return _record_download(destination, selected)

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(engine.download, wanted)
    assert entered.wait(1)
    second = pool.submit(engine.download, wanted)

    with pytest.raises(ProviderError) as caught:
        second.result(timeout=1)
    release.set()
    first.result(timeout=2)
    pool.shutdown(wait=True, cancel_futures=True)

    assert caught.value.kind == "busy"
    assert calls == 1


def test_root_switch_cannot_start_the_same_candidate_in_a_second_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    roots = (first_root, second_root)
    first_browser = browser(root=first_root, library_roots=roots)
    second_browser = browser(root=second_root, library_roots=roots)
    wanted = candidate()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class Stub:
        def download(
            self, selected: WallpaperCandidate, destination: Path, *, variant: str = ""
        ) -> DownloadResult:
            nonlocal calls
            del variant
            calls += 1
            entered.set()
            assert release.wait(2)
            return _record_download(destination, selected)

    provider = Stub()
    monkeypatch.setattr(first_browser, "provider", lambda _name: provider)
    monkeypatch.setattr(second_browser, "provider", lambda _name: provider)
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(first_browser.download, wanted)
    assert entered.wait(1)
    second = pool.submit(second_browser.download, wanted)

    with pytest.raises(ProviderError) as caught:
        second.result(timeout=1)
    release.set()
    result = first.result(timeout=2)
    pool.shutdown(wait=True, cancel_futures=True)

    assert caught.value.kind == "busy"
    assert calls == 1
    assert result.root == first_root
    assert not any(second_root.iterdir())


def test_download_claim_excludes_a_second_process(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    script = """
import sys, time
from pathlib import Path
from wall_in_one.browse import _download_claim
root, ready, release = map(Path, sys.argv[1:])
with _download_claim(root, 'wallhaven', 'ab1234'):
    ready.write_text('ready', encoding='utf-8')
    while not release.exists():
        time.sleep(0.01)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), child.communicate(timeout=1)

        with (
            pytest.raises(ProviderError) as caught,
            browse._download_claim(root, "wallhaven", "ab1234"),
        ):
            pytest.fail("a second process entered the same download claim")
        assert caught.value.kind == "busy"
    finally:
        release.touch()
        stdout, stderr = child.communicate(timeout=2)
        assert child.returncode == 0, (stdout, stderr)


# -- thumbnails ----------------------------------------------------------


def test_a_candidate_without_a_thumbnail_is_not_a_failure() -> None:
    assert browser().thumbnail(candidate()) == b""


def test_a_thumbnail_is_fetched_bounded() -> None:
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    body = png_bytes(4, 3)
    client = FakeClient(routes={url: Reply(body=body, content_type="image/png")})
    engine = Browser(client=client)

    assert engine.thumbnail(candidate(thumbnail=url)) == body
    assert client.requests[0].max_bytes <= 4 * 1024 * 1024
    assert client.requests[0].timeout > 0


def test_a_thumbnail_that_is_not_an_image_is_discarded() -> None:
    """An error page rendered into a Gtk.Picture is worse than no picture."""
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    client = FakeClient(routes={url: Reply(body=b"<html>nope", content_type="text/html")})

    assert Browser(client=client).thumbnail(candidate(thumbnail=url)) == b""


def test_a_thumbnail_redirect_is_discarded() -> None:
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    client = FakeClient(routes={url: Reply(status=302, location="https://evil.example/x.png")})

    assert Browser(client=client).thumbnail(candidate(thumbnail=url)) == b""


def test_a_plaintext_thumbnail_url_never_opens_a_socket() -> None:
    client = FakeClient(routes={})
    with pytest.raises(ProviderError):
        Browser(client=client).thumbnail(candidate(thumbnail="http://th.wallhaven.cc/x.jpg"))
    assert client.requests == []


# -- search --------------------------------------------------------------


def test_search_is_delegated_to_the_named_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = browser()
    seen: list[SearchQuery] = []
    answer = SearchResult(
        provider="wallhaven", query_url="https://wallhaven.cc/api/v1/search", items=()
    )

    class Stub:
        def search(self, query: SearchQuery) -> SearchResult:
            seen.append(query)
            return answer

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())
    assert engine.search("wallhaven", SearchQuery(text="aurora")) is answer
    assert seen[0].text == "aurora"


# -- filters -------------------------------------------------------------
#
# The dialog reads its widgets into a `Filters` and asks for a query back.
# What is worth pinning is the conditional parts: Wallhaven refuses a seed
# outside random sorting rather than ignoring it, so a control that always
# sent one would turn changing the sort order into an error message.


def test_default_filters_ask_for_nothing_unusual() -> None:
    query = browse.Filters(text="mountain").to_query(browse.WALLHAVEN)
    assert query.text == "mountain"
    assert query.options == {
        "sorting": "date_added",
        "order": "desc",
        "categories": "111",
        "purity": "100",
    }


def test_the_optional_wallhaven_filters_appear_only_when_set() -> None:
    filters = browse.Filters(atleast="1920x1080", ratios="16x9", colour="0066cc")
    options = filters.to_query(browse.WALLHAVEN).options
    assert options["atleast"] == "1920x1080"
    assert options["ratios"] == "16x9"
    assert options["colors"] == "0066cc"


def test_the_top_range_is_sent_only_for_the_toplist() -> None:
    """Wallhaven ignores it elsewhere, so sending it would misdescribe the URL."""
    assert "top_range" not in browse.Filters(sorting="hot").to_query(browse.WALLHAVEN).options
    ranged = browse.Filters(sorting="toplist", top_range="1w").to_query(browse.WALLHAVEN)
    assert ranged.options["top_range"] == "1w"


def test_a_seed_is_generated_for_random_sorting() -> None:
    """Without one, page two of a random search overlaps page one."""
    filters = browse.Filters(sorting="random").seeded()
    assert len(filters.seed) == browse.SEED_LENGTH
    assert filters.seed.isalnum()
    assert filters.to_query(browse.WALLHAVEN).options["seed"] == filters.seed


def test_an_existing_seed_is_kept_so_paging_stays_put() -> None:
    filters = browse.Filters(sorting="random", seed="abc123")
    assert filters.seeded().seed == "abc123"


def test_a_stale_seed_is_dropped_when_the_sorting_changes() -> None:
    """Wallhaven refuses a seed outside random sorting, rather than ignoring it.

    So a user who searches randomly and then switches to "Top list" would get
    a validation error instead of results.
    """
    filters = browse.Filters(sorting="toplist", seed="abc123").seeded()
    assert filters.seed == ""
    assert "seed" not in filters.to_query(browse.WALLHAVEN).options


def test_seeds_differ_between_searches() -> None:
    seeds = {browse.new_seed() for _ in range(20)}
    assert len(seeds) > 1


def test_motionbgs_gets_its_own_shape() -> None:
    options = browse.Filters(mode="genre", genre="anime").to_query(browse.MOTIONBGS).options
    assert options == {"mode": "genre", "genre": "anime"}


def test_typing_a_query_overrides_the_motionbgs_browse_mode() -> None:
    """MotionBGS rejects a query and a browse mode together."""
    options = browse.Filters(text="kakashi", mode="latest").to_query(browse.MOTIONBGS).options
    assert options["mode"] == "search"


def test_wallhaven_accepts_every_option_the_filters_produce() -> None:
    """The two halves have to agree, and only one of them is in this module.

    `WallhavenFilters.from_query` refuses an unknown option outright, so a
    name misspelled here would fail at the website rather than in a test.
    """
    filters = browse.Filters(
        text="forest",
        sorting="random",
        order="asc",
        atleast="2560x1440",
        ratios="16x9,21x9",
        colour="336600",
    ).seeded()
    parsed = wallhaven.WallhavenFilters.from_query(
        filters.to_query(browse.WALLHAVEN), authenticated=False
    )
    assert parsed.seed == filters.seed
    assert parsed.colors == "336600"
    assert parsed.ratios == "16x9,21x9"
    assert parsed.order == "asc"


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, ""),
        (-1, ""),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (int(1.5 * 1024 * 1024), "1.5 MiB"),
        (200 * 1024 * 1024, "200 MiB"),
        (3 * 1024**3, "3.0 GiB"),
    ],
)
def test_file_sizes_read_the_way_a_file_manager_shows_them(size: int, expected: str) -> None:
    """Binary units, so the two do not disagree about the same file."""
    assert base.human_bytes(size) == expected
