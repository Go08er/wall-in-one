"""The new interface's window: sidebar navigation, one shared header, banners,
pages, the player bar and the frosted backdrop.

`ShellWindow` is the same in the app and in the design prototype: each builds
it over its own `AppState` and hands it the pages to show. The app passes the
Library and "not in the new interface yet" placeholders; the prototype passes
its own pages for the rest. Subclasses add menu sections
(`extra_menu_sections`) and wrap the body (`wrap_body`).

Glass: the window carries ``wio-glass`` in the translucent and frosted styles,
which is what the app stylesheet's glass layers are scoped to
(`wall_in_one.theme.css.glass_layers`). The frosted backdrop is drawn here.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from wall_in_one.ui.next import style
from wall_in_one.ui.next.backdrop import Backdrop
from wall_in_one.ui.next.catalog import quoted
from wall_in_one.ui.next.page import Page, Placeholder
from wall_in_one.ui.next.playerbar import PlayerBar
from wall_in_one.ui.next.state import AppState, ToastButton

#: libadwaita 1.8's sidebar widgets, newer than the pinned pygobject-stubs.
_Adw: Any = Adw

#: The sidebar's pages: (navigation key, title, icon).
PAGES: Final = (
    ("library", "Library", "folder-pictures-symbolic"),
    ("store", "Store", "folder-download-symbolic"),
    ("playlist", "Playlists", "view-list-symbolic"),
    ("schedule", "Schedule", "x-office-calendar-symbolic"),
    ("displays", "Displays", "video-display-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
)
WINDOW_STYLES: Final = (
    ("solid", "Solid"),
    ("translucent", "Translucent"),
    ("frosted", "Frosted"),
)
#: Builds one page over the window's state.
PageFactory = Callable[[AppState], Page]


class ShellWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        state: AppState,
        pages: Mapping[str, PageFactory],
        *,
        version: str = "",
    ) -> None:
        super().__init__(application=app, title="Wall-in-One")
        style.install()
        self.state = state
        self._version = version
        self.set_default_size(1320, 860)
        self.set_size_request(400, 480)

        self.pages: dict[str, Page] = {}
        self._current: Page | None = None
        self._nav_keys: list[str] = []
        #: The navigation identity of the page on screen (``library``,
        #: ``playlist:<id>``, ...), whether or not the sidebar shows it.
        self._nav_key = ""
        #: The window is away (unrealized): the sidebar keeps no selection.
        self._sidebar_released = False
        self._header_widgets: list[Gtk.Widget] = []
        self._banner_action = ""

        # -- content side --------------------------------------------------
        self.header = Adw.HeaderBar()
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner)
        # Read-only states (newer files, unknown settings) have their own,
        # always visible, banner: they outlast every transient condition.
        self.notice = Adw.Banner()
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120
        )
        for name, title, icon in PAGES:
            factory = pages.get(name)
            page = factory(state) if factory is not None else Placeholder(state, name, title, icon)
            self.pages[name] = page
            self.stack.add_named(page.widget, name)
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(self.header)
        content_view.add_top_bar(self.notice)
        content_view.add_top_bar(self.banner)
        content_view.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Library", tag="content")
        self.content_page.add_css_class("wio-content-page")  # the glass paints per region
        self.content_page.set_child(content_view)

        # -- sidebar -----------------------------------------------------------
        self.sidebar: Any = _Adw.Sidebar()
        self.sidebar.connect("activated", self._on_sidebar)
        if state.editing is not None:
            self.sidebar.setup_drop_target(Gdk.DragAction.COPY, [GObject.TYPE_STRING])
            self.sidebar.connect("drop", self._on_drop)
        side_header = Adw.HeaderBar()
        self.side_title = Adw.WindowTitle(title="Wall-in-One")
        side_header.set_title_widget(self.side_title)
        side_header.pack_end(self._primary_menu())
        side_view = Adw.ToolbarView()
        side_view.add_top_bar(side_header)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.sidebar)
        side_view.set_content(scroller)
        sidebar_page = Adw.NavigationPage(title="Wall-in-One", tag="sidebar")
        sidebar_page.add_css_class("wio-sidebar-page")
        sidebar_page.set_child(side_view)

        self.split = Adw.NavigationSplitView(
            min_sidebar_width=210, max_sidebar_width=270, sidebar_width_fraction=0.2
        )
        self.split.set_sidebar(sidebar_page)
        self.split.set_content(self.content_page)

        self.playerbar = PlayerBar(state)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split.set_vexpand(True)
        body.append(self.split)
        body.append(self.playerbar)

        # Frosted style: the blurred wallpaper sits underneath everything. The
        # content is an overlay that still decides the window's size.
        self.backdrop = Backdrop(state)
        top = self.wrap_body(body)
        frost = Gtk.Overlay()
        frost.set_child(self.backdrop)
        frost.add_overlay(top)
        frost.set_measure_overlay(top, True)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(frost)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        narrow.add_setter(self.split, "collapsed", True)
        for widget in self.playerbar.compact_hidden:
            narrow.add_setter(widget, "visible", False)
        self.add_breakpoint(narrow)

        state.connect("toast", self._on_toast)
        state.connect("navigate", lambda _s, key: self.navigate(key))
        state.connect("changed", self._on_changed)
        self._install_actions(app)
        self._build_sidebar()
        self._refresh_banners()
        self._refresh_glass()
        self.navigate("library")
        self.connect("unrealize", lambda _window: self._release_sidebar())
        self.connect("realize", lambda _window: self._restore_sidebar())

    # -- subclass hooks --------------------------------------------------------
    def wrap_body(self, body: Gtk.Widget) -> Gtk.Widget:
        """The widget that holds the sidebar, pages and player bar."""
        return body

    def extra_menu_sections(self) -> Sequence[tuple[str | None, Gio.Menu]]:
        """Main-menu sections shown before the window style."""
        return ()

    def open_glass_settings(self) -> None:
        """Opacity and frost: the dials of the current glass style."""
        GlassDialog(self.state).present(self)

    # -- sidebar -------------------------------------------------------------
    def _release_sidebar(self) -> None:
        """Let the sidebar's scroll-into-view idle end with the window.

        When its list maps, Adw.Sidebar scrolls the selected row into view
        from a GLib idle that re-arms until the scroll succeeds, and only the
        sidebar's dispose removes it. A window torn down before its first
        layout never lets that scroll succeed, and GTK 4 disposes a destroyed
        window only once its last reference goes, which Python's signal
        closures can postpone forever. The idle would then run on every
        main-loop pass at full CPU, and `Gio.Application.run`'s final flush
        (``while (g_main_context_iteration (context, FALSE))``) would never
        return. With no row selected, the idle removes itself on its next run,
        so nothing selects one until the window is back (`_restore_sidebar`).
        """
        self._sidebar_released = True
        self.sidebar.set_selected(Gtk.INVALID_LIST_POSITION)

    def _restore_sidebar(self) -> None:
        """Select the row of the page on screen, now that the window is back.

        By its navigation key, not a row index saved when the window went:
        navigation and sidebar rebuilds may have happened meanwhile. A page
        with no row leaves the sidebar with none selected.
        """
        if not self._sidebar_released:
            return
        self._sidebar_released = False
        key = self._nav_key
        self.sidebar.set_selected(
            self._nav_keys.index(key) if key in self._nav_keys else Gtk.INVALID_LIST_POSITION
        )
        self._title_content()

    def _title_content(self) -> None:
        """Name the content after the selected row (follows renames), else its page."""
        item = self.sidebar.get_selected_item()
        page = self._current
        title = item.get_title() if item is not None else page.title if page is not None else ""
        self.content_page.set_title(title)

    def _build_sidebar(self, selected: str | None = None) -> None:
        state = self.state
        self.sidebar.remove_all()
        self._nav_keys = []

        def add(
            section: Any,
            key: str,
            title: str,
            icon: str | None = None,
            paintable: Gdk.Paintable | None = None,
            count: str | None = None,
            tooltip: str | None = None,
        ) -> None:
            item = _Adw.SidebarItem(title=title)
            if paintable is not None:
                item.set_icon_paintable(paintable)
            elif icon:
                item.set_icon_name(icon)
            if count is not None:
                label = Gtk.Label(label=count)
                label.add_css_class("sidebar-count")
                item.set_suffix(label)
            if tooltip:
                item.set_tooltip(tooltip)
            section.append(item)
            self._nav_keys.append(key)

        main = _Adw.SidebarSection()
        add(
            main,
            "library",
            "Library",
            "folder-pictures-symbolic",
            count=str(len(state.wallpapers)),
            tooltip="Wallpapers on this computer",
        )
        add(
            main,
            "store",
            "Store",
            "folder-download-symbolic",
            tooltip="Find and download wallpapers",
        )
        self.sidebar.append(main)

        lists = _Adw.SidebarSection(title="Playlists")
        for playlist in state.playlists:
            key = f"playlist:{playlist.id}"
            count = str(len(playlist.entries))
            if playlist.automatic:
                icon = playlist.icon or "view-grid-symbolic"
                add(lists, key, playlist.name, icon, count=count, tooltip=playlist.automatic)
            else:
                cover = state.playlist_cover(playlist.id, 64)
                add(lists, key, playlist.name, "view-list-symbolic", cover, count=count)
        if state.editing is not None:
            add(lists, "new-playlist", "New playlist", "list-add-symbolic")
        self.sidebar.append(lists)

        plan = _Adw.SidebarSection(title="Automation")
        add(plan, "schedule", "Schedule", "x-office-calendar-symbolic")
        add(plan, "displays", "Displays", "video-display-symbolic", count=str(len(state.displays)))
        self.sidebar.append(plan)

        end = _Adw.SidebarSection()
        add(end, "settings", "Settings", "emblem-system-symbolic")
        self.sidebar.append(end)
        if self._sidebar_released:
            # Adw.Sidebar selects its first row of new items by itself; while
            # the window is away it must keep none (`_release_sidebar`).
            self.sidebar.set_selected(Gtk.INVALID_LIST_POSITION)
        elif selected in self._nav_keys:
            self.sidebar.set_selected(self._nav_keys.index(selected))

    @property
    def nav_keys(self) -> list[str]:
        """The sidebar's navigation keys, in order."""
        return list(self._nav_keys)

    def _on_sidebar(self, _sidebar: Any, index: int) -> None:
        key = self._nav_keys[index]
        if key == "new-playlist":
            self._new_playlist()
            return
        self.navigate(key)

    def _on_drop(self, _sidebar: Any, index: int, value: object, _action: object) -> bool:
        key = self._nav_keys[index] if index < len(self._nav_keys) else ""
        editing = self.state.editing
        if editing is None or not key.startswith("playlist:"):
            return False
        pid = key.split(":", 1)[1]
        if not self.state.has_playlist(pid) or self.state.playlist(pid).automatic:
            return False
        wid = value if isinstance(value, str) else str(getattr(value, "get_string", str)())
        editing.add_to_playlist(pid, [wid])
        return True

    # -- navigation ------------------------------------------------------------
    def navigate(self, key: str) -> None:
        name, _, argument = key.partition(":")
        page = self.pages.get(name)
        if page is None:
            return
        if page is not self._current:
            for widget in self._header_widgets:
                self.header.remove(widget)
            self._header_widgets = []
            for widget in page.header_start():
                self.header.pack_start(widget)
                self._header_widgets.append(widget)
            for widget in reversed(page.header_end()):
                self.header.pack_end(widget)
                self._header_widgets.append(widget)
            self.header.set_title_widget(page.title_widget())
            self.stack.set_visible_child_name(name)
            self._current = page
        page.activate(argument or None)
        nav_key = key if name == "playlist" else name
        self._nav_key = nav_key
        # While the window is away the sidebar keeps no selection; it gets
        # this page's row when the window is back (`_restore_sidebar`).
        if nav_key in self._nav_keys and not self._sidebar_released:
            index = self._nav_keys.index(nav_key)
            if self.sidebar.get_selected() != index:
                self.sidebar.set_selected(index)
        self._title_content()
        self.set_title(f"Wall-in-One - {page.title}")
        self.split.set_show_content(True)

    @property
    def current_page(self) -> Page | None:
        return self._current

    # -- banners, toasts ------------------------------------------------------
    def _refresh_banners(self) -> None:
        banner = self.state.banner()
        if banner is None:
            self.banner.set_revealed(False)
            self._banner_action = ""
        else:
            self.banner.set_title(banner.title)
            self.banner.set_button_label(banner.button or None)
            self._banner_action = banner.action
            self.banner.set_revealed(True)
        notice = self.state.read_only_notice()
        self.notice.set_title(notice)
        self.notice.set_revealed(bool(notice))

    def _on_banner(self, _banner: Adw.Banner) -> None:
        if self._banner_action:
            self.state.banner_activated(self._banner_action)

    def _on_toast(self, _state: AppState, text: str, button: ToastButton | None) -> None:
        toast = Adw.Toast(title=text, timeout=4)
        if button:
            label, callback = button
            toast.set_button_label(label)
            toast.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(toast)

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic == "system":
            self._refresh_banners()
        elif topic in ("playlists", "library", "displays"):
            selected = None
            index = self.sidebar.get_selected()
            if 0 <= index < len(self._nav_keys):
                selected = self._nav_keys[index]
            self._build_sidebar(selected)
            item = self.sidebar.get_selected_item()
            if item is not None:
                self.content_page.set_title(item.get_title())  # follows renames
            if topic == "library":
                self._refresh_banners()
        elif topic == "appearance":
            self._refresh_glass()

    def _refresh_glass(self) -> None:
        if self.state.window_style == "solid":
            self.remove_css_class("wio-glass")
        else:
            self.add_css_class("wio-glass")
        style_action = self.lookup_action("style")
        if isinstance(style_action, Gio.SimpleAction):
            style_action.set_state(GLib.Variant("s", self.state.window_style))
            style_action.set_enabled(not self.state.appearance_blocked())

    # -- dialogs ---------------------------------------------------------------
    def _new_playlist(self) -> None:
        editing = self.state.editing
        if editing is None:
            return
        dialog = Adw.AlertDialog(
            heading="New playlist", body="Give it a name. You can add wallpapers next."
        )
        entry = Gtk.Entry(placeholder_text="Playlist name", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response != "create":
                self._build_sidebar(self._current.name if self._current else None)
                return
            name = entry.get_text().strip() or "Untitled playlist"
            pid = editing.create_playlist(name)  # the sidebar rebuilds on "playlists"
            self.navigate(f"playlist:{pid}")
            self.state.toast(f"Created {quoted(name)}")

        dialog.connect("response", done)
        dialog.present(self)

    def _primary_menu(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        for label, section in self.extra_menu_sections():
            menu.append_section(label, section)
        styles = Gio.Menu()
        for key, label in WINDOW_STYLES:
            styles.append(label, f"win.style::{key}")
        styles.append("Opacity and frost…", "win.glass-settings")
        menu.append_section("Window style", styles)
        about = Gio.Menu()
        about.append("Keyboard shortcuts", "win.shortcuts")
        about.append("About Wall-in-One", "win.about")
        menu.append_section(None, about)
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)
        button.set_tooltip_text("Main menu")
        return button

    def _install_actions(self, app: Adw.Application) -> None:
        style_action = Gio.SimpleAction.new_stateful(
            "style", GLib.VariantType.new("s"), GLib.Variant("s", self.state.window_style)
        )

        def set_style(action: Gio.SimpleAction, value: GLib.Variant | None) -> None:
            if value is None or self.state.appearance_blocked():
                return
            action.set_state(value)
            self.state.set_window_style(value.get_string())

        style_action.connect("activate", set_style)
        self.add_action(style_action)

        def simple(name: str, callback: Callable[[], None], accels: Sequence[str] = ()) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: callback())
            self.add_action(action)
            if accels:
                app.set_accels_for_action(f"win.{name}", list(accels))

        simple("glass-settings", self.open_glass_settings)
        simple("about", self._about)
        simple("shortcuts", self._shortcuts, ["<Control>question"])
        simple("search", self._search, ["<Control>f"])
        simple("settings", lambda: self.navigate("settings"), ["<Control>comma"])
        controls = self.state.controls
        if controls is not None:
            simple("play", controls.toggle_play, ["<Control>space"])
            simple("next", lambda: controls.step(1), ["<Control>Right"])
            simple("previous", lambda: controls.step(-1), ["<Control>Left"])
        if self.state.editing is not None:
            simple("new-playlist", self._new_playlist, ["<Control>n"])
        for index, (name, _title, _icon) in enumerate(PAGES, start=1):
            simple(f"go-{name}", functools.partial(self._go, name), [f"<Alt>{index}"])

    def _go(self, name: str) -> None:
        if name == "playlist":
            first = next((p.id for p in self.state.playlists if not p.automatic), None)
            first = first or next((p.id for p in self.state.playlists), None)
            self.navigate(f"playlist:{first}" if first else "playlist")
            return
        self.navigate(name)

    def _search(self) -> None:
        if self._current is not None and self._current.focus_search():
            return
        self.navigate("library")
        self.pages["library"].focus_search()

    def _about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Wall-in-One",
            application_icon="dev.goober.WallInOne",
            version=self._version,
            developer_name="goober",
            comments="A wallpaper manager for Wayland with Noctalia color sync.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    def _shortcuts(self) -> None:
        if not hasattr(Adw, "ShortcutsDialog"):
            return
        dialog = _Adw.ShortcutsDialog()
        general = [
            ("Search", "<Control>f"),
            ("Settings", "<Control>comma"),
            ("Keyboard shortcuts", "<Control>question"),
        ]
        if self.state.editing is not None:
            general.insert(2, ("New playlist", "<Control>n"))
        groups = [("General", general)]
        if self.state.controls is not None:
            playback = [
                ("Play or pause animation", "<Control>space"),
                ("Next wallpaper", "<Control>Right"),
                ("Previous wallpaper", "<Control>Left"),
            ]
            groups.append(("Playback", playback))
        groups.append(
            ("Go to", [(title, f"<Alt>{index}") for index, (_n, title, _i) in enumerate(PAGES, 1)])
        )
        groups.append(
            (
                "Library",
                [("Open details", "Return"), ("Apply selected wallpaper", "<Control>Return")],
            )
        )
        for title, items in groups:
            section = _Adw.ShortcutsSection(title=title)
            for label, accel in items:
                section.add(_Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self)


class GlassDialog(Adw.Dialog):
    """The window style and its dials: background, panels and (frosted) frost."""

    def __init__(self, state: AppState) -> None:
        super().__init__(title="Opacity and frost", content_width=420)
        self.state = state
        self._building = False
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description="Saved for this window only; each style keeps its own values."
        )
        self._style = Adw.ToggleGroup(halign=Gtk.Align.CENTER, margin_bottom=6)
        for key, label in WINDOW_STYLES:
            self._style.add(Adw.Toggle(name=key, label=label))
        self._style.connect("notify::active-name", self._on_style)
        group.add(self._style)
        self._background = self._dial(group, "Background opacity", "The page behind the grid")
        self._panel = self._dial(group, "Panel opacity", "Sidebar, header, player bar and cards")
        self._frost = self._dial(group, "Frost", "How strongly the wallpaper is blurred")
        self._background.connect(
            "value-changed", lambda s: self._set(state.set_background_opacity, s)
        )
        self._panel.connect("value-changed", lambda s: self._set(state.set_panel_opacity, s))
        self._frost.connect("value-changed", lambda s: self._set(state.set_frost, s))
        page.add(group)
        header = Adw.HeaderBar()
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.set_child(view)
        self._handler = state.connect("changed", self._on_changed)
        self.connect("closed", lambda *_: self._disconnect())
        self._sync()

    def _dial(self, group: Adw.PreferencesGroup, title: str, subtitle: str) -> Gtk.Scale:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_hexpand(True)
        scale.set_size_request(150, -1)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_format_value_func(lambda _s, value: f"{value:.0f}%")
        row.add_suffix(scale)
        group.add(row)
        return scale

    def _set(self, setter: Callable[[float], None], scale: Gtk.Scale) -> None:
        if not self._building:
            setter(scale.get_value() / 100)

    def _on_style(self, group: Adw.ToggleGroup, _param: object) -> None:
        name = group.get_active_name()
        if not self._building and name and name != self.state.window_style:
            self.state.set_window_style(name)

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic == "appearance":
            self._sync()

    def _disconnect(self) -> None:
        if self._handler:
            self.state.disconnect(self._handler)
            self._handler = 0

    def _sync(self) -> None:
        state = self.state
        self._building = True
        try:
            blocked = state.appearance_blocked()
            glass = state.window_style != "solid"
            self._style.set_active_name(state.window_style)
            self._style.set_sensitive(not blocked)
            for scale, value in (
                (self._background, state.background_alpha),
                (self._panel, state.panel_alpha),
                (self._frost, state.frost),
            ):
                scale.set_value(round(value * 100))
                scale.set_sensitive(glass and not blocked)
            self._frost.set_sensitive(state.window_style == "frosted" and not blocked)
            if blocked:
                self.set_title("Opacity and frost (read-only)")
        finally:
            self._building = False
