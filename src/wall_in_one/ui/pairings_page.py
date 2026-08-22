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

from wall_in_one.library import pairings
from wall_in_one.library.model import IMAGE_EXTENSIONS, Kind, MediaItem
from wall_in_one.theme import noctalia, palettes
from wall_in_one.theme.palette import Mode as PaletteMode
from wall_in_one.theme.palette import Palette, PalettePair
from wall_in_one.ui.palette_browser import SchemePreview, SchemePreviewLoader, swatch_strip
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

    def __init__(self, application: Application, on_back: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._app = application
        self._on_back = on_back
        self._session: Session | None = None
        self._selected: MediaItem | None = None
        self._rendered: tuple[MediaItem, pairings.Pairing] | None = None
        self._preview_loader = SchemePreviewLoader(max_workers=2)
        self._thumbnail_loader = ThumbnailLoader(max_workers=2)
        self._adaptive_boxes: dict[str, Gtk.Box] = {}
        self._adaptive_previews: dict[str, SchemePreview] = {}
        self._palette_sources: dict[str, Palette | PalettePair | None] = {}

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

    def shutdown(self) -> None:
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
            bundle = session.pairings.resolve(current, session.library.roots)
            if self._rendered != (current, bundle):
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
        bundle = session.pairings.resolve(item, session.library.roots)
        if self._rendered != (item, bundle):
            self._show_editor(item, bundle=bundle)

    def _clear_editor(self) -> None:
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._adaptive_boxes.clear()
        self._adaptive_previews.clear()
        self._palette_sources.clear()
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
        bundle = bundle or session.pairings.resolve(item, session.library.roots)
        rendered_item = item.with_still(bundle.still) if item.is_moving else item
        self._rendered = (rendered_item, bundle)

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
        self._retry_button: Gtk.Button | None = None
        if bundle.health.is_borked:
            self._health_group = Adw.PreferencesGroup(
                title="Borked wallpaper · automatic playback is taboo",
                description=(
                    f"{bundle.health.reason}\n\n"
                    "The service keeps using the paired still and skips this wallpaper "
                    "during automatic rotation. Clear the judgement only when you want "
                    "to try the renderer again."
                ),
            )
            self._health_group.add_css_class("error")
            health_row = Adw.ActionRow(
                title="Static fallback is active",
                subtitle=f"Reported by {bundle.health.source or 'the runtime'}",
            )
            self._retry_button = Gtk.Button(label="Clear taboo and retry now")
            self._retry_button.add_css_class("suggested-action")
            self._retry_button.set_valign(Gtk.Align.CENTER)
            self._retry_button.connect("clicked", lambda _button: self._app.retry_borked(item))
            health_row.add_suffix(self._retry_button)
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

        policy_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        policy_list.add_css_class("boxed-list")
        first: Gtk.CheckButton | None = None
        for label, policy, palette in self._policies(bundle):
            row = Gtk.ListBoxRow(activatable=False)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.set_margin_top(9)
            content.set_margin_bottom(9)
            content.set_margin_start(10)
            content.set_margin_end(10)
            radio = Gtk.CheckButton()
            if first is None:
                first = radio
            else:
                radio.set_group(first)
            selected_name = bundle.palette.name
            if bundle.palette.is_adaptive and not selected_name:
                selected_name = self._app.settings.preview_scheme
            radio.set_active(policy.kind == bundle.palette.kind and policy.name == selected_name)
            radio.connect("toggled", self._make_policy_changed(item, policy))
            content.append(radio)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            name = Gtk.Label(label=label, xalign=0.0)
            name.add_css_class("heading")
            words.append(name)
            preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            words.append(preview_box)
            key = policy.encode()
            self._adaptive_boxes[key] = preview_box
            self._palette_sources[key] = palette
            content.append(words)
            row.set_child(content)
            policy_list.append(row)
        colour_group.add(policy_list)
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
            title="Choose another image…",
            subtitle="Manual escape hatch for an image outside the indexed library",
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
            str(self._still_selected)
            if self._still_selected is not None and not known
            else "Manual escape hatch for an image outside the indexed library"
        )

    def _choose_picker_still(self, item: MediaItem, still: Path | None) -> None:
        if self._reflecting_still or still == self._still_selected:
            return
        try:
            self._app.session.pairings.choose_still(item, still)
        except pairings.PairingError as error:
            self._app.window_report(str(error))
            self._reflect_still_selection()
            return
        bundle = self._app.session.pairings.resolve(item, self._app.session.library.roots)
        rendered_item = item.with_still(bundle.still) if item.is_moving else item
        self._rendered = (rendered_item, bundle)
        self._still_selected = still
        self._reflect_still_selection()
        self._request_adaptive_previews(bundle)
        self._app.pairing_changed(item)

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
        for entry in palettes.discover().entries:
            if not entry.origin.is_applicable:
                continue
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
        if bundle.still is None or not bundle.still.is_file():
            return
        for scheme in noctalia.ALL_SCHEMES:
            self._preview_loader.request(bundle.still, scheme, self._on_adaptive_preview)

    def _on_adaptive_preview(self, preview: SchemePreview) -> None:
        box = self._adaptive_boxes.get(f"{pairings.ADAPTIVE}:{preview.scheme}")
        item = self._selected
        session = self._session
        if box is None or item is None or session is None:
            return
        bundle = session.pairings.resolve(item, session.library.roots)
        if bundle.still != preview.image:
            return
        self._adaptive_previews[preview.scheme] = preview
        self._refresh_palette_swatch(f"{pairings.ADAPTIVE}:{preview.scheme}", bundle.palette.mode)

    def _make_mode_changed(self, item: MediaItem) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            index = row.get_selected()
            if index >= len(_MODES):
                return
            session = self._app.session
            current = session.pairings.resolve(item, session.library.roots).palette
            stored = self._store_policy(
                item,
                pairings.PalettePolicy(current.kind, current.name, _MODES[index][1]),
            )
            self._refresh_palette_swatches(_MODES[index][1] if stored else current.mode)

        return changed

    def _make_policy_changed(self, item: MediaItem, policy: pairings.PalettePolicy) -> Any:
        def changed(button: Gtk.CheckButton) -> None:
            if not button.get_active():
                return
            session = self._app.session
            mode = session.pairings.resolve(item, session.library.roots).palette.mode
            self._store_policy(item, pairings.PalettePolicy(policy.kind, policy.name, mode))

        return changed

    def _store_policy(self, item: MediaItem, policy: pairings.PalettePolicy) -> bool:
        try:
            self._app.session.pairings.choose_palette(item, policy)
        except pairings.PairingError as error:
            self._app.window_report(str(error))
            return False
        session = self._app.session
        bundle = session.pairings.resolve(item, session.library.roots)
        rendered_item = item.with_still(bundle.still) if item.is_moving else item
        self._rendered = (rendered_item, bundle)
        self._app.pairing_changed(item)
        return True

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
        try:
            self._app.session.pairings.reset(item)
        except pairings.PairingError as error:
            self._app.window_report(str(error))
            return
        self._app.pairing_changed(item)
        self._show_editor(item, restore_focus="reset")

    def _regenerate_scene(self, item: MediaItem) -> None:
        if self._app.regenerate_scene_still(item):
            self._app.window_report("Regenerating the automatic scene still in the background")
        else:
            self._app.window_report("Choose a library directory before generating scene stills")
