"""Full-page pairing editor.

The media grid answers "what do I own?"; this page answers what each item
means to Wall-in-One: its representative still and its colour policy. Keeping
that work in a page rather than a tile popover leaves enough room for image and
palette previews, and makes the defaults visible instead of hiding them behind
an actions menu.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from wall_in_one.library import manage, pairings, scan
from wall_in_one.library.model import (
    IMAGE_EXTENSIONS,
    Kind,
    MediaItem,
    RepresentativeStillError,
)
from wall_in_one.theme import noctalia, palettes
from wall_in_one.theme.palette import Mode as PaletteMode
from wall_in_one.theme.palette import Palette, PalettePair
from wall_in_one.ui.palette_browser import SchemePreview, SchemePreviewLoader, swatch_strip
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog
from wall_in_one.ui.thumbnails import ThumbnailLoader

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


_MODES: tuple[tuple[str, pairings.Mode], ...] = (
    ("Keep current mode", pairings.Mode.KEEP),
    ("Dark", pairings.Mode.DARK),
    ("Light", pairings.Mode.LIGHT),
    ("Automatic", pairings.Mode.AUTO),
)

#: A picker opening against a four-thousand-item library must not enqueue four
#: thousand ffmpeg jobs or build that many GTK cards in one callback. Search
#: still considers the complete inventory; the graphical results expand in
#: deliberately small pages.
STILL_PICKER_PAGE_SIZE: Final = 48

#: Installed palette rows are substantially heavier than strings: every
#: visible swatch owns CSS and colour widgets.  Search still covers the whole
#: catalogue, while explicit paging keeps an editor opening bounded.
PALETTE_PAGE_SIZE: Final = 24


class _StillCard(Gtk.ToggleButton):
    """One asynchronously thumbnailed still in the representative picker."""

    def __init__(self, item: MediaItem, on_choose: Callable[[MediaItem], None]) -> None:
        super().__init__()
        self.item = item
        self.add_css_class("card")
        self.set_size_request(164, -1)
        self.set_tooltip_text(str(item.path))

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.picture = Gtk.Picture(width_request=164, height_request=92)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        content.append(self.picture)
        title = Gtk.Label(label=item.name)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(22)
        title.add_css_class("caption")
        content.append(title)
        self.set_child(content)
        self.connect("toggled", lambda button: on_choose(item) if button.get_active() else None)

    def show_thumbnail(self, _item: MediaItem, texture: Gdk.Texture | None) -> None:
        self.picture.set_paintable(texture)


class PairingsPage(Gtk.Box):
    """A full-size editor reached from one item in the Media grid."""

    def __init__(
        self,
        application: Application,
        on_back: Callable[[], None],
        on_remove: Callable[[MediaItem], None] | None = None,
        *,
        palette_catalog: PaletteCatalog | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._app = application
        self._on_back = on_back
        self._on_remove = on_remove
        self._session: Session | None = None
        self._selected: MediaItem | None = None
        self._rendered: tuple[MediaItem, pairings.Pairing] | None = None
        self._preview_loader = SchemePreviewLoader(max_workers=2)
        self._thumbnail_loader = ThumbnailLoader(max_workers=2)
        self._adaptive_boxes: dict[str, Gtk.Box] = {}
        self._adaptive_previews: dict[str, SchemePreview] = {}
        self._palette_sources: dict[str, Palette | PalettePair | None] = {}
        self._palette_buttons: dict[str, Gtk.CheckButton] = {}
        self._policy_rows: dict[str, Gtk.ListBoxRow] = {}
        self._policy_models: dict[
            str,
            tuple[str, pairings.PalettePolicy, Palette | PalettePair | None, Path],
        ] = {}
        self._policy_render_key: object = None
        self._reflecting_policy = False
        self._palette_catalog = palette_catalog or PaletteCatalog()
        self._owns_palette_catalog = palette_catalog is None
        self._catalog_state = self._palette_catalog.state
        self._catalog_listener = self._palette_catalog.subscribe(self._catalog_changed)
        self._palette_limit = PALETTE_PAGE_SIZE

        self._still_cards: dict[_StillCard, MediaItem] = {}
        self._still_cards_by_path: dict[Path, _StillCard] = {}
        self._still_positions: dict[Path, int] = {}
        self._still_inventory: tuple[MediaItem, ...] = ()
        self._still_selected: Path | None = None
        self._still_limit = STILL_PICKER_PAGE_SIZE
        self._reflecting_still = False

        self._editor_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._editor.set_margin_top(18)
        self._editor.set_margin_bottom(24)
        self._editor.set_margin_start(24)
        self._editor.set_margin_end(24)
        self._editor_scroll.set_child(self._editor)

        self.append(self._editor_scroll)
        self._show_empty()
        self._palette_catalog.ensure_loaded()

    def shutdown(self) -> None:
        self._palette_catalog.unsubscribe(self._catalog_listener)
        if self._owns_palette_catalog:
            self._palette_catalog.shutdown()
        self._preview_loader.shutdown()
        self._thumbnail_loader.shutdown()

    def refresh(self, session: Session) -> None:
        """Refresh the item being edited without inventing a second item list."""
        self._session = session
        if self._selected is None:
            return
        current = session.library.find(self._selected.path)
        if current is None:
            self._selected = None
            self._show_empty()
        else:
            self._selected = current
            bundle = session.pairings.resolve_accepted(current, session.library)
            if self._rendered != self._editor_key(current, bundle):
                self._show_editor(current, bundle=bundle)
            else:
                # A rescan can add a reusable picture without changing the
                # item being edited. Diff just the picker so search, scroll and
                # keyboard focus remain exactly where the author left them.
                self._sync_still_picker(current, bundle)

    def edit(self, session: Session, item: MediaItem) -> None:
        """Open ``item`` as the one implicit pairing it already represents."""
        self._session = session
        self._selected = item
        bundle = session.pairings.resolve_accepted(item, session.library)
        if self._rendered != self._editor_key(item, bundle):
            self._show_editor(item, bundle=bundle)

    @staticmethod
    def _editor_key(
        item: MediaItem,
        bundle: pairings.Pairing,
    ) -> tuple[MediaItem, pairings.Pairing]:
        """Canonical inputs, across the short scan-after-write window.

        The pairing store learns a manual still synchronously.  The library's
        moving item learns the same derived ``paired_still`` on its following
        asynchronous rescan.  Normalising both sides prevents either phase
        from looking like a new editor while still letting every other item
        field and pairing choice invalidate the surface.
        """
        resolved_item = item.with_still(bundle.still) if item.is_moving else item
        return resolved_item, bundle

    def _clear_editor(self) -> None:
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._adaptive_boxes.clear()
        self._adaptive_previews.clear()
        self._palette_sources.clear()
        self._palette_buttons.clear()
        self._policy_rows.clear()
        self._policy_models.clear()
        self._policy_render_key = None
        self._still_cards.clear()
        self._still_cards_by_path.clear()
        self._still_positions.clear()

    def _show_empty(self) -> None:
        self._clear_editor()
        self._rendered = None
        status = Adw.StatusPage(
            title="Choose media first",
            description="Return to Media/Pairings and choose an item to configure.",
            icon_name="image-x-generic-symbolic",
        )
        status.set_vexpand(True)
        self._editor.append(status)

    def _show_editor(
        self,
        item: MediaItem,
        *,
        bundle: pairings.Pairing | None = None,
        restore_focus: str = "",
    ) -> None:
        session = self._session
        if session is None:
            return
        scroll = self._editor_scroll.get_vadjustment().get_value()
        self._clear_editor()
        bundle = bundle or session.pairings.resolve_accepted(item, session.library)
        self._rendered = self._editor_key(item, bundle)

        back = Gtk.Button(label="Back to Media/Pairings", icon_name="go-previous-symbolic")
        back.set_halign(Gtk.Align.START)
        back.connect("clicked", lambda _button: self._on_back())
        self._editor.append(back)

        title = Gtk.Label(label=item.name, xalign=0.0, selectable=True)
        title.add_css_class("title-1")
        self._editor.append(title)
        source = Gtk.Label(label=str(item.path), xalign=0.0, selectable=True, wrap=True)
        source.add_css_class("dim-label")
        self._editor.append(source)

        self._health_group: Adw.PreferencesGroup | None = None
        self._health_row: Adw.ActionRow | None = None
        self._health_action: Gtk.Button | None = None
        if bundle.health.is_borked:
            self._health_group = Adw.PreferencesGroup(
                title="Borked wallpaper · known renderer crasher",
                description=(
                    f"{bundle.health.reason}\n\n"
                    "Playback, Quick choice, and transport retry are disabled. If this "
                    "wallpaper was active when it failed, its paired still may remain visible; "
                    "the service will not select it again."
                ),
            )
            self._health_group.add_css_class("error")
            health_row = Adw.ActionRow(
                title="Playback disabled · remove or uninstall to reset",
            )
            self._health_row = health_row
            removable = manage.is_removable(item, session.library.roots)
            if removable and self._on_remove is not None:
                health_row.set_subtitle(
                    f"Reported by {bundle.health.source or 'the runtime'} · "
                    "removing it also clears its saved pairing and Borked marker"
                )
                self._health_action = Gtk.Button(
                    label="Delete wallpaper…" if item.deletable else "Move to Trash"
                )
                self._health_action.add_css_class("destructive-action")
                self._health_action.set_valign(Gtk.Align.CENTER)
                self._health_action.connect(
                    "clicked", lambda _button: self._on_remove(item) if self._on_remove else None
                )
                health_row.add_suffix(self._health_action)
            else:
                unavailable = (
                    "Delete unavailable here; uninstall this Workshop item in Steam, then "
                    "rescan. A later reinstall starts clean"
                    if item.kind is Kind.SCENE or item.provider == scan.WORKSHOP_PROVIDER
                    else "Delete unavailable here; remove it from its source, then rescan"
                )
                health_row.set_subtitle(unavailable)
            self._health_group.add(health_row)
            self._editor.append(self._health_group)

        still_group = self._build_still_picker(item, bundle)
        if item.kind is Kind.SCENE:
            regenerate = Adw.ActionRow(
                title="Regenerate automatic scene still",
                subtitle="Recapture at the target display's physical aspect and resolution",
            )
            button = Gtk.Button(label="Regenerate")
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", lambda _button: self._regenerate_scene(item))
            regenerate.add_suffix(button)
            still_group.add(regenerate)
        self._editor.append(still_group)

        colour_group = Adw.PreferencesGroup(
            title="Colour policy",
            description="The swatches preview the colours this pairing will ask Noctalia to use.",
        )
        self._mode_row = Adw.ComboRow(
            title="Theme mode",
            model=Gtk.StringList.new([label for label, _mode in _MODES]),
        )
        self._mode_row.set_selected(
            next(i for i, choice in enumerate(_MODES) if choice[1] is bundle.palette.mode)
        )
        self._mode_row.connect("notify::selected", self._make_mode_changed(item))
        colour_group.add(self._mode_row)

        self._palette_search = Gtk.SearchEntry(
            placeholder_text="Search installed palettes",
        )
        self._palette_search.connect("search-changed", self._palette_search_changed)
        colour_group.add(self._palette_search)

        self._policy_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._policy_list.add_css_class("boxed-list")
        colour_group.add(self._policy_list)

        self._palette_status = Gtk.Label(xalign=0.0, wrap=True)
        self._palette_status.add_css_class("caption")
        self._palette_status.add_css_class("dim-label")
        colour_group.add(self._palette_status)

        self._palette_more = Gtk.Button()
        self._palette_more.set_halign(Gtk.Align.CENTER)
        self._palette_more.connect("clicked", self._show_more_palettes)
        colour_group.add(self._palette_more)
        self._palette_limit = PALETTE_PAGE_SIZE
        self._populate_policy_list(item, bundle)
        self._editor.append(colour_group)
        self._refresh_palette_swatches(bundle.palette.mode)

        self._reset_button = Gtk.Button(label="Reset this pairing to automatic defaults")
        self._reset_button.add_css_class("destructive-action")
        self._reset_button.set_halign(Gtk.Align.START)
        self._reset_button.set_sensitive(bundle.customized)
        self._reset_button.connect("clicked", lambda _button: self._reset(item))
        self._editor.append(self._reset_button)

        self._request_adaptive_previews(bundle)
        GLib.idle_add(self._restore_interaction, scroll, restore_focus)

    def _populate_policy_list(self, item: MediaItem, bundle: pairings.Pairing) -> None:
        """Rebuild only the bounded policy rows; keep search and page identity."""
        render_key = (
            item.path,
            bundle.palette,
            self._catalog_state.discovery,
            self._palette_search.get_text(),
            self._palette_limit,
        )
        if render_key == self._policy_render_key:
            self._update_palette_navigation()
            return
        self._policy_render_key = render_key
        old_rows = dict(self._policy_rows)
        old_models = dict(self._policy_models)
        old_buttons = dict(self._palette_buttons)
        old_boxes = dict(self._adaptive_boxes)
        while (child := self._policy_list.get_first_child()) is not None:
            self._policy_list.remove(child)
        self._adaptive_boxes.clear()
        self._palette_sources.clear()
        self._palette_buttons.clear()
        self._policy_rows.clear()
        self._policy_models.clear()
        policies = self._policies(bundle)
        wanted_keys = {policy.encode() for _label, policy, _palette in policies}
        for key, button in old_buttons.items():
            if key not in wanted_keys:
                button.set_group(None)
        first: Gtk.CheckButton | None = None
        self._reflecting_policy = True
        try:
            for label, policy, palette in policies:
                key = policy.encode()
                model = (label, policy, palette, item.path)
                row = old_rows.get(key) if old_models.get(key) == model else None
                radio = old_buttons.get(key) if row is not None else None
                preview_box = old_boxes.get(key) if row is not None else None
                if radio is None or preview_box is None:
                    row = Gtk.ListBoxRow(activatable=False)
                    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                    content.set_margin_top(9)
                    content.set_margin_bottom(9)
                    content.set_margin_start(10)
                    content.set_margin_end(10)
                    radio = Gtk.CheckButton()
                    radio.connect("toggled", self._make_policy_changed(item, policy))
                    content.append(radio)
                    words = Gtk.Box(
                        orientation=Gtk.Orientation.VERTICAL,
                        spacing=3,
                        hexpand=True,
                    )
                    name = Gtk.Label(label=label, xalign=0.0)
                    name.add_css_class("heading")
                    words.append(name)
                    preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                    words.append(preview_box)
                    content.append(words)
                    row.set_child(content)
                assert row is not None
                if first is None:
                    first = radio
                else:
                    radio.set_group(first)
                selected_name = bundle.palette.name
                if bundle.palette.is_adaptive and not selected_name:
                    selected_name = self._app.settings.preview_scheme
                radio.set_active(
                    policy.kind == bundle.palette.kind and policy.name == selected_name
                )
                self._palette_buttons[key] = radio
                self._adaptive_boxes[key] = preview_box
                self._palette_sources[key] = palette
                self._policy_rows[key] = row
                self._policy_models[key] = model
                self._policy_list.append(row)
        finally:
            self._reflecting_policy = False
        self._update_palette_navigation()

    def _palette_entries(self) -> tuple[palettes.PaletteEntry, ...]:
        query = self._palette_search.get_text().strip().casefold()
        return tuple(
            entry
            for entry in self._catalog_state.discovery.entries
            if entry.origin.is_applicable
            and (
                not query
                or query in entry.name.casefold()
                or query in entry.origin.label.casefold()
            )
        )

    def _update_palette_navigation(self) -> None:
        entries = self._palette_entries()
        remaining = max(0, len(entries) - self._palette_limit)
        self._palette_more.set_visible(remaining > 0)
        self._palette_more.set_label(
            f"Show {min(PALETTE_PAGE_SIZE, remaining)} more installed palettes"
        )
        state = self._catalog_state
        if state.loading:
            message = (
                "Loading installed palettes…"
                if not state.discovery.entries
                else "Refreshing installed palettes…"
            )
        elif state.phase == "error":
            message = f"Installed palettes could not be loaded: {state.error}"
        elif not entries and self._palette_search.get_text().strip():
            message = "No installed palettes match this search."
        else:
            message = ""
        self._palette_status.set_label(message)
        self._palette_status.set_visible(bool(message))

    def _palette_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._palette_limit = PALETTE_PAGE_SIZE
        item = self._selected
        session = self._session
        if item is None or session is None:
            return
        bundle = session.pairings.resolve_accepted(item, session.library)
        self._populate_policy_list(item, bundle)
        self._refresh_palette_swatches(bundle.palette.mode)

    def _show_more_palettes(self, _button: Gtk.Button) -> None:
        self._palette_limit += PALETTE_PAGE_SIZE
        item = self._selected
        session = self._session
        if item is None or session is None:
            return
        bundle = session.pairings.resolve_accepted(item, session.library)
        self._populate_policy_list(item, bundle)
        self._refresh_palette_swatches(bundle.palette.mode)

    def _catalog_changed(self, state: CatalogState) -> None:
        self._catalog_state = state
        item = self._selected
        session = self._session
        if item is None or session is None or not hasattr(self, "_policy_list"):
            return
        bundle = session.pairings.resolve_accepted(item, session.library)
        self._populate_policy_list(item, bundle)
        self._refresh_palette_swatches(bundle.palette.mode)

    def _build_still_picker(
        self, item: MediaItem, bundle: pairings.Pairing
    ) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Representative still",
            description=(
                "Used behind videos and scenes, on the lock screen, and as the input "
                "to adaptive colours. Search every still in the library; pictures "
                "already paired elsewhere remain reusable."
            ),
        )

        self._automatic_still = Gtk.ToggleButton(label="Use automatic default")
        self._automatic_still.set_halign(Gtk.Align.START)
        self._automatic_still.connect(
            "toggled",
            lambda button: self._choose_picker_still(item, None) if button.get_active() else None,
        )
        group.add(self._automatic_still)

        self._still_search = Gtk.SearchEntry(placeholder_text="Search still images")
        self._still_search.connect("search-changed", self._still_search_changed)
        group.add(self._still_search)

        self._still_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            column_spacing=10,
            row_spacing=10,
            min_children_per_line=1,
            max_children_per_line=5,
            valign=Gtk.Align.START,
        )
        self._still_flow.set_sort_func(self._compare_still_cards)
        self._still_flow.set_margin_top(6)
        self._still_flow.set_margin_bottom(6)
        self._still_scroll = Gtk.ScrolledWindow(
            min_content_height=220,
            max_content_height=320,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        self._still_scroll.set_child(self._still_flow)
        group.add(self._still_scroll)

        self._still_more = Gtk.Button()
        self._still_more.set_halign(Gtk.Align.CENTER)
        self._still_more.connect("clicked", self._show_more_stills)
        group.add(self._still_more)

        self._manual_still = Adw.ActionRow(
            title="Browse indexed library images…",
            subtitle="Choose a still already found by the latest library refresh",
        )
        choose = Gtk.Button(label="Choose")
        choose.set_valign(Gtk.Align.CENTER)
        choose.connect("clicked", lambda _button: self._choose_manual_still(item))
        self._manual_still.add_suffix(choose)
        group.add(self._manual_still)

        self._still_limit = STILL_PICKER_PAGE_SIZE
        self._sync_still_picker(item, bundle)
        return group

    def _explicit_still(self, bundle: pairings.Pairing) -> Path | None:
        session = self._session
        saved = session.pairings.get(bundle.identity) if session is not None else None
        return saved.still if saved is not None else None

    def _sync_still_picker(self, item: MediaItem, bundle: pairings.Pairing) -> None:
        """Diff the bounded thumbnail page and reflect its authored choice."""
        session = self._session
        if session is None or not hasattr(self, "_still_flow"):
            return
        self._still_inventory = session.library.reusable_stills
        self._still_selected = self._explicit_still(bundle)
        self._reconcile_still_cards(item)
        self._reflect_still_selection()

    def _matching_stills(self) -> tuple[MediaItem, ...]:
        query = self._still_search.get_text().strip().casefold()
        if not query:
            return self._still_inventory
        return tuple(
            candidate
            for candidate in self._still_inventory
            if query in candidate.name.casefold()
            or query in str(candidate.path).casefold()
            or query in candidate.provider.casefold()
        )

    def _visible_stills(self) -> tuple[MediaItem, ...]:
        matches = self._matching_stills()
        visible = list(matches[: self._still_limit])
        # Keep an existing choice visible on opening a large inventory without
        # constructing every card before it. A search that deliberately
        # excludes it is allowed to hide it; the manual/current subtitle still
        # names the choice.
        if not self._still_search.get_text().strip() and self._still_selected is not None:
            chosen = next(
                (item for item in self._still_inventory if item.path == self._still_selected),
                None,
            )
            if chosen is not None and all(item.path != chosen.path for item in visible):
                visible.append(chosen)
        return tuple(visible)

    def _reconcile_still_cards(self, item: MediaItem) -> None:
        visible = self._visible_stills()
        incoming = {candidate.path: candidate for candidate in visible}
        for path, card in list(self._still_cards_by_path.items()):
            replacement = incoming.get(path)
            if replacement is None or replacement != card.item:
                self._still_flow.remove(card)
                self._still_cards.pop(card, None)
                del self._still_cards_by_path[path]

        self._still_positions = {candidate.path: index for index, candidate in enumerate(visible)}
        for candidate in visible:
            if candidate.path in self._still_cards_by_path:
                continue
            card = _StillCard(candidate, self._make_still_card_chosen(item))
            card.set_group(self._automatic_still)
            self._still_cards[card] = candidate
            self._still_cards_by_path[candidate.path] = card
            self._still_flow.append(card)
            self._thumbnail_loader.request(candidate, card.show_thumbnail)
        self._still_flow.invalidate_sort()

        remaining = max(0, len(self._matching_stills()) - self._still_limit)
        self._still_more.set_visible(remaining > 0)
        self._still_more.set_label(f"Show {min(STILL_PICKER_PAGE_SIZE, remaining)} more")

    def _make_still_card_chosen(self, item: MediaItem) -> Callable[[MediaItem], None]:
        def choose(chosen: MediaItem) -> None:
            self._choose_picker_still(item, chosen.path)

        return choose

    def _compare_still_cards(self, first: Gtk.FlowBoxChild, second: Gtk.FlowBoxChild) -> int:
        first_card = first.get_child()
        second_card = second.get_child()
        first_item = (
            self._still_cards.get(first_card) if isinstance(first_card, _StillCard) else None
        )
        second_item = (
            self._still_cards.get(second_card) if isinstance(second_card, _StillCard) else None
        )
        end = len(self._still_positions)
        first_rank = self._still_positions.get(first_item.path, end) if first_item else end
        second_rank = self._still_positions.get(second_item.path, end) if second_item else end
        return first_rank - second_rank

    def _still_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._still_limit = STILL_PICKER_PAGE_SIZE
        item = self._selected
        if item is not None:
            self._reconcile_still_cards(item)
            self._reflect_still_selection()

    def _show_more_stills(self, _button: Gtk.Button) -> None:
        self._still_limit += STILL_PICKER_PAGE_SIZE
        item = self._selected
        if item is not None:
            self._reconcile_still_cards(item)
            self._reflect_still_selection()

    def _reflect_still_selection(self) -> None:
        self._reflecting_still = True
        try:
            self._automatic_still.set_active(self._still_selected is None)
            for path, card in self._still_cards_by_path.items():
                card.set_active(path == self._still_selected)
        finally:
            self._reflecting_still = False
        known = self._still_selected is None or any(
            candidate.path == self._still_selected for candidate in self._still_inventory
        )
        self._manual_still.set_subtitle(
            (
                f"Saved choice is no longer indexed: {self._still_selected}. "
                "Move or copy it into a library folder, or add its folder in Settings, "
                "then refresh."
            )
            if self._still_selected is not None and not known
            else "Choose a still already found by the latest library refresh"
        )

    def _choose_picker_still(self, item: MediaItem, still: Path | None) -> None:
        if self._reflecting_still or still == self._still_selected:
            return
        try:
            if still is not None:
                still = self._app.session.library.require_representative_still(still).path
        except RepresentativeStillError as error:
            self._app.window_report(str(error))
            self._reflect_still_selection()
            return
        store = self._app.session.pairings

        def saved(result: Any) -> None:
            current = self._app.adopt_pairing_still(result.item, result.effective_still)
            bundle = self._app.session.pairings.resolve_accepted(
                current,
                self._app.session.library,
            )
            self._rendered = self._editor_key(current, bundle)
            self._still_selected = still
            self._reflect_still_selection()
            self._request_adaptive_previews(bundle)
            self._app.pairing_changed(current)

        def failed(error: str) -> None:
            self._app.window_report(str(error))
            self._reflect_still_selection()

        self._app.authoring_action_async(
            lambda: store.choose_still(item, still),
            saved,
            prepare=lambda: self._app.prepare_still_pairing_mutation(
                item,
                still,
                lambda current_store, current, current_still: current_store.choose_still(
                    current, current_still
                ),
            ),
            failure=failed,
        )

    def _restore_interaction(self, scroll: float, focus: str) -> bool:
        self._editor_scroll.get_vadjustment().set_value(scroll)
        if focus == "still":
            self._still_search.grab_focus()
        elif focus == "reset":
            self._reset_button.grab_focus()
        return GLib.SOURCE_REMOVE

    def _policies(
        self, bundle: pairings.Pairing
    ) -> list[tuple[str, pairings.PalettePolicy, Palette | PalettePair | None]]:
        resolved = self._app.resolved_palette
        choices: list[tuple[str, pairings.PalettePolicy, Palette | PalettePair | None]] = [
            *(
                (
                    f"Adaptive · {scheme}",
                    pairings.PalettePolicy(pairings.ADAPTIVE, scheme),
                    None,
                )
                for scheme in noctalia.ALL_SCHEMES
            ),
            (
                "Keep the current colours",
                pairings.PalettePolicy(kind=pairings.KEEP),
                resolved.palette if resolved is not None else None,
            ),
        ]
        entries = self._palette_entries()
        visible = list(entries[: self._palette_limit])
        selected = next(
            (
                entry
                for entry in self._catalog_state.discovery.entries
                if entry.origin.value == bundle.palette.kind and entry.name == bundle.palette.name
            ),
            None,
        )
        if selected is not None and selected not in visible:
            # The durable choice stays visible even beyond the current page or
            # outside the query, instead of making another radio look selected.
            visible.append(selected)
        for entry in visible:
            policy = pairings.PalettePolicy(entry.origin.value, entry.name)
            choices.append((f"{entry.origin.label} · {entry.name}", policy, entry.colours))
        # Preserve a policy whose source is temporarily unavailable, rather
        # than making the editor silently select something else.
        selected_name = bundle.palette.name
        if bundle.palette.is_adaptive and not selected_name:
            selected_name = self._app.settings.preview_scheme
        if not any(
            policy.kind == bundle.palette.kind and policy.name == selected_name
            for _label, policy, _palette in choices
        ):
            choices.append((bundle.palette.encode(), bundle.palette, None))
        return choices

    def _preview_mode(self, selected: pairings.Mode) -> PaletteMode:
        """Return the concrete light/dark variant selected by the editor."""
        if selected is pairings.Mode.LIGHT:
            return "light"
        if selected is pairings.Mode.DARK:
            return "dark"
        # Automatic and Keep do not name one variant. In those cases preview
        # the live shell mode rather than silently hard-coding dark.
        resolved = self._app.resolved_palette
        return resolved.palette.mode if resolved is not None else "dark"

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    def _refresh_palette_swatches(self, selected: pairings.Mode) -> None:
        for key in self._adaptive_boxes:
            self._refresh_palette_swatch(key, selected)

    def _refresh_palette_swatch(self, key: str, selected: pairings.Mode) -> None:
        box = self._adaptive_boxes.get(key)
        if box is None:
            return
        mode = self._preview_mode(selected)
        self._clear_box(box)
        source = self._palette_sources.get(key)
        if key.startswith(f"{pairings.ADAPTIVE}:"):
            scheme = key.partition(":")[2]
            preview = self._adaptive_previews.get(scheme)
            if preview is None:
                message = "Generating preview…"
            elif preview.colours is None:
                message = preview.error or "Preview unavailable"
            else:
                box.append(swatch_strip(preview.colours.for_mode(mode), height=18, width=24))
                return
        elif isinstance(source, PalettePair):
            box.append(swatch_strip(source.for_mode(mode), height=18, width=24))
            return
        elif isinstance(source, Palette):
            # Keep-current has only the variant Noctalia last rendered.
            box.append(swatch_strip(source, height=18, width=24))
            return
        else:
            message = "Noctalia does not expose this built-in palette until it is applied"
        note = Gtk.Label(label=message, xalign=0.0, wrap=True)
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        box.append(note)

    def _request_adaptive_previews(self, bundle: pairings.Pairing) -> None:
        self._adaptive_previews.clear()
        self._refresh_palette_swatches(bundle.palette.mode)
        if bundle.still is None:
            return
        for scheme in noctalia.ALL_SCHEMES:
            self._preview_loader.request(bundle.still, scheme, self._on_adaptive_preview)

    def _on_adaptive_preview(self, preview: SchemePreview) -> None:
        box = self._adaptive_boxes.get(f"{pairings.ADAPTIVE}:{preview.scheme}")
        item = self._selected
        session = self._session
        if box is None or item is None or session is None:
            return
        bundle = session.pairings.resolve_accepted(item, session.library)
        if bundle.still != preview.image:
            return
        self._adaptive_previews[preview.scheme] = preview
        self._refresh_palette_swatch(f"{pairings.ADAPTIVE}:{preview.scheme}", bundle.palette.mode)

    def _make_mode_changed(self, item: MediaItem) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            if self._reflecting_policy:
                return
            index = row.get_selected()
            if index >= len(_MODES):
                return
            session = self._app.session
            current = session.pairings.resolve_accepted(item, session.library).palette
            wanted = pairings.PalettePolicy(current.kind, current.name, _MODES[index][1])
            if wanted == current:
                return
            if self._store_policy(item, wanted):
                self._refresh_palette_swatches(wanted.mode)
            else:
                self._reflect_palette_policy(current)

        return changed

    def _make_policy_changed(self, item: MediaItem, policy: pairings.PalettePolicy) -> Any:
        def changed(button: Gtk.CheckButton) -> None:
            if self._reflecting_policy or not button.get_active():
                return
            session = self._app.session
            current = session.pairings.resolve_accepted(item, session.library).palette
            wanted = pairings.PalettePolicy(policy.kind, policy.name, current.mode)
            if wanted == current:
                return
            if not self._store_policy(item, wanted):
                self._reflect_palette_policy(current)

        return changed

    def _reflect_palette_policy(self, policy: pairings.PalettePolicy) -> None:
        """Restore colour controls to the last durable pairing after a failed write."""
        selected_name = policy.name
        if policy.is_adaptive and not selected_name:
            selected_name = self._app.settings.preview_scheme
        key = pairings.PalettePolicy(policy.kind, selected_name).encode()
        mode = next(
            (index for index, (_label, choice) in enumerate(_MODES) if choice is policy.mode),
            0,
        )
        self._reflecting_policy = True
        try:
            self._mode_row.set_selected(mode)
            button = self._palette_buttons.get(key)
            if button is not None:
                button.set_active(True)
        finally:
            self._reflecting_policy = False
        self._refresh_palette_swatches(policy.mode)

    def _store_policy(self, item: MediaItem, policy: pairings.PalettePolicy) -> bool:
        store = self._app.session.pairings

        def saved(_record: pairings.Pairing) -> None:
            session = self._app.session
            bundle = session.pairings.resolve_accepted(item, session.library)
            self._rendered = self._editor_key(item, bundle)
            self._app.pairing_changed(item)

        def failed(error: str) -> None:
            self._app.window_report(f"Colour policy was not saved; nothing changed: {error}")
            current = self._app.session.pairings.resolve_accepted(
                item,
                self._app.session.library,
            ).palette
            self._reflect_palette_policy(current)

        return self._app.authoring_action_async(
            lambda: store.choose_palette(item, policy),
            saved,
            prepare=lambda: self._app.prepare_pairing_mutation(
                item,
                lambda current_store, current: current_store.choose_palette(current, policy),
            ),
            failure=failed,
        )

    def _choose_manual_still(self, item: MediaItem) -> None:
        dialog = Gtk.FileDialog(title=f"Choose a still for {item.name}", modal=True)
        images = Gtk.FileFilter()
        images.set_name("Images")
        for extension in sorted(IMAGE_EXTENSIONS):
            images.add_pattern(f"*{extension}")
            images.add_pattern(f"*{extension.upper()}")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.open(parent, None, self._make_manual_receiver(item))

    def _make_manual_receiver(self, item: MediaItem) -> Any:
        def chosen(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                picked = dialog.open_finish(result)
            except GLib.Error:
                return
            raw = picked.get_path() if picked is not None else None
            if raw is None:
                self._app.window_report("That image is not on this machine's filesystem")
                return
            self._choose_picker_still(item, Path(raw))

        return chosen

    def _reset(self, item: MediaItem) -> None:
        store = self._app.session.pairings

        def saved(result: Any) -> None:
            current = self._app.adopt_pairing_still(result.item, result.effective_still)
            self._app.pairing_changed(current)
            self._show_editor(current, restore_focus="reset")

        self._app.authoring_action_async(
            lambda: store.reset(item),
            saved,
            prepare=lambda: self._app.prepare_pairing_reset(item),
            failure=self._app.window_report,
        )

    def _regenerate_scene(self, item: MediaItem) -> None:
        if self._app.regenerate_scene_still(item):
            self._app.window_report("Regenerating the automatic scene still in the background")
        else:
            self._app.window_report("Choose a library directory before generating scene stills")
