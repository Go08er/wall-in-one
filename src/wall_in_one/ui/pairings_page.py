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
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from wall_in_one.library import pairings
from wall_in_one.library.model import IMAGE_EXTENSIONS, Kind, MediaItem
from wall_in_one.theme import noctalia, palettes
from wall_in_one.theme.palette import Palette
from wall_in_one.ui.palette_browser import SchemePreview, SchemePreviewLoader, swatch_strip

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


_MODES: tuple[tuple[str, pairings.Mode], ...] = (
    ("Keep current mode", pairings.Mode.KEEP),
    ("Dark", pairings.Mode.DARK),
    ("Light", pairings.Mode.LIGHT),
    ("Automatic", pairings.Mode.AUTO),
)


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
        self._adaptive_boxes: dict[str, Gtk.Box] = {}

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

        still_group = Adw.PreferencesGroup(
            title="Representative still",
            description=(
                "Used behind videos and scenes, on the lock screen, and as the input "
                "to adaptive colours."
            ),
        )
        stills = tuple(session.library.stills)
        labels = ["Automatic default", *(candidate.name for candidate in stills)]
        self._still_row = Adw.ComboRow(
            title="Still from your library", model=Gtk.StringList.new(labels)
        )
        selected = 0
        if bundle.still is not None:
            for index, candidate in enumerate(stills, start=1):
                if candidate.path == bundle.still:
                    selected = index
                    break
        self._still_row.set_selected(selected)
        self._still_row.connect("notify::selected", self._make_still_changed(item, stills))
        still_group.add(self._still_row)

        manual = Adw.ActionRow(
            title="Choose another image…",
            subtitle=(
                str(bundle.still)
                if bundle.still is not None and selected == 0
                else "Manual escape hatch for an image outside the indexed library"
            ),
        )
        choose = Gtk.Button(label="Choose")
        choose.set_valign(Gtk.Align.CENTER)
        choose.connect("clicked", lambda _button: self._choose_manual_still(item))
        manual.add_suffix(choose)
        still_group.add(manual)
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
        mode_row = Adw.ComboRow(
            title="Theme mode",
            model=Gtk.StringList.new([label for label, _mode in _MODES]),
        )
        mode_row.set_selected(
            next(i for i, choice in enumerate(_MODES) if choice[1] is bundle.palette.mode)
        )
        mode_row.connect("notify::selected", self._make_mode_changed(item))
        colour_group.add(mode_row)

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
            if palette is not None:
                words.append(swatch_strip(palette, height=18, width=24))
            elif policy.kind == pairings.ADAPTIVE:
                self._adaptive_boxes[policy.name] = words
                waiting = Gtk.Label(label="Generating preview…", xalign=0.0)
                waiting.add_css_class("caption")
                waiting.add_css_class("dim-label")
                words.append(waiting)
            else:
                note = Gtk.Label(
                    label="Noctalia does not expose this built-in palette until it is applied",
                    xalign=0.0,
                    wrap=True,
                )
                note.add_css_class("caption")
                note.add_css_class("dim-label")
                words.append(note)
            content.append(words)
            row.set_child(content)
            policy_list.append(row)
        colour_group.add(policy_list)
        self._editor.append(colour_group)

        self._reset_button = Gtk.Button(label="Reset this pairing to automatic defaults")
        self._reset_button.add_css_class("destructive-action")
        self._reset_button.set_halign(Gtk.Align.START)
        self._reset_button.set_sensitive(bundle.customized)
        self._reset_button.connect("clicked", lambda _button: self._reset(item))
        self._editor.append(self._reset_button)

        if bundle.still is not None and bundle.still.is_file():
            for scheme in noctalia.ALL_SCHEMES:
                self._preview_loader.request(
                    bundle.still,
                    scheme,
                    self._on_adaptive_preview,
                )
        GLib.idle_add(self._restore_interaction, scroll, restore_focus)

    def _restore_interaction(self, scroll: float, focus: str) -> bool:
        self._editor_scroll.get_vadjustment().set_value(scroll)
        if focus == "still":
            self._still_row.grab_focus()
        elif focus == "reset":
            self._reset_button.grab_focus()
        return GLib.SOURCE_REMOVE

    def _policies(
        self, bundle: pairings.Pairing
    ) -> list[tuple[str, pairings.PalettePolicy, Palette | None]]:
        resolved = self._app.resolved_palette
        mode = resolved.palette.mode if resolved is not None else "dark"
        choices: list[tuple[str, pairings.PalettePolicy, Palette | None]] = [
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
            choices.append((f"{entry.origin.label} · {entry.name}", policy, entry.for_mode(mode)))
        # Preserve a policy whose source is temporarily unavailable, rather
        # than making the editor silently select something else.
        if not any(
            policy.kind == bundle.palette.kind and policy.name == bundle.palette.name
            for _label, policy, _palette in choices
        ):
            choices.append((bundle.palette.encode(), bundle.palette, None))
        return choices

    def _on_adaptive_preview(self, preview: SchemePreview) -> None:
        box = self._adaptive_boxes.get(preview.scheme)
        item = self._selected
        session = self._session
        if box is None or item is None or session is None:
            return
        bundle = session.pairings.resolve(item, session.library.roots)
        if bundle.still != preview.image:
            return
        first = box.get_first_child()
        child = first.get_next_sibling() if first is not None else None
        while child is not None:
            following = child.get_next_sibling()
            box.remove(child)
            child = following
        if preview.colours is None:
            message = Gtk.Label(label=preview.error or "Preview unavailable", xalign=0.0, wrap=True)
            message.add_css_class("caption")
            message.add_css_class("dim-label")
            box.append(message)
            return
        mode = self._app.resolved_palette.palette.mode if self._app.resolved_palette else "dark"
        box.append(swatch_strip(preview.colours.for_mode(mode), height=18, width=24))

    def _make_still_changed(self, item: MediaItem, stills: tuple[MediaItem, ...]) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            index = row.get_selected()
            still = stills[index - 1].path if 0 < index <= len(stills) else None
            try:
                self._app.session.pairings.choose_still(item, still)
            except pairings.PairingError as error:
                self._app.window_report(str(error))
                return
            self._app.pairing_changed(item)
            self._show_editor(item, restore_focus="still")

        return changed

    def _make_mode_changed(self, item: MediaItem) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            index = row.get_selected()
            if index >= len(_MODES):
                return
            session = self._app.session
            current = session.pairings.resolve(item, session.library.roots).palette
            self._store_policy(
                item,
                pairings.PalettePolicy(current.kind, current.name, _MODES[index][1]),
            )

        return changed

    def _make_policy_changed(self, item: MediaItem, policy: pairings.PalettePolicy) -> Any:
        def changed(button: Gtk.CheckButton) -> None:
            if not button.get_active():
                return
            session = self._app.session
            mode = session.pairings.resolve(item, session.library.roots).palette.mode
            self._store_policy(item, pairings.PalettePolicy(policy.kind, policy.name, mode))

        return changed

    def _store_policy(self, item: MediaItem, policy: pairings.PalettePolicy) -> None:
        try:
            self._app.session.pairings.choose_palette(item, policy)
        except pairings.PairingError as error:
            self._app.window_report(str(error))
            return
        self._app.pairing_changed(item)
        session = self._app.session
        bundle = session.pairings.resolve(item, session.library.roots)
        rendered_item = item.with_still(bundle.still) if item.is_moving else item
        self._rendered = (rendered_item, bundle)

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
            try:
                self._app.session.pairings.choose_still(item, Path(raw))
            except pairings.PairingError as error:
                self._app.window_report(str(error))
                return
            self._app.pairing_changed(item)
            self._show_editor(item, restore_focus="still")

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
