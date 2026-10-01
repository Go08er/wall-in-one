"""The demo's window: the app's own shell (``wall_in_one.ui.next.shell``) plus demo extras.

The sidebar, header, banners, player bar, frosted backdrop, Library and
inspector are the widgets the app ships. This subclass adds only what is
demo-only: the prototype's other pages, the ☰ menu's Demo switches, the
welcome screen, and the colors that follow the simulated (or real, read-only)
Noctalia (``glass.Look``).
"""

from __future__ import annotations

import importlib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from wall_in_one.ui.next import library
from wall_in_one.ui.next.shell import PAGES, PageFactory, ShellWindow

from . import art, glass
from .pages import Page, Placeholder

MODULE_FOR = {"playlist": "playlists"}


def _factory(name: str, title: str, icon: str) -> PageFactory:
    if name == "library":
        return library.create
    try:
        module = importlib.import_module(f".pages.{MODULE_FOR.get(name, name)}", __package__)
    except ModuleNotFoundError:
        return lambda state: Placeholder(state, name, title, icon)
    return module.create


class MainWindow(ShellWindow):
    def __init__(self, app: Adw.Application, state) -> None:
        self._clock_source = 0
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        super().__init__(
            app,
            state,
            {name: _factory(name, title, icon) for name, title, icon in PAGES},
            version="UI prototype",
        )
        self.pages: dict[str, Page]
        self.look = glass.Look(self, state)  # the demo desktop's colors, kept live
        state.connect("changed", self._on_demo_changed)
        self._install_demo_actions(app)

    # -- shell hooks -------------------------------------------------------------
    def wrap_body(self, body: Gtk.Widget) -> Gtk.Widget:
        # First-run screen, swapped in by the demo menu.
        self.root_stack.add_named(body, "main")
        self.root_stack.add_named(self._welcome(), "welcome")
        return self.root_stack

    def extra_menu_sections(self):
        demo = Gio.Menu()
        demo.append("Real Noctalia colors", "win.live")
        demo.append("Dark style", "win.dark")
        demo.append("Simulate battery power", "win.battery")
        demo.append("Simulate stopped service", "win.service")
        demo.append("Show welcome screen", "win.welcome")
        demo.append("Simulate time passing", "win.time")
        return [("Demo", demo)]

    def open_glass_settings(self) -> None:
        self.navigate("settings")
        self.pages["settings"].scroll_to("appearance")

    # -- demo ----------------------------------------------------------------------
    def _on_demo_changed(self, _state, topic: str) -> None:
        if topic == "theme":
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if self.state.dark else Adw.ColorScheme.FORCE_LIGHT
            )

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

    def _install_demo_actions(self, _app: Adw.Application) -> None:
        def toggle(name: str, initial: bool, callback) -> Gio.SimpleAction:
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", initial))

            def flip(act, _param) -> None:
                value = not act.get_state().get_boolean()
                act.set_state(GLib.Variant("b", value))
                callback(value)

            action.connect("activate", flip)
            self.add_action(action)
            return action

        dark_action = toggle("dark", self.state.dark, self.state.set_dark)
        live = toggle("live", self.state.live_colors(), self.state.set_use_live_colors)
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
                    self.side_title.set_subtitle("Demo clock · " + self.state.now.strftime("%a %H:%M"))
                    return GLib.SOURCE_CONTINUE

                self._clock_source = GLib.timeout_add(1200, tick)
                self.side_title.set_subtitle("Demo clock · " + self.state.now.strftime("%a %H:%M"))
            else:
                self.side_title.set_subtitle("")

        toggle("time", False, simulate_time)

        welcome = Gio.SimpleAction.new("welcome", None)
        welcome.connect("activate", lambda *_: self.root_stack.set_visible_child_name("welcome"))
        self.add_action(welcome)
