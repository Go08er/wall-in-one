"""The slim bar at the bottom: what is on screen, why, and the playback controls.

It replaces the classic window's header popover. It is deliberately not a
dashboard: one line of truth plus the controls you reach for most. Everything
it says comes from `AppState.player`, which an adapter builds from what the
runtime reported; nothing here works out a schedule. Without
`AppState.controls` the transport buttons are shown but off.

The controls never change what the bar shows: a press goes to
`AppState.controls`, and the bar follows the next `Player`. A toggle that
GTK flipped on the click is put back at once, until the runtime agrees.
While `Player.busy` the controls wait (Play shows a spinner), and while
`Player.controls_off` gives a reason they are off and say it.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.state import AppState, OnScreen, Player, reason_text

#: Topics after which the bar reads the state again.
TOPICS = frozenset(
    {"now", "playback", "system", "displays", "playlists", "settings", "library", "scope"}
)
#: Said on the transport controls when the adapter offers none.
NO_CONTROLS = "Playback controls are not in the new interface yet"
#: Said on the transport controls while a command is in flight.
SENDING = "Sending a playback command\u2026"


class PlayerBar(Gtk.Box):
    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.add_css_class("playerbar")
        self._building = False
        self._shown: tuple[str, ...] = ()
        self._detail = ""

        bar = Gtk.CenterBox()
        self.append(bar)

        # -- left: what is showing -------------------------------------------
        left = Gtk.Box(spacing=10)
        self._thumbs = Gtk.Fixed()
        self._thumb_button = Gtk.Button()
        self._thumb_button.add_css_class("flat")
        self._thumb_button.set_child(self._thumbs)
        self._thumb_button.set_tooltip_text("Show in Library")
        self._thumb_button.connect("clicked", self._show_current)
        left.append(self._thumb_button)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=1)
        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
        self._title.add_css_class("now-title")
        why = Gtk.Box(spacing=6)
        self._dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self._dot.add_css_class("status-dot")
        self._why = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
        self._why.add_css_class("dimmed")
        self._why.add_css_class("caption")
        self._resume = Gtk.Button(label="Resume schedule")
        for css in ("flat", "caption", "accent"):
            self._resume.add_css_class(css)
        self._resume.set_valign(Gtk.Align.CENTER)
        self._resume.set_tooltip_text("Go back to what the schedule says should play now")
        self._resume.connect("clicked", lambda *_: self._resume_schedule())
        why.append(self._dot)
        why.append(self._why)
        # Wrapped so the narrow breakpoint can hide it; the ⋯ menu keeps the action.
        resume_box = Gtk.Box()
        resume_box.append(self._resume)
        why.append(resume_box)
        self._resume_box = resume_box
        text.append(self._title)
        text.append(why)
        left.append(text)
        bar.set_start_widget(left)

        # -- center: controls ------------------------------------------------
        controls = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self._shuffle = Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic")
        self._shuffle.add_css_class("flat")
        self._shuffle.add_css_class("player-control")
        self._shuffle.set_tooltip_text("Shuffle the order")
        self._shuffle.connect("toggled", self._on_shuffle)
        self._previous = widgets.icon_button(
            "media-skip-backward-symbolic",
            "Previous wallpaper",
            lambda *_: self._step(-1),
            "flat",
            "player-control",
        )
        self._play = Gtk.Button()
        self._play.add_css_class("play-button")
        self._play.add_css_class("suggested-action")
        self._play.connect("clicked", self._on_play)
        self._next = widgets.icon_button(
            "media-skip-forward-symbolic",
            "Next wallpaper",
            lambda *_: self._step(1),
            "flat",
            "player-control",
        )
        self._rotate = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic")
        self._rotate.add_css_class("flat")
        self._rotate.add_css_class("player-control")
        self._rotate.set_tooltip_text("Change wallpaper automatically")
        self._rotate.connect("toggled", self._on_rotate)
        # Shuffle and rotate sit in their own boxes so a narrow-window breakpoint
        # can hide them without fighting refresh(), which manages the buttons.
        self.compact_hidden: list[Gtk.Widget] = []
        shuffle_box, rotate_box = Gtk.Box(), Gtk.Box()
        shuffle_box.append(self._shuffle)
        rotate_box.append(self._rotate)
        for widget in (shuffle_box, self._previous, self._play, self._next, rotate_box):
            controls.append(widget)
        self.compact_hidden += [shuffle_box, rotate_box, self._resume_box]
        self._controls = controls
        #: Each control's own tooltip, said whenever it is not off for a reason.
        self._tips: dict[Gtk.Widget, str] = {
            button: button.get_tooltip_text() or ""
            for button in (self._shuffle, self._previous, self._next, self._rotate, self._resume)
        }
        self._spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._spinner.set_size_request(24, 24)
        bar.set_center_widget(controls)

        # -- right: timing, scope, more -------------------------------------
        right = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self._timing = Gtk.Label()
        for css in ("dimmed", "caption", "numeric"):
            self._timing.add_css_class(css)
        self._scope = Gtk.MenuButton()
        self._scope.add_css_class("flat")
        self._scope.set_tooltip_text("Which displays these controls affect")
        self._scope_menu_model = Gio.Menu()
        self._scope.set_menu_model(self._scope_menu_model)
        more_model = Gio.Menu()
        more_model.append("Resume schedule", "player.resume")
        more_model.append("Random wallpaper", "player.random")
        more_model.append("Stop animation (keep still)", "player.stop")
        section = Gio.Menu()
        section.append("Why this wallpaper?", "player.why")
        more_model.append_section(None, section)
        self._more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more_model)
        self._more.add_css_class("flat")
        self._more.set_tooltip_text("More playback actions")
        details = Gtk.Box(spacing=8)
        # Battery state isn't repeated here: the window's banner says it.
        details.append(self._timing)
        details.append(self._scope)
        right.append(details)
        right.append(self._more)
        self.compact_hidden.append(details)
        bar.set_end_widget(right)

        actions = Gio.SimpleActionGroup()
        self._actions = actions
        for name, callback in (
            ("resume", self._resume_schedule),
            ("random", self._random),
            ("stop", self._stop),
            ("why", self._explain),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _v, run=callback: run())
            actions.add_action(action)
        scope = Gio.SimpleAction.new_stateful(
            "scope", GLib.VariantType.new("s"), GLib.Variant("s", state.scope)
        )
        scope.connect("activate", self._on_scope)
        actions.add_action(scope)
        self.insert_action_group("player", actions)

        state.connect("changed", self._on_changed)
        self.refresh()

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic in TOPICS:
            self.refresh()

    # -- callbacks -----------------------------------------------------------
    def _on_play(self, *_args: object) -> None:
        controls = self.state.controls
        if controls is None:
            return
        player = self.state.player()
        if player.service == "stopped" and not player.controls_off:
            controls.start_service()
            return
        controls.toggle_play()

    def _step(self, direction: int) -> None:
        if self.state.controls is not None:
            self.state.controls.step(direction, self.state.scope)

    def _random(self) -> None:
        if self.state.controls is not None:
            self.state.controls.random(self.state.scope)

    def _stop(self) -> None:
        if self.state.controls is not None:
            self.state.controls.stop()

    def _resume_schedule(self) -> None:
        if self.state.controls is not None:
            self.state.controls.resume_schedule(self.state.scope)

    def _on_shuffle(self, button: Gtk.ToggleButton) -> None:
        if not self._building and self.state.controls is not None:
            self.state.controls.set_shuffle(button.get_active(), self.state.scope)
            self.refresh()  # back to what the runtime says until it says otherwise

    def _on_rotate(self, button: Gtk.ToggleButton) -> None:
        if not self._building and self.state.controls is not None:
            self.state.controls.set_rotate(button.get_active())
            self.refresh()  # back to what the runtime says until it says otherwise

    def _on_scope(self, action: Gio.SimpleAction, value: GLib.Variant | None) -> None:
        if value is None:
            return
        action.set_state(value)
        self.state.set_scope(value.get_string())  # the inspector's "Apply to …" follows
        self.refresh()

    def _show_current(self, *_args: object) -> None:
        shown = [wid for wid in self._shown if wid]
        if shown:
            self.state.navigate("library:" + shown[0])

    def _explain(self) -> None:
        self.state.navigate("schedule:why")

    # -- view ----------------------------------------------------------------
    def set_detail(self, text: str) -> None:
        """A longer status line for the status dot's tooltip (the runtime's own words)."""
        self._dot.set_tooltip_text(text or None)
        self._detail = text

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def title_text(self) -> str:
        """What is on screen, as shown."""
        return self._title.get_label()

    @property
    def reason_text(self) -> str:
        """Why, as shown."""
        return self._why.get_label()

    def refresh(self) -> None:
        self._building = True
        try:
            self._render(self.state.player())
        finally:
            self._building = False

    def _render(self, player: Player) -> None:
        state = self.state
        screens = player.screens
        names: dict[str, str] = {}
        for screen in screens:
            names.setdefault(screen.wallpaper or screen.name, screen.name)
        self._shown = tuple(screen.wallpaper for screen in screens)
        self._render_thumbs(screens)
        self._thumb_button.set_sensitive(any(self._shown))

        running = player.service == "running"
        if player.service == "stopped":
            self._title.set_label("Wallpaper service isn\u2019t running")
            self._why.set_label("Your last wallpaper stays on screen")
            self._why.set_tooltip_text(None)
        elif player.service == "checking":
            self._title.set_label("Checking the wallpaper service…")
            self._why.set_label("")
            self._why.set_tooltip_text(None)
        else:
            # "·" separates facts in the line below, so names are joined in words.
            shown = [name for name in names.values() if name]
            self._title.set_label(_and_join(shown) or "Nothing reported yet")
            reasons = {screen.connector: reason_text(screen.reason) for screen in screens}
            if len(set(reasons.values())) > 1:
                # Two displays playing for different reasons: say so rather
                # than describing only the first one.
                why = "Different on each display"
                self._why.set_tooltip_text(
                    "\n".join(f"{connector}: {text}" for connector, text in reasons.items())
                )
            else:
                why = next(iter(reasons.values()), "")
                self._why.set_tooltip_text(None)
            if player.playback == "paused":
                why = "Paused · " + why
            elif player.playback == "stopped":
                why = "Still only · " + why
            self._why.set_label(" · ".join(part for part in (why, *player.notes) if part))
        self._title.set_tooltip_text(
            "\n".join(f"{screen.connector}: {screen.name}" for screen in screens if screen.name)
            or None
        )

        # Why every control is off now, if it is: no controls, the adapter's
        # reason (service down, a newer file), or a command in flight.
        off = NO_CONTROLS if state.controls is None else player.controls_off
        waiting = off or (SENDING if player.busy else "")
        usable = running and not waiting
        manual = running and not player.following_schedule
        self._resume.set_visible(manual)
        for button, tip in self._tips.items():
            button.set_sensitive(usable)
            button.set_tooltip_text(waiting or tip)
        resume = self._actions.lookup_action("resume")
        if isinstance(resume, Gio.SimpleAction):
            resume.set_enabled(manual and usable)
        for name in ("random", "stop"):
            found = self._actions.lookup_action(name)
            if isinstance(found, Gio.SimpleAction):
                found.set_enabled(usable)

        for css in ("paused", "stopped"):
            self._dot.remove_css_class(css)
        if not running or player.playback == "stopped":
            self._dot.add_css_class("stopped")
        elif player.playback == "paused":
            self._dot.add_css_class("paused")

        self._render_play(player, off)
        self._shuffle.set_active(player.shuffle)
        self._rotate.set_active(player.rotate)

        self._timing.set_label(player.timing if running else "")
        self._timing.set_visible(bool(self._timing.get_label()))

        self._scope.set_visible(state.display_mode == "independent" and len(state.displays) > 1)
        self._scope.set_label(state.scope_label())
        self._scope_menu_model.remove_all()
        self._scope_menu_model.append("All displays", "player.scope::all")
        for display in state.displays:
            label = display.connector
            if display.model:
                label = f"{display.connector} \u2014 {display.model}"
            self._scope_menu_model.append(label, f"player.scope::{display.connector}")
        scope = self._actions.lookup_action("scope")
        if isinstance(scope, Gio.SimpleAction):
            scope.set_state(GLib.Variant("s", state.scope))

    def _render_play(self, player: Player, off: str) -> None:
        """Play's face: a spinner while busy, else what a press would do (or why not)."""
        play = self._play
        if player.busy:
            if play.get_child() is not self._spinner:
                play.set_child(self._spinner)
            play.set_sensitive(False)
            play.set_tooltip_text(SENDING)
            return
        playing = player.service == "running" and player.playback == "playing"
        if off:
            icon, tip = "media-playback-pause-symbolic" if playing else "", off
        elif player.service == "stopped":
            icon, tip = "", "Start the wallpaper service"
        elif player.service == "checking":
            icon, tip = "", "Checking the wallpaper service\u2026"
        elif player.play_refused:
            icon, tip = "dialog-warning-symbolic", player.play_refused
        elif player.retry:
            icon, tip = "", "Retry the animation: its renderer stopped"
        elif playing:
            icon, tip = "media-playback-pause-symbolic", "Pause animation"
        else:
            icon, tip = "", "Resume animation"
        play.set_icon_name(icon or "media-playback-start-symbolic")
        play.set_tooltip_text(tip)
        play.set_sensitive(not off and player.service != "checking")

    @property
    def play_button(self) -> Gtk.Button:
        return self._play

    @property
    def busy_shown(self) -> bool:
        """Whether Play shows the spinner of a command in flight."""
        return self._play.get_child() is self._spinner

    def _render_thumbs(self, screens: tuple[OnScreen, ...]) -> None:
        child = self._thumbs.get_first_child()
        while child is not None:
            self._thumbs.remove(child)
            child = self._thumbs.get_first_child()
        shown = list(dict.fromkeys(s.wallpaper for s in screens if s.wallpaper))
        # Two displays showing different wallpapers get two overlapping thumbs.
        for index, wid in enumerate(shown[:2]):
            if not self.state.has_wallpaper(wid):
                continue
            thumb = widgets.thumbnail(self.state.wallpaper(wid), 72, 40, 7)
            if len(shown) > 1:
                thumb.add_css_class("stacked-thumb")
            self._thumbs.put(thumb, index * 26, index * 6 if len(shown) > 1 else 3)
        if not shown:
            icon = Gtk.Image.new_from_icon_name("preferences-desktop-wallpaper-symbolic")
            icon.set_pixel_size(32)
            icon.set_size_request(72, 40)
            icon.add_css_class("dimmed")
            self._thumbs.put(icon, 0, 3)


def _and_join(names: list[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
