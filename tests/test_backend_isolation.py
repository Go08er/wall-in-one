"""The backend pool runs real work and never turns failure into a false pass."""

from __future__ import annotations

import operator
import subprocess
import sys
from collections.abc import Iterator
from concurrent import interpreters
from pathlib import Path

import pytest

from wall_in_one import backend
from wall_in_one.providers import backend_workers
from wall_in_one.providers.base import ProviderError


@pytest.fixture(autouse=True)
def clean_pool() -> Iterator[None]:
    backend.shutdown()
    yield
    backend.shutdown()


def test_one_executor_is_shared_process_wide() -> None:
    assert backend.executor() is backend.executor()


def test_work_executes_in_another_interpreter_and_returns_data() -> None:
    marker, worker_id = backend.run(backend_workers._probe, "completed")

    assert marker == "completed"
    assert worker_id != int(interpreters.get_current().id)


def test_an_unexpected_worker_exception_surfaces() -> None:
    future = backend.submit(operator.truediv, 1, 0)

    with pytest.raises(ZeroDivisionError):
        future.result()


def test_expected_provider_errors_cross_as_data_not_a_broken_pool() -> None:
    result = backend.run(
        backend_workers.motionbgs_listing,
        "<html><title>Just a moment...</title>",
        "latest",
        "",
        "",
        1,
        "https://motionbgs.com/",
        48,
    )

    with pytest.raises(ProviderError) as caught:
        backend_workers.value(result)
    assert caught.value.kind == "challenge"

    # The same executor still performs useful work after an expected failure.
    marker, _worker_id = backend.run(backend_workers._probe, "still-running")
    assert marker == "still-running"


def test_the_measured_listing_parser_really_completes_in_the_pool() -> None:
    cards = "".join(
        f"""
        <a href="/frog-{index}/" title="Frog {index} Live Wallpaper 4K">
          <img src="https://motionbgs.com/media/preview/frog-{index}.jpg">
          <span class="ttl">Frog {index}</span>
          <span class="frm">4K</span>
        </a>
        """
        for index in range(36)
    )
    markup = (
        "<!doctype html><html><head><title>36+ 4K Live Wallpapers</title>"
        f"</head><body>{cards}</body></html>"
    )

    parsed = backend_workers.value(
        backend.run(
            backend_workers.motionbgs_listing,
            markup,
            "4k",
            "",
            "",
            1,
            "https://motionbgs.com/4k/",
            48,
        )
    )

    assert len(parsed.items) == 36
    assert parsed.items[0].identifier == "frog-0"


def test_a_late_added_package_path_is_reestablished_in_workers(tmp_path: Path) -> None:
    """Match wrappers which add the application site directory after startup."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
import site
import sys

site.addsitedir(sys.argv[1])

from wall_in_one import backend
from wall_in_one.providers import backend_workers

markup = '''
<!doctype html><html><head><title>1+ Live Wallpapers</title></head><body>
  <a href="/frog/" title="Frog Live Wallpaper 4K">
    <img src="https://motionbgs.com/media/preview/frog.jpg">
    <span class="ttl">Frog</span>
    <span class="frm">4K</span>
  </a>
</body></html>
'''
detail_markup = '''
<!doctype html><html><head>
  <title>Frog Live Wallpaper</title>
  <meta property="og:title" content="Frog Live Wallpaper">
  <meta property="og:image" content="https://motionbgs.com/media/42/frog.jpg">
  <meta property="og:video" content="https://motionbgs.com/media/42/frog.mp4">
</head><body>
  <a href="/dl/hd/42/">HD 1920x1080 (12.5 MB)</a>
  <a href="/dl/4k/42/">4K 3840x2160 (48.0 MB)</a>
  <script type="application/ld+json">{"duration":"PT10S"}</script>
</body></html>
'''
routes = (
    ("latest", "", "", "https://motionbgs.com/"),
    ("4k", "", "", "https://motionbgs.com/4k/"),
    ("hd", "", "", "https://motionbgs.com/hd/"),
    ("genre", "", "nature", "https://motionbgs.com/tag:nature/"),
    ("search", "frog", "", "https://motionbgs.com/search?q=frog"),
)
try:
    for mode, query, genre, source_url in routes:
        result = backend_workers.value(
            backend.run(
                backend_workers.motionbgs_listing,
                markup,
                mode,
                query,
                genre,
                1,
                source_url,
                48,
            )
        )
        assert result.items[0].identifier == "frog"
    detail = backend_workers.value(
        backend.run(backend_workers.motionbgs_detail, detail_markup, "frog")
    )
    assert detail.slug == "frog"
    assert detail.media_id == "42"
    assert [option.quality for option in detail.downloads] == ["4k", "hd"]
finally:
    backend.shutdown()

print("late package path reached every isolated MotionBGS worker")
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(source_root)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "late package path reached every isolated MotionBGS worker"
