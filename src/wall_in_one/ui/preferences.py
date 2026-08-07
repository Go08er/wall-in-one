"""Settings dialog.

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

from gi.repository import Adw, Gio, GLib, Gtk

from wall_in_one import config
from wall_in_one.library import scan
from wall_in_one.providers import credentials, registry
from wall_in_one.providers.base import ProviderError
from wall_in_one.theme import source
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.theme.palette import Palette
from wall_in_one.ui.palette_browser import STRIP_TOKENS, swatch

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application


class PreferencesDialog(Adw.PreferencesDialog):
    """Playback, colour, and appearance settings."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self._app = application
        # Set while widgets are being populated from settings, so that
        # programmatic changes do not read as user edits and write back.
        self._loading = False

        self.set_title("Settings")
        page = Adw.PreferencesPage()
        page.add(self._build_library_group())
        page.add(self._build_playback_group())
        page.add(self._build_providers_group())
        page.add(self._build_colour_group())
        page.add(self._build_appearance_group())
        self.add(page)

        self._load(application.settings)
        self._refresh_roots()
        self._refresh_api_key_status()
        self.show_palette(application.resolved_palette)

    # -- construction ----------------------------------------------------

    def _build_library_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Library",
            description=(
                "Folders scanned for wallpapers. With none listed, Noctalia's "
                "own wallpaper directory is used. Downloads and generated "
                "stills go into the first one."
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
        return group

    def _refresh_roots(self) -> None:
        for row in self._root_rows:
            self._roots_group.remove(row)
        self._root_rows = []

        roots = self._app.settings.roots
        if not roots:
            row = Adw.ActionRow(
                title="Following Noctalia",
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
        return str(found[0]) if found else "no wallpaper directory found"

    def _make_root_remover(self, root: Path) -> Any:
        def remove(_button: Gtk.Button) -> None:
            self._set_roots(tuple(r for r in self._app.settings.roots if r != root))

        return remove

    def _set_roots(self, roots: tuple[Path, ...]) -> None:
        self._app.update_settings(roots=roots)
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
        group = Adw.PreferencesGroup(title="Playback")

        self._shuffle = Adw.SwitchRow(
            title="Shuffle",
            subtitle="Visit every wallpaper once before repeating",
        )
        self._shuffle.connect("notify::active", self._on_changed)
        group.add(self._shuffle)

        self._cycle = Adw.SwitchRow(
            title="Cycle",
            subtitle="Change wallpaper on a timer",
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
        return group

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
            description="Colours follow Noctalia's active palette.",
        )

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

    def _load(self, settings: config.Settings) -> None:
        self._loading = True
        try:
            self._shuffle.set_active(settings.shuffle)
            self._cycle.set_active(settings.cycle_enabled)
            self._interval.set_value(settings.cycle_interval)
            self._dynamics.set_active(settings.dynamics_enabled)
            self._opacity.set_value(settings.opacity)
            if settings.preview_scheme in ALL_SCHEMES:
                self._scheme.set_selected(ALL_SCHEMES.index(settings.preview_scheme))
        finally:
            self._loading = False

    def _on_changed(self, *_arguments: object) -> None:
        if self._loading:
            return
        scheme_index = self._scheme.get_selected()
        self._app.update_settings(
            shuffle=self._shuffle.get_active(),
            cycle_enabled=self._cycle.get_active(),
            cycle_interval=int(self._interval.get_value()),
            dynamics_enabled=self._dynamics.get_active(),
            opacity=round(self._opacity.get_value(), 2),
            preview_scheme=ALL_SCHEMES[scheme_index]
            if scheme_index < len(ALL_SCHEMES)
            else config.Settings().preview_scheme,
        )

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
        self.add_toast(Adw.Toast.new(message))

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
