"""Live colours for auxiliary windows that must not open the authoring app."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib

from wall_in_one import config, paths
from wall_in_one.theme import source


@dataclass(frozen=True)
class _Result:
    generation: int
    palette: source.ResolvedPalette | None
    opacity: float
    watch_directories: tuple[Path, ...]
    error: str = ""


class ReadOnlyPalette:
    """One cancellable read lane, coalesced notifications, no profile writes."""

    def __init__(self, apply: Callable[[source.ResolvedPalette, float], None]) -> None:
        self._apply = apply
        self._cancelled = threading.Event()
        self._pool: ThreadPoolExecutor | None = None
        self._active = False
        self._watch_ready = False
        self._generation = 0
        self._debounce = 0
        self._monitors: dict[Path, Gio.FileMonitor] = {}
        self._targets = (
            paths.palette_path(),
            paths.noctalia_settings_path(),
            paths.settings_path(),
        )

    def start(self) -> None:
        self._apply(source.fixed(), 1.0)
        self.reload()

    def reload(self) -> None:
        if self._cancelled.is_set():
            return
        self._generation += 1
        if self._debounce:
            GLib.source_remove(self._debounce)
            self._debounce = 0
        if self._active:
            return
        self._active = True
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notice-colours")
        self._pool.submit(self._read, self._generation, not self._watch_ready)

    def _read(self, generation: int, prepare_only: bool) -> None:
        directories: set[Path] = set()
        palette = None
        opacity = 1.0
        error = ""
        try:
            # Discover existing ancestors on this worker. Missing paths are
            # watched as they appear; the notice never creates a directory.
            for target in self._targets:
                parent = target.parent
                while not parent.is_dir() and parent != parent.parent:
                    parent = parent.parent
                directories.add(parent)
            if not prepare_only and not self._cancelled.is_set():
                settings = config.load_strict()
                opacity = settings.opacity
                palette = (
                    source.resolve(scheme=settings.preview_scheme, cancelled=self._cancelled.is_set)
                    if settings.follow_noctalia_palette
                    else source.fixed()
                )
        except Exception as caught:
            # Unknown/newer settings must not be rewritten or normalized just
            # to paint a notice. Keep its last usable colours instead.
            error = str(caught)
        if not self._cancelled.is_set():
            GLib.idle_add(
                self._finish,
                _Result(generation, palette, opacity, tuple(sorted(directories)), error),
            )

    def _finish(self, result: _Result) -> bool:
        self._active = False
        if self._cancelled.is_set():
            return GLib.SOURCE_REMOVE
        self._watch(result.watch_directories)
        if not self._watch_ready:
            # Install watches before the first palette read; otherwise a
            # settings replacement during initial resolution could be missed.
            self._watch_ready = True
            self.reload()
            return GLib.SOURCE_REMOVE
        if result.generation != self._generation:
            self.reload()
            return GLib.SOURCE_REMOVE
        error = result.error
        if result.palette is not None:
            try:
                self._apply(result.palette, result.opacity)
            except Exception as caught:
                # A generated palette can parse but still lack a CSS token.
                # Keep following future changes without leaking an exception
                # from this main-loop callback or rewriting any settings.
                error = str(caught)
        if error:
            print(f"warning: update notice keeps its current colours: {error}", file=sys.stderr)
        return GLib.SOURCE_REMOVE

    def _watch(self, directories: tuple[Path, ...]) -> None:
        for path in tuple(self._monitors):
            if path not in directories:
                self._monitors.pop(path).cancel()
        for path in directories:
            if path in self._monitors:
                continue
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
            except GLib.Error:
                continue
            monitor.connect("changed", self._changed)
            self._monitors[path] = monitor

    def _changed(
        self,
        _monitor: Gio.FileMonitor,
        changed: Gio.File,
        other: Gio.File | None,
        _event: Gio.FileMonitorEvent,
    ) -> None:
        names = {changed.get_path(), other.get_path() if other is not None else None}
        relevant = {str(path) for target in self._targets for path in (target, *target.parents)}
        if not names.intersection(relevant) or self._cancelled.is_set():
            return
        # Invalidate an in-flight answer immediately, not only after debounce.
        self._generation += 1
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(75, self._after_change)

    def _after_change(self) -> bool:
        self._debounce = 0
        self.reload()
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        self._cancelled.set()
        if self._debounce:
            GLib.source_remove(self._debounce)
            self._debounce = 0
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
