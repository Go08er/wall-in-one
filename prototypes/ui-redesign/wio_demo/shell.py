"""Main window: sidebar navigation, one shared header, banner, pages, player bar."""

from __future__ import annotations

import importlib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import art, glass
from .pages import Page, Placeholder
from .playerbar import PlayerBar

PAGE_MODULES = [
    ("library", "Library", "folder-pictures-symbolic"),
    ("store", "Store", "folder-download-symbolic"),
    ("playlist", "Playlists", "view-list-symbolic"),
    ("schedule", "Schedule", "x-office-calendar-symbolic"),
    ("displays", "Displays", "video-display-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
]


MODULE_FOR = {"playlist": "playlists"}


def _load_page(state, name: str, title: str, icon: str) -> Page:
    try:
        module = importlib.import_module(f".pages.{MODULE_FOR.get(name, name)}", __package__)
    except ModuleNotFoundError:
        return Placeholder(state, name, title, icon)
    return module.create(state)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, state) -> None:
        super().__init__(application=app, title="Wall-in-One")
        self.state = state
        self.set_default_size(1320, 860)
        self.set_size_request(400, 480)

        self.pages: dict[str, Page] = {}
        self._current: Page | None = None
        self._nav_keys: list[str] = []
        self._header_widgets: list[Gtk.Widget] = []

        # -- content side --------------------------------------------------
        self.header = Adw.HeaderBar()
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        for name, title, icon in PAGE_MODULES:
            page = _load_page(state, name, title, icon)
            self.pages[name] = page
            self.stack.add_named(page.widget, name)
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(self.header)
        content_view.add_top_bar(self.banner)
        content_view.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Library", tag="content")
        self.content_page.add_css_class("wio-content-page")  # glass.py paints per region
        self.content_page.set_child(content_view)

        # -- sidebar -----------------------------------------------------------
        self.sidebar = Adw.Sidebar()
        self.sidebar.connect("activated", self._on_sidebar)
        self.sidebar.setup_drop_target(Gdk.DragAction.COPY, [GObject.TYPE_STRING])
        self.sidebar.connect("drop", self._on_drop)
        side_header = Adw.HeaderBar()
        self._side_title = Adw.WindowTitle(title="Wall-in-One")
        side_header.set_title_widget(self._side_title)
        self._clock_source = 0
        side_header.pack_end(self._primary_menu())
        side_view = Adw.ToolbarView()
        side_view.add_top_bar(side_header)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.sidebar)
        side_view.set_content(scroller)
        sidebar_page = Adw.NavigationPage(title="Wall-in-One", tag="sidebar")
        sidebar_page.add_css_class("wio-sidebar-page")
        sidebar_page.set_child(side_view)

        self.split = Adw.NavigationSplitView(min_sidebar_width=210, max_sidebar_width=270, sidebar_width_fraction=0.2)
        self.split.set_sidebar(sidebar_page)
        self.split.set_content(self.content_page)

        self.playerbar = PlayerBar(state)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split.set_vexpand(True)
        body.append(self.split)
        body.append(self.playerbar)

        # First-run screen, swapped in by the demo menu.
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.add_named(body, "main")
        self.root_stack.add_named(self._welcome(), "welcome")
        # Frosted style: the blurred wallpaper sits underneath everything. The
        # content is an overlay that still decides the window's size.
        self.backdrop = glass.Backdrop(state)
        frost = Gtk.Overlay()
        frost.set_child(self.backdrop)
        frost.add_overlay(self.root_stack)
        frost.set_measure_overlay(self.root_stack, True)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(frost)
        self.set_content(self.toasts)
        self.look = glass.Look(self, state)  # colors and glass, kept live

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
        self._refresh_banner()
        self.navigate("library")

    # -- sidebar -------------------------------------------------------------
    def _build_sidebar(self, selected: str | None = None) -> None:
        state = self.state
        self.sidebar.remove_all()
        self._nav_keys = []

        def add(
            section: Adw.SidebarSection,
            key: str,
            title: str,
            icon: str | None = None,
            paintable=None,
            count: str | None = None,
            tooltip: str | None = None,
        ) -> None:
            item = Adw.SidebarItem(title=title)
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

        main = Adw.SidebarSection()
        add(
            main,
            "library",
            "Library",
            "folder-pictures-symbolic",
            count=str(len(state.wallpapers)),
            tooltip="Wallpapers on this computer",
        )
        add(main, "store", "Store", "folder-download-symbolic", tooltip="Find and download wallpapers")
        self.sidebar.append(main)

        lists = Adw.SidebarSection(title="Playlists")
        for playlist in state.playlists:
            if playlist.automatic:
                add(
                    lists,
                    f"playlist:{playlist.id}",
                    playlist.name,
                    playlist.icon or "view-grid-symbolic",
                    count=str(len(playlist.entries)),
                    tooltip=playlist.automatic,
                )
            else:
                add(
                    lists,
                    f"playlist:{playlist.id}",
                    playlist.name,
                    paintable=state.playlist_cover(playlist.id, 64),
                    count=str(len(playlist.entries)),
                )
        add(lists, "new-playlist", "New playlist", "list-add-symbolic")
        self.sidebar.append(lists)

        plan = Adw.SidebarSection(title="Automation")
        add(plan, "schedule", "Schedule", "x-office-calendar-symbolic")
        add(plan, "displays", "Displays", "video-display-symbolic", count=str(len(state.displays)))
        self.sidebar.append(plan)

        end = Adw.SidebarSection()
        add(end, "settings", "Settings", "emblem-system-symbolic")
        self.sidebar.append(end)
        if selected in self._nav_keys:
            self.sidebar.set_selected(self._nav_keys.index(selected))

    def _on_sidebar(self, _sidebar, index: int) -> None:
        key = self._nav_keys[index]
        if key == "new-playlist":
            self._new_playlist()
            return
        self.navigate(key)

    def _on_drop(self, _sidebar, index: int, value, _action) -> bool:
        key = self._nav_keys[index] if index < len(self._nav_keys) else ""
        if not key.startswith("playlist:") or key == "playlist:all-media":
            return False
        wid = value if isinstance(value, str) else value.get_string()
        self.state.add_to_playlist(key.split(":", 1)[1], [wid])
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
        if nav_key in self._nav_keys:
            index = self._nav_keys.index(nav_key)
            if self.sidebar.get_selected() != index:
                self.sidebar.set_selected(index)
        self.content_page.set_title(
            self.sidebar.get_selected_item().get_title() if self.sidebar.get_selected_item() else page.title
        )
        self.split.set_show_content(True)

    # -- banner, toasts ------------------------------------------------------
    def _refresh_banner(self) -> None:
        state = self.state
        if not state.service_running:
            self.banner.set_title("The wallpaper service isn't running, so nothing changes on schedule")
            self.banner.set_button_label("Start")
            self.banner.set_revealed(True)
            self._banner_action = "start"
        elif state.on_battery and state.stop_on_battery:
            self.banner.set_title("On battery: animations are paused and stills stay on screen")
            self.banner.set_button_label("Battery settings")
            self.banner.set_revealed(True)
            self._banner_action = "settings"
        else:
            self.banner.set_revealed(False)

    def _on_banner(self, _banner) -> None:
        if self._banner_action == "start":
            self.state.set_service_running(True)
            self.state.toast("Wallpaper service started")
        else:
            self.navigate("settings:playback")

    def _on_toast(self, _state, text: str, button) -> None:
        toast = Adw.Toast(title=text, timeout=4)
        if callable(button):  # older callers passed a bare undo callback
            button = ("Undo", button)
        if button:
            label, callback = button
            toast.set_button_label(label)
            toast.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(toast)

    def _on_changed(self, _state, topic: str) -> None:
        if topic == "system":
            self._refresh_banner()
        elif topic in ("playlists", "library"):
            selected = None
            item_index = self.sidebar.get_selected()
            if 0 <= item_index < len(self._nav_keys):
                selected = self._nav_keys[item_index]
            self._build_sidebar(selected)
            item = self.sidebar.get_selected_item()
            if item is not None:
                self.content_page.set_title(item.get_title())  # follows renames
        elif topic == "theme":
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if self.state.dark else Adw.ColorScheme.FORCE_LIGHT
            )

    # -- dialogs ---------------------------------------------------------------
    def _new_playlist(self) -> None:
        dialog = Adw.AlertDialog(heading="New playlist", body="Give it a name. You can add wallpapers next.")
        entry = Gtk.Entry(placeholder_text="Playlist name", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            if response != "create":
                self._build_sidebar(self._current.name if self._current else None)
                return
            name = entry.get_text().strip() or "Untitled playlist"
            pid = self.state.create_playlist(name)  # the sidebar rebuilds on "playlists"
            self.navigate(f"playlist:{pid}")
            self.state.toast(f"Created “{name}”")

        dialog.connect("response", done)
        dialog.present(self)

    def _welcome(self) -> Gtk.Widget:
        status = Adw.StatusPage(
            paintable=art.mosaic(
                tuple(self.state.wallpaper(w).key for w in ("alpine", "lily-pond", "northern-lights", "golden-coast")),
                160,
            ),
            title="Welcome to Wall-in-One",
            description="Pick the folder where your wallpapers live. Images, videos and "
            "Wallpaper Engine scenes are all welcome — nothing is moved or renamed.",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        suggested = Gtk.Button(label="Use ~/Pictures/Wallpapers")
        suggested.add_css_class("pill")
        suggested.add_css_class("suggested-action")
        other = Gtk.Button(label="Choose another folder…")
        other.add_css_class("pill")
        steam = Adw.SwitchRow(
            title="Include Wallpaper Engine scenes", subtitle="Found in your Steam library", active=True
        )
        steam_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        steam_box.add_css_class("boxed-list")
        steam_box.append(steam)
        steam_box.set_size_request(360, -1)
        for widget in (suggested, other):
            widget.connect("clicked", lambda *_: self.root_stack.set_visible_child_name("main"))
        # Choose what to include first; either button then leaves this screen.
        box.append(steam_box)
        box.append(suggested)
        box.append(other)
        status.set_child(box)
        # A flat header bar: window controls, and something to drag the window by.
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("flat")
        view = Adw.ToolbarView(content=status)
        view.add_top_bar(header)
        view.add_css_class("background")  # one background layer in every window style
        return view

    def _primary_menu(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        demo = Gio.Menu()
        demo.append("Real Noctalia colors", "win.live")
        demo.append("Dark style", "win.dark")
        demo.append("Simulate battery power", "win.battery")
        demo.append("Simulate stopped service", "win.service")
        demo.append("Show welcome screen", "win.welcome")
        demo.append("Simulate time passing", "win.time")
        menu.append_section("Demo", demo)
        styles = Gio.Menu()
        styles.append("Solid", "win.style::solid")
        styles.append("Translucent", "win.style::translucent")
        styles.append("Frosted", "win.style::frosted")
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
        def toggle(name: str, initial: bool, callback) -> Gio.SimpleAction:
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", initial))

            def flip(act, _param) -> None:
                value = not act.get_state().get_boolean()
                act.set_state(GLib.Variant("b", value))
                callback(value)

            action.connect("activate", flip)
            self.add_action(action)
            return action

        def dark(value: bool) -> None:
            self.state.dark = value
            self.state.emit_changed("theme", "now")

        dark_action = toggle("dark", self.state.dark, dark)

        def live_colors(value: bool) -> None:
            self.state.use_live_colors = value
            self.state.sync_live()  # adopt the real mode when switching on
            self.state.emit_changed("settings", "now")

        live = toggle("live", self.state.live_colors(), live_colors)
        live.set_enabled(self.state.live is not None and self.state.live.found)

        def sync_toggles(_state, topic: str) -> None:
            # Following Noctalia can flip light/dark; keep the menu's checks honest.
            if topic in ("theme", "settings"):
                dark_action.set_state(GLib.Variant("b", self.state.dark))
                live.set_state(GLib.Variant("b", self.state.live_colors()))
                live.set_enabled(self.state.live is not None and self.state.live.found)

        self.state.connect("changed", sync_toggles)
        toggle("battery", self.state.on_battery, self.state.set_battery)
        toggle("service", not self.state.service_running, lambda v: self.state.set_service_running(not v))

        def simulate_time(value: bool) -> None:
            if self._clock_source:
                GLib.source_remove(self._clock_source)
                self._clock_source = 0
            if value:

                def tick() -> bool:
                    self.state.advance(20)
                    self._side_title.set_subtitle("Demo clock · " + self.state.now.strftime("%a %H:%M"))
                    return GLib.SOURCE_CONTINUE

                self._clock_source = GLib.timeout_add(1200, tick)
                self._side_title.set_subtitle("Demo clock · " + self.state.now.strftime("%a %H:%M"))
            else:
                self._side_title.set_subtitle("")

        toggle("time", False, simulate_time)

        style = Gio.SimpleAction.new_stateful(
            "style", GLib.VariantType.new("s"), GLib.Variant("s", self.state.window_style)
        )

        def set_style(action: Gio.SimpleAction, value: GLib.Variant) -> None:
            action.set_state(value)
            self.state.window_style = value.get_string()
            self.state.emit_changed("appearance")

        style.connect("activate", set_style)
        # Keep the radio in step when Settings changes the style.
        self.state.connect(
            "changed",
            lambda _s, topic: topic == "appearance" and style.set_state(GLib.Variant("s", self.state.window_style)),
        )
        self.add_action(style)

        def glass_settings(*_args) -> None:
            self.navigate("settings")
            self.pages["settings"].scroll_to("appearance")

        glass_action = Gio.SimpleAction.new("glass-settings", None)
        glass_action.connect("activate", glass_settings)
        self.add_action(glass_action)

        def simple(name: str, callback, accels: list[str] | None = None) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: callback())
            self.add_action(action)
            if accels:
                app.set_accels_for_action(f"win.{name}", accels)

        simple("welcome", lambda: self.root_stack.set_visible_child_name("welcome"))
        simple("about", self._about)
        simple("shortcuts", self._shortcuts, ["<Control>question"])
        simple("search", self._search, ["<Control>f"])
        simple("play", self.state.toggle_play, ["<Control>space"])
        simple("next", lambda: self.state.step(1), ["<Control>Right"])
        simple("previous", lambda: self.state.step(-1), ["<Control>Left"])
        simple("new-playlist", self._new_playlist, ["<Control>n"])
        simple("settings", lambda: self.navigate("settings"), ["<Control>comma"])
        for index, (name, _title, _icon) in enumerate(PAGE_MODULES, start=1):
            target = name if name != "playlist" else "playlist:frog-day"
            simple(f"go-{name}", lambda t=target: self.navigate(t), [f"<Alt>{index}"])

    def _search(self) -> None:
        if self._current and self._current.focus_search():
            return
        self.navigate("library")
        self.pages["library"].focus_search()

    def _about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Wall-in-One",
            application_icon="folder-pictures-symbolic",
            version="UI prototype",
            developer_name="goober",
            comments="A wallpaper manager for Wayland with Noctalia color sync.\n"
            "This build is an unwired design prototype with demo data.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    def _shortcuts(self) -> None:
        dialog = Adw.ShortcutsDialog()
        groups = [
            (
                "General",
                [
                    ("Search", "<Control>f"),
                    ("Settings", "<Control>comma"),
                    ("New playlist", "<Control>n"),
                    ("Keyboard shortcuts", "<Control>question"),
                ],
            ),
            (
                "Playback",
                [
                    ("Play or pause animation", "<Control>space"),
                    ("Next wallpaper", "<Control>Right"),
                    ("Previous wallpaper", "<Control>Left"),
                ],
            ),
            (
                "Go to",
                [
                    ("Library", "<Alt>1"),
                    ("Store", "<Alt>2"),
                    ("Playlists", "<Alt>3"),
                    ("Schedule", "<Alt>4"),
                    ("Displays", "<Alt>5"),
                    ("Settings", "<Alt>6"),
                ],
            ),
            ("Library", [("Open details", "Return"), ("Apply selected wallpaper", "<Control>Return")]),
        ]
        for title, items in groups:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self)
