"""Settings page and compatibility dialog.

Everything that used to be the whole window lives here now, plus the four
controls that previously existed only as `ctl` verbs: shuffle, cycle, cycle
interval, and dynamics.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import config, display_policy
from wall_in_one.library import scan
from wall_in_one.providers import credentials, registry
from wall_in_one.providers.base import ProviderError
from wall_in_one.theme import source
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.theme.palette import Palette
from wall_in_one.ui import runtime_truth
from wall_in_one.ui.palette_browser import STRIP_TOKENS, swatch
from wall_in_one.wallpaper import renderer, scenes

#: mpvpaper's vocabulary is `--auto-pause` and `--auto-stop`, which say what
#: the flag does rather than what the user gets. These say what they get.
_WHEN_HIDDEN_LABELS: dict[str, str] = {
    "pause": "Pause (resumes instantly)",
    "stop": "Stop (frees memory too)",
    "play": "Keep playing",
}

_INTERPOLATION_LABELS: dict[str, str] = {
    "off": "Off (source cadence)",
    "oversample": "Oversample (recommended)",
    "linear": "Linear (stronger blending)",
}


def _connected_outputs() -> tuple[str, ...]:
    """Connector names of the monitors attached right now, in GTK's order."""
    display = Gdk.Display.get_default()
    if display is None:
        return ()
    monitors = display.get_monitors()
    found = []
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        connector = monitor.get_connector() if isinstance(monitor, Gdk.Monitor) else None
        if connector:
            found.append(connector)
    return tuple(found)


if TYPE_CHECKING:
    from wall_in_one.ui.app import Application


class PreferencesPage(Adw.PreferencesPage):
    """Playback, colour, and appearance settings."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self._app = application
        # Set while widgets are being populated from settings, so that
        # programmatic changes do not read as user edits and write back.
        self._loading = False

        self.add(self._build_library_group())
        self.add(self._build_playback_group())
        self.add(self._build_providers_group())
        self.add(self._build_colour_group())
        self.add(self._build_appearance_group())

        self._load(application.settings)
        self._watch_output_changes()
        self._refresh_roots()
        self._refresh_api_key_status()
        self.show_palette(application.resolved_palette)

    # -- construction ----------------------------------------------------

    def _build_library_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Library",
            description=(
                "Folders scanned for wallpapers. Downloads and generated stills "
                "go into the first one; Wall-in-One never chooses it silently."
            ),
        )
        add = Gtk.Button(icon_name="folder-new-symbolic", tooltip_text="Add a folder")
        add.set_valign(Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", self._on_add_root)
        group.set_header_suffix(add)

        # Rebuilt wholesale on every change: a handful of rows, and tracking
        # which one moved would be more code than making them again.
        self._roots_group = group
        self._root_rows: list[Gtk.Widget] = []

        self._workshop = Adw.SwitchRow(
            title="Scan Wallpaper Engine library",
            subtitle=("Show installed Steam Workshop wallpapers; Wall-in-One never modifies them"),
        )
        self._workshop.connect("notify::active", self._on_changed)
        group.add(self._workshop)
        return group

    def _refresh_roots(self) -> None:
        for row in self._root_rows:
            self._roots_group.remove(row)
        self._root_rows = []

        roots = self._app.settings.roots
        if not roots:
            row = Adw.ActionRow(
                title="Not configured",
                subtitle=self._noctalia_root_subtitle(),
            )
            self._roots_group.add(row)
            self._root_rows.append(row)
            return

        for index, root in enumerate(roots):
            row = Adw.ActionRow(title=root.name or str(root), subtitle=str(root))
            if index == 0:
                row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
                row.set_tooltip_text("Downloads and generated stills go here")
            else:
                destination = Gtk.Button(
                    icon_name="folder-download-symbolic",
                    tooltip_text="Make this the download and generated-still folder",
                )
                destination.set_valign(Gtk.Align.CENTER)
                destination.add_css_class("flat")
                destination.connect("clicked", self._make_root_primary(root))
                row.add_suffix(destination)
            if not root.is_dir():
                # Said plainly rather than dropped: a folder on a drive that is
                # not mounted should come back when it is, not disappear.
                row.set_subtitle(f"{root} -- not there right now")
                row.add_css_class("warning")
            remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove")
            remove.set_valign(Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", self._make_root_remover(root))
            row.add_suffix(remove)
            self._roots_group.add(row)
            self._root_rows.append(row)

    def _noctalia_root_subtitle(self) -> str:
        found = scan.default_roots()
        return (
            f"Suggested folder: {found[0]}"
            if found
            else "Choose a folder before downloading or generating stills"
        )

    def _make_root_remover(self, root: Path) -> Any:
        def remove(_button: Gtk.Button) -> None:
            self._set_roots(tuple(r for r in self._app.settings.roots if r != root))

        return remove

    def _make_root_primary(self, root: Path) -> Any:
        def make_primary(_button: Gtk.Button) -> None:
            roots = self._app.settings.roots
            self._set_roots((root, *(candidate for candidate in roots if candidate != root)))

        return make_primary

    def _set_roots(self, roots: tuple[Path, ...]) -> None:
        try:
            self._app.update_settings(roots=roots)
        except config.ConfigError as error:
            self.apply_settings(self._app.settings)
            self._report(f"Library folders were not saved; nothing changed: {error}")
            return
        self._refresh_roots()

    def _on_add_root(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Add a wallpaper folder", modal=True)
        dialog.select_folder(self._window_for_dialog(), None, self._on_root_chosen)

    def _window_for_dialog(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _on_root_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            chosen = dialog.select_folder_finish(result)
        except GLib.Error:
            # The only realistic error here is the user dismissing the chooser,
            # and a toast saying so would be noise.
            return
        path = chosen.get_path() if chosen is not None else None
        if path is None:
            self._report("That folder is not on this machine's filesystem")
            return
        added = Path(path)
        if added in self._app.settings.roots:
            self._report(f"{added.name} is already in the library")
            return
        self._set_roots((*self._app.settings.roots, added))
        self._report(f"Scanning {added.name}")

    def _build_playback_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Playback defaults",
            description=(
                "Saved defaults for the service. Use the header playback menu "
                "to change cycle, shuffle, pause or stop for this running session."
            ),
        )

        self._shuffle = Adw.SwitchRow(
            title="Shuffle by default",
            subtitle="Initial order when the service starts",
        )
        self._shuffle.connect("notify::active", self._on_changed)
        group.add(self._shuffle)

        self._cycle = Adw.SwitchRow(
            title="Cycle by default",
            subtitle="Initial timer state when the service starts",
        )
        self._cycle.connect("notify::active", self._on_changed)
        group.add(self._cycle)

        self._interval = Adw.SpinRow(
            title="Cycle interval",
            subtitle="Seconds between changes",
            adjustment=Gtk.Adjustment(lower=5, upper=24 * 60 * 60, step_increment=30, value=300),
        )
        self._interval.connect("notify::value", self._on_changed)
        group.add(self._interval)

        self._dynamics = Adw.SwitchRow(
            title="Dynamics",
            subtitle="Play video wallpapers. Off shows their paired stills instead",
        )
        self._dynamics.connect("notify::active", self._on_changed)
        group.add(self._dynamics)

        self._own_scenes = Adw.SwitchRow(
            title="Play Wallpaper Engine scenes",
            subtitle=(
                "Starts linux-wallpaperengine only when no other process owns the target output"
            ),
        )
        self._own_scenes.connect("notify::active", self._on_changed)
        group.add(self._own_scenes)

        scene_available = scenes.is_available()
        scene_status = Adw.ActionRow(
            title="Wallpaper Engine renderer",
            subtitle=(
                "linux-wallpaperengine is available"
                if scene_available
                else (
                    "linux-wallpaperengine was not found. Install the Nix package or launch "
                    "Wall-in-One from its packaged wrapper."
                )
            ),
        )
        scene_status.add_prefix(
            Gtk.Image(
                icon_name=("emblem-ok-symbolic" if scene_available else "dialog-warning-symbolic")
            )
        )
        group.add(scene_status)

        self._display_mode = Adw.ComboRow(
            title="Displays",
            subtitle=(
                "Same wallpaper everywhere is lighter; independent mode unlocks connector "
                "assignments, schedules and separate Quick choices"
            ),
            model=Gtk.StringList.new(
                ["Same wallpaper on every display", "Control displays independently"]
            ),
        )
        self._display_mode.connect("notify::selected", self._on_changed)
        group.add(self._display_mode)

        self._theme_connectors: tuple[str, ...] = ()
        self._theme_attached: frozenset[str] = frozenset()
        self._theme_source = Adw.ComboRow(
            title="Colours follow",
            subtitle="Choose which display drives Noctalia's one shell-wide palette",
        )
        self._theme_source.connect("notify::selected", self._on_changed)
        group.add(self._theme_source)

        self._favourites_only = Adw.SwitchRow(
            title="Cycle favourites only",
            subtitle="Ignored while nothing is starred, so the rotation never empties",
        )
        self._favourites_only.connect("notify::active", self._on_changed)
        group.add(self._favourites_only)

        self._muted = Adw.SwitchRow(
            title="Mute video wallpapers",
            subtitle="The audio track stays loaded, so this takes effect immediately",
        )
        self._muted.connect("notify::active", self._on_changed)
        group.add(self._muted)

        self._volume = Adw.SpinRow(
            title="Video volume",
            subtitle="Applied to the video already playing",
            adjustment=Gtk.Adjustment(
                lower=0, upper=renderer.MAX_VOLUME, step_increment=5, value=renderer.MAX_VOLUME
            ),
        )
        self._volume.connect("notify::value", self._on_changed)
        group.add(self._volume)

        self._hardware_decode = Adw.SwitchRow(
            title="Hardware video decoding",
            subtitle="Usually lowers CPU use; turn off to diagnose decoder or driver artifacts",
        )
        self._hardware_decode.connect("notify::active", self._on_changed)
        group.add(self._hardware_decode)

        self._interpolation = Adw.ComboRow(
            title="Smooth low-frame-rate videos",
            subtitle=(
                "Uses an unambiguous active-monitor refresh; mixed-rate All outputs keeps "
                "source cadence. Linear blending may ghost"
            ),
            model=Gtk.StringList.new(
                [_INTERPOLATION_LABELS[choice] for choice in renderer.INTERPOLATION_CHOICES]
            ),
        )
        self._interpolation.connect("notify::selected", self._on_changed)
        group.add(self._interpolation)

        self._scene_fps = Adw.SpinRow(
            title="Wallpaper Engine frame rate",
            subtitle="Native scene-rendering limit; videos keep their source frame rate",
            adjustment=Gtk.Adjustment(
                lower=scenes.MIN_FPS,
                upper=scenes.MAX_FPS,
                step_increment=1,
                page_increment=10,
                value=scenes.DEFAULT_FPS,
            ),
        )
        self._scene_fps.connect("notify::value", self._on_changed)
        group.add(self._scene_fps)

        self._when_hidden = Adw.ComboRow(
            title="When covered by a window",
            subtitle="Takes effect on the next video. Try 'Keep playing' if the others misbehave",
            model=Gtk.StringList.new(
                [_WHEN_HIDDEN_LABELS[choice] for choice in renderer.WHEN_HIDDEN_CHOICES]
            ),
        )
        self._when_hidden.connect("notify::selected", self._on_changed)
        group.add(self._when_hidden)
        return group

    def _known_theme_connectors(self, settings: config.Settings) -> tuple[str, ...]:
        connected = self._live_theme_connectors()
        session = getattr(self._app, "session", None)
        saved = (
            {connector for connector, _playlist in session.displays.all()}
            if session is not None
            else set()
        )
        if session is not None:
            saved.update(rule.connector for rule in session.schedules.rules if rule.connector)
        if settings.theme_source_connector:
            saved.add(settings.theme_source_connector)
        if settings.output:
            # Kept for the legacy Python service and offered as a migration
            # candidate without continuing to expose the ambiguous old Output
            # setting as a separate control.
            saved.add(settings.output)
        ordered = list(connected)
        ordered.extend(connector for connector in sorted(saved) if connector not in ordered)
        return tuple(ordered)

    def _live_theme_connectors(self) -> tuple[str, ...]:
        """Merge GTK monitor names with Rust's authoritative niri snapshot."""
        found = list(_connected_outputs())
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        if truth is not None and truth.status_version == 2:
            for display in truth.displays:
                if display.connected and display.connector not in found:
                    found.append(display.connector)
        return tuple(found)

    def _watch_output_changes(self) -> None:
        """Refresh connector labels when docking changes without rewriting the choice."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        # The model belongs to the display for the lifetime of this page. A
        # directory-style diff is unnecessary here: rebuilding one small
        # StringList leaves the persisted connector untouched, and ComboRow
        # focus is not an editing surface.
        display.get_monitors().connect("items-changed", self._on_outputs_changed)

    def _on_outputs_changed(self, *_arguments: object) -> None:
        self._loading = True
        try:
            self._refresh_display_controls(self._app.settings)
        finally:
            self._loading = False

    def _selected_theme_connector(self) -> str:
        index = self._theme_source.get_selected()
        if index >= len(self._theme_connectors):
            return ""
        return self._theme_connectors[index]

    def _refresh_display_controls(self, settings: config.Settings) -> None:
        self._theme_connectors = self._known_theme_connectors(settings)
        connected = self._live_theme_connectors()
        attached = set(connected)
        self._theme_attached = frozenset(attached)
        labels = [
            connector if connector in attached else f"{connector} (not attached)"
            for connector in self._theme_connectors
        ]
        self._theme_source.set_model(Gtk.StringList.new(labels))
        selected = (
            self._theme_connectors.index(settings.theme_source_connector)
            if settings.theme_source_connector in self._theme_connectors
            else 0
        )
        self._theme_source.set_selected(selected)
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        reported = truth.theme_source if truth is not None and truth.status_version == 2 else None
        resolved = display_policy.resolve_theme_source(settings.theme_source_connector, connected)
        effective = reported.effective if reported is not None else resolved.effective
        fallback_active = reported.fallback if reported is not None else resolved.is_fallback
        if fallback_active:
            fallback = (
                f"Colours temporarily follow {effective}"
                if effective
                else "No active display is available for palette changes"
            )
            self._theme_source.set_subtitle(
                f"{resolved.configured} is not attached. {fallback}; the saved choice "
                "returns when it reconnects"
            )
        elif resolved.configured:
            self._theme_source.set_subtitle(
                "Noctalia has one shell-wide palette; it follows this display"
            )
        else:
            self._theme_source.set_subtitle("Choose which display drives shell-wide colours")
        independent = settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
        self._theme_source.set_visible(independent)

    def runtime_status_changed(self, settings: config.Settings) -> None:
        """Adopt niri connector/palette truth without rebuilding other settings."""
        connectors = self._known_theme_connectors(settings)
        attached = frozenset(self._live_theme_connectors())
        if connectors == self._theme_connectors and attached == self._theme_attached:
            # Effective palette fallback can still move when Rust changes its
            # designated source, so update the subtitle from the new snapshot
            # without replacing the focused ComboRow model.
            truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
            if truth is None or truth.theme_source is None:
                return
            effective = truth.theme_source.effective
            if truth.theme_source.fallback:
                fallback = (
                    f"Colours temporarily follow {effective}"
                    if effective
                    else "No active display is available for palette changes"
                )
                self._theme_source.set_subtitle(
                    f"{truth.theme_source.configured} is not attached. {fallback}; the saved "
                    "choice returns when it reconnects"
                )
            elif truth.theme_source.configured:
                self._theme_source.set_subtitle(
                    "Noctalia has one shell-wide palette; it follows this display"
                )
            return
        self._loading = True
        try:
            self._refresh_display_controls(settings)
        finally:
            self._loading = False

    def _build_providers_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Providers",
            description=(
                "Wallhaven searches work without an API key, but NSFW results "
                "are only reachable with one. A key saved here is written to a "
                "file only you can read."
            ),
        )

        self._api_key_status = Adw.ActionRow(title="Wallhaven API key")
        self._clear_api_key = Gtk.Button(label="Clear")
        self._clear_api_key.set_valign(Gtk.Align.CENTER)
        self._clear_api_key.add_css_class("flat")
        self._clear_api_key.set_tooltip_text("Delete the saved key file")
        self._clear_api_key.connect("clicked", self._on_clear_api_key)
        self._api_key_status.add_suffix(self._clear_api_key)
        group.add(self._api_key_status)

        # A password row so the key is not left legible on a screen someone
        # else can see. It starts empty and is never filled from the stored
        # key: this dialogue only ever needs to know that a key exists.
        self._api_key_entry = Adw.PasswordEntryRow(title="New key")
        self._api_key_entry.connect("entry-activated", self._on_save_api_key)
        # Otherwise the row stays red from a rejected key while the user is
        # already typing the corrected one.
        self._api_key_entry.connect(
            "changed", lambda _entry: self._api_key_entry.remove_css_class("error")
        )
        save = Gtk.Button(label="Save")
        save.set_valign(Gtk.Align.CENTER)
        save.add_css_class("flat")
        save.connect("clicked", self._on_save_api_key)
        self._api_key_entry.add_suffix(save)
        group.add(self._api_key_entry)
        return group

    def _build_colour_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Colour",
            description="Use Noctalia's active palette or a fixed app palette.",
        )

        self._follow_palette = Adw.SwitchRow(
            title="Follow Noctalia colours",
            subtitle="Update this app's chrome when Noctalia's active palette changes",
        )
        self._follow_palette.connect("notify::active", self._on_changed)
        group.add(self._follow_palette)

        self._palette_source = Adw.ActionRow(title="Palette source")
        group.add(self._palette_source)

        self._scheme = Adw.ComboRow(
            title="Scheme",
            subtitle="Generator used when a palette is derived from the wallpaper",
            model=Gtk.StringList.new(list(ALL_SCHEMES)),
        )
        self._scheme.connect("notify::selected", self._on_changed)
        group.add(self._scheme)

        self._swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._swatches.set_homogeneous(True)
        swatch_row = Adw.PreferencesRow()
        swatch_row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(self._swatches)
        swatch_row.set_child(box)
        group.add(swatch_row)

        browse_row = Adw.ActionRow(
            title="Palettes",
            subtitle="Browse installed palettes and preview every scheme",
        )
        browse_button = Gtk.Button(label="Browse")
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button.add_css_class("flat")
        browse_button.connect("clicked", lambda _button: self._app.open_palette_browser())
        browse_row.add_suffix(browse_button)
        group.add(browse_row)

        reload_row = Adw.ActionRow(
            title="Reload palette",
            subtitle="Re-read the colours Noctalia last rendered",
        )
        reload_button = Gtk.Button(label="Reload")
        reload_button.set_valign(Gtk.Align.CENTER)
        reload_button.add_css_class("flat")
        reload_button.connect("clicked", self._on_reload_palette)
        reload_row.add_suffix(reload_button)
        group.add(reload_row)
        return group

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Appearance",
            description=(
                "Translucency is applied by this app. Blur behind it is the "
                "compositor's job -- see docs/niri.md for the window rule."
            ),
        )
        self._opacity = Adw.SpinRow(
            title="Window opacity",
            subtitle="Lower values let the compositor show and blur through",
            adjustment=Gtk.Adjustment(
                lower=config.MIN_OPACITY, upper=1.0, step_increment=0.01, value=1.0
            ),
            digits=2,
        )
        self._opacity.connect("notify::value", self._on_changed)
        group.add(self._opacity)
        return group

    # -- state -----------------------------------------------------------

    def apply_settings(self, settings: config.Settings) -> None:
        """Reflect an application-owned settings snapshot without writing it.

        Settings can also move through the authoring socket and other pages.
        Keeping the same widgets preserves focus and text interaction, while
        reloading their values prevents the next local edit from serialising a
        stale copy of every unrelated field.
        """
        self._load(settings)
        self._refresh_roots()

    def _load(self, settings: config.Settings) -> None:
        self._loading = True
        try:
            self._shuffle.set_active(settings.shuffle)
            self._cycle.set_active(settings.cycle_enabled)
            self._interval.set_value(settings.cycle_interval)
            self._dynamics.set_active(settings.dynamics_enabled)
            self._own_scenes.set_active(settings.own_scene_renderer)
            self._workshop.set_active(settings.scan_workshop)
            self._favourites_only.set_active(settings.cycle_favourites_only)
            self._display_mode.set_selected(
                1 if settings.display_mode == config.DISPLAY_MODE_INDEPENDENT else 0
            )
            self._refresh_display_controls(settings)
            self._muted.set_active(settings.video_muted)
            self._volume.set_value(settings.video_volume)
            self._hardware_decode.set_active(settings.video_hardware_decode)
            if settings.video_interpolation in renderer.INTERPOLATION_CHOICES:
                self._interpolation.set_selected(
                    renderer.INTERPOLATION_CHOICES.index(settings.video_interpolation)
                )
            self._scene_fps.set_value(settings.scene_fps)
            if settings.video_when_hidden in renderer.WHEN_HIDDEN_CHOICES:
                self._when_hidden.set_selected(
                    renderer.WHEN_HIDDEN_CHOICES.index(settings.video_when_hidden)
                )
            self._opacity.set_value(settings.opacity)
            self._follow_palette.set_active(settings.follow_noctalia_palette)
            if settings.preview_scheme in ALL_SCHEMES:
                self._scheme.set_selected(ALL_SCHEMES.index(settings.preview_scheme))
        finally:
            self._loading = False

    def _on_changed(self, *_arguments: object) -> None:
        if self._loading:
            return
        scheme_index = self._scheme.get_selected()
        hidden_index = self._when_hidden.get_selected()
        interpolation_index = self._interpolation.get_selected()
        independent = self._display_mode.get_selected() == 1
        if independent and not self._selected_theme_connector():
            connected = self._live_theme_connectors()
            if not connected:
                self._loading = True
                try:
                    self._display_mode.set_selected(0)
                finally:
                    self._loading = False
                self._theme_source.set_visible(False)
                self._report(
                    "Independent display control needs an attached display for Colours "
                    "follow. Connect a display and try again."
                )
                return
            connector = connected[0]
            # Live connectors are always part of this model. Refresh once in
            # case a monitor appeared between the signal and this callback.
            if connector not in self._theme_connectors:
                self._loading = True
                try:
                    self._refresh_display_controls(self._app.settings)
                finally:
                    self._loading = False
            if connector in self._theme_connectors:
                self._loading = True
                try:
                    self._theme_source.set_selected(self._theme_connectors.index(connector))
                finally:
                    self._loading = False
        self._theme_source.set_visible(independent)
        changes: dict[str, object] = {
            "shuffle": self._shuffle.get_active(),
            "cycle_enabled": self._cycle.get_active(),
            "cycle_interval": int(self._interval.get_value()),
            "dynamics_enabled": self._dynamics.get_active(),
            "own_scene_renderer": self._own_scenes.get_active(),
            "scan_workshop": self._workshop.get_active(),
            "cycle_favourites_only": self._favourites_only.get_active(),
            "display_mode": (
                config.DISPLAY_MODE_INDEPENDENT if independent else config.DISPLAY_MODE_MIRRORED
            ),
            "theme_source_connector": (
                self._selected_theme_connector()
                if independent
                else self._app.settings.theme_source_connector
            ),
            "video_muted": self._muted.get_active(),
            "video_volume": int(self._volume.get_value()),
            "video_hardware_decode": self._hardware_decode.get_active(),
            "video_interpolation": renderer.INTERPOLATION_CHOICES[interpolation_index]
            if interpolation_index < len(renderer.INTERPOLATION_CHOICES)
            else renderer.DEFAULT_INTERPOLATION,
            "scene_fps": int(self._scene_fps.get_value()),
            "video_when_hidden": renderer.WHEN_HIDDEN_CHOICES[hidden_index]
            if hidden_index < len(renderer.WHEN_HIDDEN_CHOICES)
            else renderer.DEFAULT_WHEN_HIDDEN,
            "opacity": round(self._opacity.get_value(), 2),
            "follow_noctalia_palette": self._follow_palette.get_active(),
            "preview_scheme": ALL_SCHEMES[scheme_index]
            if scheme_index < len(ALL_SCHEMES)
            else config.Settings().preview_scheme,
        }
        # A failed write restores several widgets. Some GTK controls can emit
        # a trailing notification after that synchronous restore; do not turn
        # it into a second write attempt or a duplicate error toast.
        if all(getattr(self._app.settings, key) == value for key, value in changes.items()):
            return
        try:
            self._app.update_settings(**changes)
        except config.ConfigError as error:
            # The application adopts only after a durable write. Put every
            # control back on that last durable snapshot so a later edit cannot
            # smuggle the rejected value into an unrelated save.
            self.apply_settings(self._app.settings)
            self._report(f"Settings were not saved; nothing changed: {error}")

    # -- the Wallhaven key -----------------------------------------------

    def _refresh_api_key_status(self) -> None:
        """Say where the key in force comes from, without showing the key."""
        from_environment = bool(credentials.environment_key())
        stored = credentials.stored_key_present()
        if from_environment and stored:
            subtitle = (
                f"Taken from {registry.API_KEY_VARIABLE}, which overrides the "
                "saved key. Unset the variable to use the saved one."
            )
        elif from_environment:
            subtitle = (
                f"Taken from {registry.API_KEY_VARIABLE}. The environment is "
                "read first, so a key saved here would stay unused."
            )
        elif credentials.environment_key_is_malformed():
            # Set but unusable. Reporting this as "not set" would send the user
            # looking for a key they have already exported.
            # The environment is read first and stops there, so a saved key is
            # not a way out of this: the variable has to be corrected or unset.
            saved = " Unset it to use the saved key." if stored else ""
            subtitle = (
                f"{registry.API_KEY_VARIABLE} is set to something that is not a "
                f"valid key, so Wallhaven runs unauthenticated.{saved}"
            )
        elif stored:
            subtitle = f"Saved in {credentials.key_path()}"
        else:
            subtitle = "Not set. Searches work; NSFW results are out of reach."
        self._api_key_status.set_subtitle(subtitle)
        self._clear_api_key.set_sensitive(stored)

    def _on_save_api_key(self, _widget: Gtk.Widget) -> None:
        try:
            credentials.save_key(self._api_key_entry.get_text())
        except ProviderError as error:
            # Every message shown here is composed from the error's kind rather
            # than from the text the user typed, so that no path out of this
            # branch can put the key on screen or into the log.
            self._api_key_entry.add_css_class("error")
            self._report(
                "That does not look like a Wallhaven API key"
                if error.kind == "credential"
                else "The key could not be written to disk"
            )
            return
        self._api_key_entry.remove_css_class("error")
        self._api_key_entry.set_text("")
        self._refresh_api_key_status()
        self._report("Wallhaven API key saved")

    def _on_clear_api_key(self, _button: Gtk.Button) -> None:
        try:
            removed = credentials.clear_key()
        except ProviderError:
            self._report("The saved key could not be removed")
            return
        self._refresh_api_key_status()
        self._report("Wallhaven API key removed" if removed else "There was no saved key")

    def _report(self, message: str) -> None:
        self._app.window_report(message)

    def _on_reload_palette(self, _button: Gtk.Button) -> None:
        self.show_palette(self._app.reload_palette())

    def show_palette(self, resolved: source.ResolvedPalette | None) -> None:
        if resolved is None:
            self._palette_source.set_subtitle("not resolved yet")
            return
        # `detail` already reads as a sentence ("generated from x.png with ..."),
        # so prefixing the origin would stutter. The one case worth naming is the
        # fallback, where the colours are ours rather than Noctalia's.
        prefix = "" if resolved.is_live else "fallback palette - "
        self._palette_source.set_subtitle(f"{prefix}{resolved.detail}")
        self._rebuild_swatches(resolved.palette)

    def _rebuild_swatches(self, palette: Palette) -> None:
        while (child := self._swatches.get_first_child()) is not None:
            self._swatches.remove(child)
        for token, label in STRIP_TOKENS:
            colour = palette.colours.get(token)
            if colour is not None:
                self._swatches.append(swatch(colour, label))


class PreferencesDialog(Adw.PreferencesDialog):
    """Compatibility wrapper for callers that still request a dialog."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self.set_title("Settings")
        self.page = PreferencesPage(application)
        self.add(self.page)

    def show_palette(self, resolved: source.ResolvedPalette | None) -> None:
        self.page.show_palette(resolved)
