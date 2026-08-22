#!/usr/bin/env python3
"""Offline production-widget RSS probe for the Browse result surface.

Run under a disposable display; no provider request or thumbnail download is
made::

    nix shell nixpkgs#xvfb-run --command \
      xvfb-run -a nix develop --command python benchmarks/browse_rss.py

The useful contracts are printed alongside RSS: 600 retained candidates,
never more than ``MAX_MATERIALIZED_RESULTS`` live cards, and result 600 reached.
RSS is allocator- and GTK-theme-dependent, so this is a benchmark rather than a
test-suite threshold.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Browser credential/ownership discovery must not inspect the invoking user's
# files even though this probe never performs a provider request.
_sandbox = tempfile.TemporaryDirectory(prefix="wall-in-one-browse-rss-")
for _name in ("CONFIG", "STATE", "CACHE"):
    os.environ[f"XDG_{_name}_HOME"] = str(Path(_sandbox.name) / _name.lower())

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.providers.base import SearchResult, WallpaperCandidate  # noqa: E402
from wall_in_one.ui.browse_dialog import (  # noqa: E402
    MAX_MATERIALIZED_RESULTS,
    BrowsePage,
)


class _App:
    settings = SimpleNamespace(roots=(Path(_sandbox.name) / "library",))

    def refresh_library(self) -> None:
        pass


def _rss_mib() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    raise RuntimeError("/proc/self/status did not report VmRSS")


def _settle() -> None:
    context = GLib.MainContext.default()
    for _ in range(20):
        while context.pending():
            context.iteration(False)


def main() -> None:
    Adw.init()
    page = BrowsePage(_App())  # type: ignore[arg-type]
    window = Gtk.Window(child=page, default_width=1000, default_height=760)
    window.present()
    _settle()
    baseline = _rss_mib()

    candidates = tuple(
        WallpaperCandidate(
            provider="offline",
            identifier=f"item-{index:04d}",
            title=f"Offline wallpaper {index:04d}",
            kind=Kind.STILL,
            page_url=f"https://example.invalid/item-{index:04d}",
        )
        for index in range(600)
    )
    surface = page._surface
    surface._show_result(
        SearchResult(
            provider="offline",
            query_url="https://example.invalid/offline",
            items=candidates,
            page=1,
            has_next=False,
            total_hint=600,
        ),
        page=1,
    )
    _settle()
    samples = [_rss_mib()]
    live_counts = [len(surface._cards)]
    while surface._result_page + 1 < len(surface._result_pages):
        surface._show_next_page()
        _settle()
        samples.append(_rss_mib())
        live_counts.append(len(surface._cards))

    last = surface._cards[-1].candidate.identifier
    print(f"baseline_rss_mib={baseline:.1f}")
    print(f"peak_600_results_rss_mib={max(samples):.1f}")
    print(f"peak_delta_mib={max(samples) - baseline:.1f}")
    print(f"retained_candidates={len(surface._candidates)}")
    print(f"maximum_live_cards={max(live_counts)} (contract {MAX_MATERIALIZED_RESULTS})")
    print(f"last_result={last}")
    if max(live_counts) > MAX_MATERIALIZED_RESULTS or last != "item-0599":
        raise SystemExit("Browse paging contract failed")

    page.shutdown()
    window.destroy()
    _sandbox.cleanup()


if __name__ == "__main__":
    main()
