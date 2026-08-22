"""The backend pool runs real work and never turns failure into a false pass."""

from __future__ import annotations

import operator
from collections.abc import Iterator
from concurrent import interpreters

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
