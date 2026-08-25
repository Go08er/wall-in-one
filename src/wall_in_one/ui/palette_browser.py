"""The palette browser: what is installed, what a wallpaper would produce, and
a way to keep either as your own.

Two views, because they answer two different questions:

* **Browse** -- the palettes Noctalia already has, from all three sources.
  Applying one goes through Noctalia (`color-scheme-set`), because that is the
  only path that covers built-ins and the only one that re-themes the rest of
  the desktop with us.
* **Schemes** -- what each of the ten generators makes of a chosen wallpaper.
  Generation is ~0.3s a call, so ten of them run on a small thread pool and
  arrive through `GLib.idle_add`, exactly as thumbnails do.

Editing is deliberately narrow: the fourteen core keys a palette file actually
carries, with a colour picker each. A good editor for those beats a bad one for
all seventy-two, most of which Noctalia derives rather than stores.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from wall_in_one.theme import noctalia, palettes
from wall_in_one.theme.palette import Colour, Mode, Palette, PaletteError, PalettePair
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: Tokens worth showing as a strip. Enough to tell two palettes apart at a
#: glance without turning every row into a colour chart.
STRIP_TOKENS: Final[tuple[tuple[str, str], ...]] = (
    ("primary", "Primary"),
    ("secondary", "Secondary"),
    ("tertiary", "Tertiary"),
    ("error", "Error"),
    ("surface", "Surface"),
    ("surface_container", "Container"),
    ("outline", "Outline"),
)

#: Three at a time. `noctalia theme` peaks around 119 MB, and ten of them at
#: once would be a memory spike for no wall-clock gain on the machines this
#: runs on.
MAX_WORKERS: Final = 3

#: Ten schemes times a handful of wallpapers. Past that the oldest go.
MAX_CACHED_PREVIEWS: Final = 200

#: How long to wait before re-reading the palette after asking Noctalia to
#: switch. Noctalia re-renders templates as part of the switch, and our
#: template's post-hook is the real signal, but that arrives over the control
#: socket which is not wired up yet.
_SETTLE_MS: Final = 600

#: Palette rows contain seven swatches and their CSS providers.  Searching is
#: over the complete immutable snapshot, but GTK materialises only one page at
#: a time so a maximum-size catalogue cannot freeze the dialog.
BROWSE_PAGE_SIZE: Final = 24
MAX_SKIPPED_ROWS: Final = 24


def swatch(
    colour: Colour, caption: str = "", *, height: int = 44, show_value: bool = True
) -> Gtk.Widget:
    """A colour chip, optionally captioned and labelled with its hex."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    chip = Gtk.Box()
    chip.set_size_request(-1, height)
    # Set inline rather than through the stylesheet: these are data, not theme,
    # and regenerating 70-odd rules on every palette change to express them
    # would be the wrong shape.
    name = f"wio-swatch-{colour.hex[1:]}"
    provider = Gtk.CssProvider()
    provider.load_from_string(
        f".{name} {{ background-color: {colour.hex};"
        " border-radius: 8px; border: 1px solid alpha(currentColor, 0.15); }"
    )
    chip.add_css_class(name)
    chip.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    box.append(chip)

    if caption:
        label = Gtk.Label(label=caption)
        label.add_css_class("caption")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(label)
    if show_value:
        value = Gtk.Label(label=colour.hex)
        value.add_css_class("caption")
        value.add_css_class("dim-label")
        box.append(value)
    return box


def swatch_strip(
    palette: Palette,
    *,
    height: int = 20,
    width: int = 22,
    show_values: bool = False,
) -> Gtk.Widget:
    """The `STRIP_TOKENS` of ``palette``, side by side."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    box.set_valign(Gtk.Align.CENTER)
    for token, label in STRIP_TOKENS:
        colour = palette.colours.get(token)
        if colour is None:
            continue
        chip = swatch(colour, label if show_values else "", height=height, show_value=show_values)
        chip.set_size_request(width, -1)
        chip.set_tooltip_text(f"{label} {colour.hex}")
        box.append(chip)
    return box


# -- scheme previews -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemePreview:
    """What one generator made of one wallpaper, or why it could not."""

    image: Path
    scheme: str
    colours: PalettePair | None
    error: str = ""


PreviewCallback = Callable[[SchemePreview], None]


class SchemePreviewLoader:
    """Runs `noctalia theme` off-thread and delivers on the main thread.

    Same shape as `ui.thumbnails.ThumbnailLoader`, and for the same reason: at
    ~0.3s a call, generating ten schemes inline would stall the window for
    three seconds every time the chosen wallpaper changed.
    """

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scheme")
        self._pending: dict[tuple[Path, str], Future[SchemePreview]] = {}
        self._cache: dict[tuple[Path, str], SchemePreview] = {}
        self._closed = False

    def request(self, image: Path, scheme: str, callback: PreviewCallback) -> None:
        """Ask for ``scheme`` applied to ``image``. ``callback`` runs on the main thread.

        A cached result is delivered immediately and synchronously, so going
        back to a wallpaper you have already looked at does not flash through
        ten spinners on its way to looking identical.
        """
        if self._closed:
            return
        key = (image, scheme)
        cached = self._cache.get(key)
        if cached is not None:
            callback(cached)
            return
        if key in self._pending:
            return

        future = self._pool.submit(self._generate, image, scheme)
        self._pending[key] = future
        future.add_done_callback(lambda done: self._finish(key, done, callback))

    @staticmethod
    def _generate(image: Path, scheme: str) -> SchemePreview:
        try:
            pair = noctalia.generate(image, scheme)
        except noctalia.NoctaliaError as error:
            return SchemePreview(image=image, scheme=scheme, colours=None, error=str(error))
        return SchemePreview(image=image, scheme=scheme, colours=pair)

    def _finish(
        self,
        key: tuple[Path, str],
        future: Future[SchemePreview],
        callback: PreviewCallback,
    ) -> None:
        self._pending.pop(key, None)
        if self._closed:
            return
        try:
            preview = future.result()
        except Exception as error:
            # Broad on purpose: a worker must never take the app down.
            preview = SchemePreview(image=key[0], scheme=key[1], colours=None, error=str(error))

        def deliver() -> bool:
            if self._closed:
                return GLib.SOURCE_REMOVE
            if preview.colours is not None:
                if len(self._cache) >= MAX_CACHED_PREVIEWS:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = preview
            callback(preview)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def shutdown(self) -> None:
        self._closed = True
        self._pending.clear()
        # Not waiting: a generation is a fraction of a second and closing the
        # dialog should be immediate. Nothing is written, so an abandoned
        # worker leaves nothing behind.
        self._pool.shutdown(wait=False, cancel_futures=True)


class _SchemeCard(Gtk.Box):
    """One generator's result, with the two things you might do about it."""

    def __init__(
        self,
        scheme: str,
        on_use: Callable[[str], None],
        on_save: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.scheme = scheme
        self._preview: SchemePreview | None = None

        self.add_css_class("card")
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        title = Gtk.Label(label=scheme)
        title.add_css_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_margin_top(10)
        self.append(title)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._body.set_margin_start(10)
        self._body.set_margin_end(10)
        self._body.set_size_request(-1, 34)
        self.append(self._body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_bottom(10)
        self._use = Gtk.Button(label="Use")
        self._use.add_css_class("flat")
        self._use.connect("clicked", lambda _button: on_use(scheme))
        self._save = Gtk.Button(label="Save as...")
        self._save.add_css_class("flat")
        self._save.connect("clicked", lambda _button: on_save(scheme))
        buttons.append(self._use)
        buttons.append(self._save)
        self.append(buttons)

        self.set_pending()

    def _clear(self) -> None:
        while (child := self._body.get_first_child()) is not None:
            self._body.remove(child)

    def set_pending(self) -> None:
        self._preview = None
        self._save.set_sensitive(False)
        self._clear()
        spinner = Adw.Spinner()
        spinner.set_size_request(-1, 28)
        self._body.append(spinner)

    def set_preview(self, preview: SchemePreview, mode: Mode) -> None:
        self._preview = preview
        self.set_mode(mode)

    def set_mode(self, mode: Mode) -> None:
        preview = self._preview
        if preview is None:
            return
        self._clear()
        self._save.set_sensitive(preview.colours is not None)
        if preview.colours is None:
            failed = Gtk.Label(label=preview.error or "generation failed")
            failed.add_css_class("caption")
            failed.add_css_class("dim-label")
            failed.set_wrap(True)
            self._body.append(failed)
            return
        self._body.append(swatch_strip(preview.colours.for_mode(mode), height=28, width=26))

    @property
    def preview(self) -> SchemePreview | None:
        return self._preview


# -- the dialog ----------------------------------------------------------


class PaletteBrowserDialog(Adw.Dialog):
    """Browse, preview, apply, and duplicate palettes."""

    def __init__(
        self,
        application: Application,
        *,
        palette_catalog: PaletteCatalog | None = None,
    ) -> None:
        super().__init__()
        self._app = application
        self._loader = SchemePreviewLoader()
        self._palette_catalog = palette_catalog or PaletteCatalog()
        self._owns_palette_catalog = palette_catalog is None
        self._catalog_state = self._palette_catalog.state
        self._discovery = self._catalog_state.discovery
        self._cards: dict[str, _SchemeCard] = {}
        self._groups: list[Adw.PreferencesGroup] = []
        self._entry_rows: dict[tuple[palettes.Origin, str, str], Adw.ActionRow] = {}
        self._entry_groups: dict[tuple[palettes.Origin, str, str], Adw.PreferencesGroup] = {}
        self._row_models: dict[
            tuple[palettes.Origin, str, str], tuple[palettes.PaletteEntry, Mode]
        ] = {}
        self._reuse_rows: dict[tuple[palettes.Origin, str, str], Adw.ActionRow] = {}
        self._reuse_models: dict[
            tuple[palettes.Origin, str, str], tuple[palettes.PaletteEntry, Mode]
        ] = {}
        self._render_key: object = None
        self._browse_limit = BROWSE_PAGE_SIZE
        self._image: Path | None = None
        self._images: tuple[Path, ...] = ()
        self._mode: Mode = "dark"
        self._closed = False

        self.set_title("Palettes")
        self.set_content_width(900)
        self.set_content_height(720)
        self.connect("closed", self._on_closed)

        self._browse = self._build_browse_page()
        self._toast = Adw.ToastOverlay()
        self._stack = Adw.ViewStack()
        self._stack.add_titled_with_icon(self._browse, "browse", "Installed", "view-list-symbolic")
        self._stack.add_titled_with_icon(
            self._build_schemes_page(), "schemes", "Schemes", "applications-graphics-symbolic"
        )

        self.set_child(self._build_content())
        self._reload_images()
        self._catalog_listener = self._palette_catalog.subscribe(self._catalog_changed)
        self._palette_catalog.ensure_loaded()

    # -- construction ----------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan palettes")
        rescan.connect("clicked", lambda _button: self.refresh())
        header.pack_start(rescan)

        toolbar.add_top_bar(header)
        self._toast.set_child(self._stack)
        toolbar.set_content(self._toast)
        return toolbar

    def _build_browse_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        search_group = Adw.PreferencesGroup(
            title="Installed palettes",
            description="Search every source; results are shown in small, responsive pages.",
        )
        self._palette_search = Gtk.SearchEntry(placeholder_text="Search palettes")
        self._palette_search.connect("search-changed", self._on_palette_search_changed)
        search_group.add(self._palette_search)
        page.add(search_group)

        self._catalog_status = Adw.PreferencesGroup()
        self._catalog_status_row = Adw.ActionRow()
        self._catalog_spinner = Adw.Spinner()
        self._catalog_spinner.set_size_request(22, 22)
        self._catalog_retry = Gtk.Button(label="Retry")
        self._catalog_retry.set_valign(Gtk.Align.CENTER)
        self._catalog_retry.connect("clicked", lambda _button: self.refresh())
        self._catalog_status_row.add_prefix(self._catalog_spinner)
        self._catalog_status_row.add_suffix(self._catalog_retry)
        self._catalog_status.add(self._catalog_status_row)
        page.add(self._catalog_status)

        self._more_group = Adw.PreferencesGroup()
        self._more_row = Adw.ActionRow()
        self._more_button = Gtk.Button()
        self._more_button.set_valign(Gtk.Align.CENTER)
        self._more_button.connect("clicked", self._show_more_palettes)
        self._more_row.add_suffix(self._more_button)
        self._more_group.add(self._more_row)
        return page

    def _build_schemes_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        source = Adw.PreferencesGroup(
            title="Preview source",
            description=(
                "What each of the ten generators makes of one wallpaper. "
                "Generation is the same code Noctalia uses, so this is the "
                "result rather than an approximation."
            ),
        )
        self._wallpaper = Adw.ComboRow(title="Wallpaper", model=Gtk.StringList.new([]))
        self._wallpaper.connect("notify::selected", self._on_wallpaper_changed)
        source.add(self._wallpaper)

        self._variant = Adw.ComboRow(
            title="Variant",
            subtitle="Every scheme produces both; this only chooses what is shown",
            model=Gtk.StringList.new(["Dark", "Light"]),
        )
        self._variant.connect("notify::selected", self._on_variant_changed)
        source.add(self._variant)

        self._sync = Adw.SwitchRow(
            title="Also set the scheme in Noctalia",
            subtitle="Otherwise 'Use' only changes what this app generates for itself",
        )
        source.add(self._sync)
        page.add(source)

        grid = Adw.PreferencesGroup(title="Schemes")
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_min_children_per_line(2)
        flow.set_max_children_per_line(3)
        for scheme in noctalia.ALL_SCHEMES:
            card = _SchemeCard(scheme, self._on_use_scheme, self._on_save_scheme)
            self._cards[scheme] = card
            flow.append(card)
        grid.add(flow)
        page.add(grid)
        return page

    # -- browse view -----------------------------------------------------

    def refresh(self) -> None:
        """Request a fresh filesystem snapshot without blocking GTK."""
        self._palette_catalog.refresh()

    def _catalog_changed(self, state: CatalogState) -> None:
        if self._closed:
            return
        self._catalog_state = state
        self._discovery = state.discovery
        if state.loading:
            self._catalog_status_row.set_title(
                "Loading installed palettes…"
                if not state.discovery.entries
                else "Refreshing installed palettes…"
            )
            self._catalog_status_row.set_subtitle(
                "The current page stays usable while files are checked."
                if state.discovery.entries
                else "Palette files are being checked outside the interface thread."
            )
            self._catalog_status.set_visible(True)
            self._catalog_spinner.set_visible(True)
            self._catalog_retry.set_visible(False)
        elif state.phase == "error":
            self._catalog_status_row.set_title("Installed palettes could not be loaded")
            self._catalog_status_row.set_subtitle(state.error)
            self._catalog_status.set_visible(True)
            self._catalog_spinner.set_visible(False)
            self._catalog_retry.set_visible(True)
        elif state.phase == "idle":
            self._catalog_status_row.set_title("Palette catalogue has not loaded yet")
            self._catalog_status_row.set_subtitle("Choose Retry to scan the installed sources.")
            self._catalog_status.set_visible(True)
            self._catalog_spinner.set_visible(False)
            self._catalog_retry.set_visible(True)
        else:
            self._catalog_status.set_visible(False)
        self._rebuild_browse()

    def _matching_entries(self) -> tuple[palettes.PaletteEntry, ...]:
        query = self._palette_search.get_text().strip().casefold()
        if not query:
            return self._discovery.entries
        return tuple(
            entry
            for entry in self._discovery.entries
            if query in entry.name.casefold()
            or query in entry.origin.label.casefold()
            or (entry.path is not None and query in str(entry.path).casefold())
        )

    def _visible_entries(self) -> tuple[palettes.PaletteEntry, ...]:
        """Fairly page across origins so a large community cache hides none."""
        buckets = {
            origin: list(entry for entry in self._matching_entries() if entry.origin is origin)
            for origin in palettes.Origin
        }
        visible: list[palettes.PaletteEntry] = []
        index = 0
        while len(visible) < self._browse_limit:
            added = False
            for origin in palettes.Origin:
                entries = buckets[origin]
                if index < len(entries):
                    visible.append(entries[index])
                    added = True
                    if len(visible) >= self._browse_limit:
                        break
            if not added:
                break
            index += 1
        return tuple(visible)

    def _on_palette_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._browse_limit = BROWSE_PAGE_SIZE
        self._rebuild_browse()

    def _show_more_palettes(self, _button: Gtk.Button) -> None:
        self._browse_limit += BROWSE_PAGE_SIZE
        self._rebuild_browse()

    def _rebuild_browse(self, *, force: bool = False) -> None:
        matches = self._matching_entries()
        visible = self._visible_entries()
        render_key = (
            self._discovery,
            self._palette_search.get_text(),
            self._browse_limit,
            self._mode,
        )
        if not force and render_key == self._render_key:
            return
        self._render_key = render_key
        # Adw.PreferencesPage has no "remove everything", and walking its
        # internal tree to find the groups again would be guessing at private
        # structure. Keeping the list is shorter and does not break.
        for group in self._groups:
            self._browse.remove(group)
        self._reuse_rows = dict(self._entry_rows)
        self._reuse_models = dict(self._row_models)
        for key, row in self._reuse_rows.items():
            old_group = self._entry_groups.get(key)
            if old_group is not None:
                old_group.remove(row)
        self._entry_rows.clear()
        self._entry_groups.clear()
        self._row_models.clear()
        self._groups = [
            self._build_origin_group(origin, visible, matches) for origin in palettes.Origin
        ]
        if self._discovery.skipped:
            self._groups.append(self._build_skipped_group())
        remaining = len(matches) - len(visible)
        if remaining > 0:
            self._more_row.set_title(f"{len(visible)} of {len(matches)} matching palettes shown")
            self._more_row.set_subtitle("The complete catalogue remains searchable.")
            self._more_button.set_label(f"Load {min(BROWSE_PAGE_SIZE, remaining)} more")
            self._groups.append(self._more_group)
        for group in self._groups:
            self._browse.add(group)
        self._reuse_rows.clear()
        self._reuse_models.clear()

    def _build_origin_group(
        self,
        origin: palettes.Origin,
        visible: tuple[palettes.PaletteEntry, ...],
        matches: tuple[palettes.PaletteEntry, ...],
    ) -> Adw.PreferencesGroup:
        entries = tuple(entry for entry in visible if entry.origin is origin)
        total = sum(entry.origin is origin for entry in matches)
        description = _ORIGIN_DESCRIPTIONS[origin]
        if total > len(entries):
            description += f" Showing {len(entries)} of {total} matches."
        group = Adw.PreferencesGroup(
            title=origin.label,
            description=description,
        )
        if not entries:
            searching = bool(self._palette_search.get_text().strip())
            empty = Adw.ActionRow(
                title="No matches" if searching else "Nothing here yet",
                subtitle=(
                    "Try a different palette name or source."
                    if searching
                    else _ORIGIN_EMPTY[origin]
                ),
            )
            empty.set_activatable(False)
            group.add(empty)
            return group
        for entry in entries:
            key = (entry.origin, entry.name, str(entry.path) if entry.path is not None else "")
            model = (entry, self._mode)
            row = self._reuse_rows.pop(key, None)
            if row is None or self._reuse_models.get(key) != model:
                row = self._build_entry_row(entry)
            self._entry_rows[key] = row
            self._entry_groups[key] = group
            self._row_models[key] = model
            group.add(row)
        return group

    def _build_entry_row(self, entry: palettes.PaletteEntry) -> Adw.ActionRow:
        row = Adw.ActionRow(title=entry.name, subtitle=entry.describe())
        palette = entry.for_mode(self._mode)
        if palette is not None:
            row.add_prefix(swatch_strip(palette))

        duplicate = Gtk.Button(
            icon_name="edit-copy-symbolic",
            tooltip_text="Duplicate into your own palettes and edit",
        )
        duplicate.set_valign(Gtk.Align.CENTER)
        duplicate.add_css_class("flat")
        duplicate.set_sensitive(entry.path is not None)
        duplicate.connect("clicked", lambda _button: self._open_editor(entry, in_place=False))
        row.add_suffix(duplicate)

        if entry.is_editable:
            edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit this palette")
            edit.set_valign(Gtk.Align.CENTER)
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _button: self._open_editor(entry, in_place=True))
            row.add_suffix(edit)

        apply_button = Gtk.Button(label="Apply")
        apply_button.set_valign(Gtk.Align.CENTER)
        apply_button.add_css_class("flat")
        apply_button.connect("clicked", lambda _button: self._on_apply_entry(entry))
        if not entry.origin.is_applicable:
            # `color-scheme-set` has no source name for these, so the button
            # could only ever produce "unknown palette source". Duplicating is
            # the route that works, and the tooltip has to say so or a dead
            # button is all the user gets.
            apply_button.set_sensitive(False)
            apply_button.set_tooltip_text(
                "Noctalia no longer reads this layout; duplicate it first"
            )
        row.add_suffix(apply_button)
        return row

    def _build_skipped_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Skipped",
            description="Files in a palette directory that could not be read.",
        )
        for note in self._discovery.skipped[:MAX_SKIPPED_ROWS]:
            row = Adw.ActionRow(title=note)
            row.set_activatable(False)
            row.add_css_class("dim-label")
            group.add(row)
        remaining = len(self._discovery.skipped) - MAX_SKIPPED_ROWS
        if remaining > 0:
            row = Adw.ActionRow(
                title=f"{remaining} more skipped files",
                subtitle="Fix the listed source errors, then rescan to see the next group.",
            )
            row.set_activatable(False)
            row.add_css_class("dim-label")
            group.add(row)
        return group

    # -- actions ---------------------------------------------------------

    def _on_apply_entry(self, entry: palettes.PaletteEntry) -> None:
        """Hand the palette to Noctalia, then adopt whatever it renders.

        Going through Noctalia rather than painting the colours ourselves is
        the whole point: it is the only route that covers built-ins, and it
        keeps the rest of the desktop in step with the app.
        """
        if not entry.origin.is_applicable:
            # The button is insensitive, so this is only reachable if a future
            # caller forgets. Better a sentence than a Noctalia error.
            self.report(f"{entry.name} lives in a layout Noctalia no longer reads")
            return
        self.report(f"Applying {entry.name}…")
        self._app.apply_noctalia_palette_async(
            entry.origin.value,
            entry.name,
            on_complete=lambda error: self._on_entry_applied(entry, error),
        )

    def _on_entry_applied(self, entry: palettes.PaletteEntry, error: str) -> None:
        if self._closed:
            return
        if error:
            self.report(f"{entry.name} was not applied: {error}")
            return
        self.report(f"{entry.name} applied")
        GLib.timeout_add(_SETTLE_MS, self._settle)

    def _settle(self) -> bool:
        if not self._closed:
            self._app.reload_palette()
            self._rebuild_browse()
        return GLib.SOURCE_REMOVE

    def _on_use_scheme(self, scheme: str) -> None:
        sync_noctalia = self._sync.get_active()
        self._app.use_preview_scheme(
            scheme,
            sync_noctalia=sync_noctalia,
            on_complete=lambda error: self._on_scheme_synced(scheme, error),
        )

    def _on_scheme_synced(self, scheme: str, error: str) -> None:
        if self._closed:
            return
        if error:
            # The app preference was durably saved before the external shell
            # request. Calling the whole gesture a failure would hide a real
            # partial success and make the next opening look spontaneous.
            self.report(f"App scheme changed to {scheme}, but Noctalia was not updated: {error}")
            return
        self.report(f"scheme {scheme}")
        GLib.timeout_add(_SETTLE_MS, self._settle)

    def _on_save_scheme(self, scheme: str) -> None:
        card = self._cards.get(scheme)
        preview = card.preview if card is not None else None
        if preview is None or preview.colours is None:
            self.report("that scheme has not finished generating")
            return
        self._present_editor(
            title=f"Save {scheme}",
            name=_suggest_name(f"{preview.image.stem} {scheme}"),
            document=palettes.document_from_pair(preview.colours),
            in_place=None,
        )

    def _open_editor(self, entry: palettes.PaletteEntry, *, in_place: bool) -> None:
        if entry.path is None:
            self.report(f"{entry.name} has no file to copy")
            return
        try:
            document = palettes.read_document(entry.path)
        except PaletteError as error:
            self.report(str(error))
            return
        self._present_editor(
            title=f"Edit {entry.name}" if in_place else f"Duplicate {entry.name}",
            name=entry.name if in_place else _suggest_name(f"{entry.name} copy"),
            document=document,
            in_place=entry if in_place else None,
        )

    def _present_editor(
        self,
        *,
        title: str,
        name: str,
        document: dict[str, object],
        in_place: palettes.PaletteEntry | None,
    ) -> None:
        editor = _PaletteEditor(
            application=self._app,
            title=title,
            name=name,
            document=document,
            in_place=in_place,
            on_saved=self._on_saved,
            on_error=self.report,
        )
        editor.present(self)

    def _on_saved(self, entry: palettes.PaletteEntry) -> None:
        # The old snapshot may contain the just-edited payload (or omit this
        # new entry), so clear it immediately rather than leave a stale choice
        # actionable while the post-write discovery runs.
        self._palette_catalog.invalidate()
        self.report(f"saved {entry.name}")

    # -- scheme preview --------------------------------------------------

    def _reload_images(self) -> None:
        """Offer the still wallpapers as preview sources.

        Stills only: the generator takes an image, and a video's paired still
        is already in the library as an item of its own.
        """
        library = self._app.session.library
        self._images = tuple(item.path for item in library.stills)
        names = Gtk.StringList.new([path.name for path in self._images])
        self._wallpaper.set_model(names)
        if not self._images:
            self._wallpaper.set_subtitle("No still wallpapers in the library")
            empty = SchemePreview(Path(), "", None, "nothing to generate from")
            for card in self._cards.values():
                card.set_preview(empty, self._mode)
            return

        cursor = self._app.session.cursor
        wanted = None
        if cursor is not None:
            wanted = cursor.paired_still if cursor.is_video else cursor.path
        index = self._images.index(wanted) if wanted in self._images else 0
        self._wallpaper.set_selected(index)
        self._start_previews(self._images[index])

    def _on_wallpaper_changed(self, *_arguments: object) -> None:
        index = self._wallpaper.get_selected()
        if index < len(self._images):
            self._start_previews(self._images[index])

    def _on_variant_changed(self, *_arguments: object) -> None:
        self._mode = "light" if self._variant.get_selected() == 1 else "dark"
        for card in self._cards.values():
            card.set_mode(self._mode)
        self._rebuild_browse()

    def _start_previews(self, image: Path) -> None:
        # `set_selected` fires a notify only when the value really changes, so
        # the initial load has to ask for previews itself -- and then this
        # guard is what stops the two paths asking twice.
        if self._image == image:
            return
        self._image = image
        self._wallpaper.set_subtitle(str(image.parent))
        for scheme, card in self._cards.items():
            card.set_pending()
            self._loader.request(image, scheme, self._on_preview)

    def _on_preview(self, preview: SchemePreview) -> None:
        # A result for a wallpaper the user has already moved on from is not
        # wrong, just stale; the cache keeps it for when they come back.
        if self._closed or preview.image != self._image:
            return
        card = self._cards.get(preview.scheme)
        if card is not None:
            card.set_preview(preview, self._mode)

    # -- lifecycle -------------------------------------------------------

    def report(self, message: str) -> None:
        self._toast.add_toast(Adw.Toast.new(message))

    def show_palette(self) -> None:
        """Called when the app's palette changed under us."""
        if not self._closed:
            self._rebuild_browse()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        self._palette_catalog.unsubscribe(self._catalog_listener)
        if self._owns_palette_catalog:
            self._palette_catalog.shutdown()
        self._loader.shutdown()


_ORIGIN_DESCRIPTIONS: Final[dict[palettes.Origin, str]] = {
    palettes.Origin.BUILTIN: (
        "Compiled into Noctalia. No CLI surface exposes their colours, so they "
        "have no swatches here until one is applied."
    ),
    palettes.Origin.COMMUNITY: "Downloaded by Noctalia and cached on disk.",
    palettes.Origin.CUSTOM: "Yours. The only palettes this app will write to.",
    palettes.Origin.LEGACY: (
        "From an older Noctalia layout that this version no longer reads. "
        "Duplicate one to make it applicable again."
    ),
}

_ORIGIN_EMPTY: Final[dict[palettes.Origin, str]] = {
    palettes.Origin.BUILTIN: "This build of Noctalia reports no built-in palettes",
    palettes.Origin.COMMUNITY: "Browse community palettes in Noctalia to cache some",
    palettes.Origin.CUSTOM: "Duplicate any palette above to start one",
    palettes.Origin.LEGACY: "Nothing left over from an older Noctalia",
}


def _suggest_name(raw: str) -> str:
    """A starting point for the name field, not a substitute for validation.

    `palettes.validate_name` refuses rather than rewrites, which is right for
    something the user typed. A prefilled suggestion is the one place where
    quietly dropping awkward characters is the kind thing to do.
    """
    kept = "".join(character for character in raw if character.isalnum() or character in " ._+-()")
    return " ".join(kept.split()).lstrip(".")[:64] or "Untitled"


# -- editing -------------------------------------------------------------


def _rgba(colour: Colour) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.red = colour.red / 255
    rgba.green = colour.green / 255
    rgba.blue = colour.blue / 255
    rgba.alpha = 1.0
    return rgba


def _hex_of(rgba: Gdk.RGBA) -> str:
    channels = (rgba.red, rgba.green, rgba.blue)
    red, green, blue = (round(min(1.0, max(0.0, value)) * 255) for value in channels)
    return f"#{red:02x}{green:02x}{blue:02x}"


class _PaletteEditor(Adw.Dialog):
    """Name it, pick the fourteen core colours, save it as your own."""

    def __init__(
        self,
        *,
        application: Application,
        title: str,
        name: str,
        document: dict[str, object],
        in_place: palettes.PaletteEntry | None,
        on_saved: Callable[[palettes.PaletteEntry], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._app = application
        self._document = document
        self._in_place = in_place
        self._on_saved = on_saved
        self._on_error = on_error
        self._buttons: dict[tuple[Mode, str], Gtk.ColorDialogButton] = {}

        self.set_title(title)
        self.set_content_width(640)
        self.set_content_height(760)

        self._name = Adw.EntryRow(title="Name")
        self._name.set_text(name)
        if in_place is not None:
            self._name.set_editable(False)
            self._name.set_tooltip_text("Editing in place; duplicate it to rename")

        self.set_child(self._build_content())

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)

        self._save = Gtk.Button(label="Save")
        self._save.add_css_class("suggested-action")
        self._save.connect("clicked", self._on_save)
        header.pack_end(self._save)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        naming = Adw.PreferencesGroup(title="Name")
        naming.add(self._name)
        page.add(naming)

        colours = Adw.PreferencesGroup(
            title="Core colours",
            description=(
                "The keys a palette file actually carries. Everything else in "
                "the 72-token set is derived by Noctalia when the palette is "
                "applied, and the terminal colours are carried over unchanged."
            ),
        )
        for key, label in palettes.EDITABLE_KEYS:
            colours.add(self._build_row(key, label))
        page.add(colours)

        toolbar.set_content(page)
        return toolbar

    def _build_row(self, key: str, label: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=label, subtitle=key)
        modes: tuple[Mode, ...] = ("light", "dark")
        for mode in modes:
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            button.set_valign(Gtk.Align.CENTER)
            button.set_tooltip_text(f"{label}, {mode}")
            button.set_rgba(_rgba(self._initial(mode, key)))
            self._buttons[(mode, key)] = button
            row.add_suffix(button)
        return row

    def _initial(self, mode: Mode, key: str) -> Colour:
        variant = self._document.get(mode)
        raw = variant.get(key) if isinstance(variant, dict) else None
        if isinstance(raw, str):
            try:
                return Colour.parse(raw)
            except PaletteError:
                pass
        # A key the source file does not carry still needs a starting colour;
        # mid-grey reads as "unset" rather than as a deliberate black.
        return Colour(128, 128, 128)

    def _on_save(self, _button: Gtk.Button) -> None:
        overrides: dict[Mode, dict[str, str]] = {"dark": {}, "light": {}}
        for (mode, key), button in self._buttons.items():
            overrides[mode][key] = _hex_of(button.get_rgba())
        try:
            document = palettes.with_overrides(self._document, overrides)
        except PaletteError as error:
            self._on_error(str(error))
            return
        name = self._name.get_text()
        in_place = self._in_place
        self._save.set_sensitive(False)

        def work() -> palettes.PaletteEntry:
            return (
                palettes.save_edits(in_place, document)
                if in_place is not None
                else palettes.write_custom(name, document)
            )

        def saved(entry: palettes.PaletteEntry) -> None:
            self._save.set_sensitive(True)
            self._on_saved(entry)
            self.close()

        def failed(message: str) -> None:
            self._save.set_sensitive(True)
            self._on_error(message)

        self._app.authoring_action_async(work, saved, failure=failed)
