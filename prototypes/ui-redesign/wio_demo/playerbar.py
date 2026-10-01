"""The slim bar at the bottom: what is on screen, why, and the playback controls.

It replaces the header popover of the current app. It is deliberately not a
dashboard: one line of truth plus the controls you reach for most.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk

from . import ui


class PlayerBar(Gtk.Box):
    def __init__(self, state) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.add_css_class("playerbar")
        self._building = False

        bar = Gtk.CenterBox()
        self.append(bar)

        # -- left: what is showing -------------------------------------------
        left = Gtk.Box(spacing=10)
        self._thumbs = Gtk.Fixed()
        thumb_button = Gtk.Button()
        thumb_button.add_css_class("flat")
        thumb_button.set_child(self._thumbs)
        thumb_button.set_tooltip_text("Show in Library")
        thumb_button.connect("clicked", self._show_current)
        left.append(thumb_button)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=1)
        self._title = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=28)
        self._title.add_css_class("now-title")
        why = Gtk.Box(spacing=6)
        self._dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self._dot.add_css_class("status-dot")
        self._why = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=40)
        self._why.add_css_class("dimmed")
        self._why.add_css_class("caption")
        self._resume = Gtk.Button(label="Resume schedule")
        self._resume.add_css_class("flat")
        self._resume.add_css_class("caption")
        self._resume.add_css_class("accent")
        self._resume.set_valign(Gtk.Align.CENTER)
        self._resume.set_tooltip_text("Go back to what the schedule says should play now")
        self._resume.connect("clicked", lambda *_: self.state.resume_schedule(self.state.scope))
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
        previous = ui.icon_button(
            "media-skip-backward-symbolic",
            "Previous wallpaper",
            lambda *_: self.state.step(-1),
            "flat",
            "player-control",
        )
        self._previous = previous
        self._play = Gtk.Button()
        self._play.add_css_class("play-button")
        self._play.add_css_class("suggested-action")
        self._play.connect("clicked", self._on_play)
        following = ui.icon_button(
            "media-skip-forward-symbolic", "Next wallpaper", lambda *_: self.state.step(1), "flat", "player-control"
        )
        self._next = following
        self._rotate = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic")
        self._rotate.add_css_class("flat")
        self._rotate.add_css_class("player-control")
        self._rotate.set_tooltip_text("Change wallpaper automatically")
        self._rotate.connect("toggled", self._on_rotate)
        # Shuffle/rotate sit in their own boxes so a narrow-window breakpoint can
        # hide them without fighting refresh(), which manages the buttons inside.
        self.compact_hidden: list[Gtk.Widget] = []
        shuffle_box, rotate_box = Gtk.Box(), Gtk.Box()
        shuffle_box.append(self._shuffle)
        rotate_box.append(self._rotate)
        for widget in (shuffle_box, previous, self._play, following, rotate_box):
            controls.append(widget)
        self.compact_hidden += [shuffle_box, rotate_box, self._resume_box]
        self._controls = controls
        bar.set_center_widget(controls)

        # -- right: timing, scope, more -------------------------------------
        right = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self._timing = Gtk.Label()
        self._timing.add_css_class("dimmed")
        self._timing.add_css_class("caption")
        self._timing.add_css_class("numeric")
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
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more_model)
        more.add_css_class("flat")
        more.set_tooltip_text("More playback actions")
        details = Gtk.Box(spacing=8)
        # Battery state isn't repeated here: the window's banner says it.
        for widget in (self._timing, self._scope):
            details.append(widget)
        right.append(details)
        right.append(more)
        self.compact_hidden.append(details)
        bar.set_end_widget(right)

        actions = Gio.SimpleActionGroup()
        self._actions = actions
        for name, callback in (
            ("resume", lambda *_: self.state.resume_schedule(self.state.scope)),
            ("random", lambda *_: self.state.random()),
            ("stop", lambda *_: self.state.stop()),
            ("why", lambda *_: self._explain()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        scope = Gio.SimpleAction.new_stateful("scope", GLib.VariantType.new("s"), GLib.Variant("s", "all"))
        scope.connect("activate", self._on_scope)
        actions.add_action(scope)
        self.insert_action_group("player", actions)

        state.connect(
            "changed",
            lambda _s, topic: (
                topic in ("now", "playback", "system", "displays", "playlists", "settings", "library")
                and self.refresh()
            ),
        )
        self.refresh()

    # -- callbacks -----------------------------------------------------------
    def _on_play(self, *_args) -> None:
        if not self.state.service_running:
            self.state.set_service_running(True)
            self.state.toast("Wallpaper service started")
            return
        self.state.toggle_play()

    def _on_shuffle(self, button: Gtk.ToggleButton) -> None:
        if not self._building:
            self.state.set_shuffle(button.get_active())

    def _on_rotate(self, button: Gtk.ToggleButton) -> None:
        if not self._building:
            self.state.set_rotate(button.get_active())

    def _on_scope(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        action.set_state(value)
        self.state.set_scope(value.get_string())  # e.g. the inspector's "Apply to …" follows
        self.refresh()

    def _show_current(self, *_args) -> None:
        connector = self.state.targets()[0]
        self.state.navigate("library:" + self.state.current[connector])

    def _explain(self) -> None:
        self.state.navigate("schedule:why")

    # -- view ----------------------------------------------------------------
    def _reason(self, connector: str) -> str:
        """Why this display shows what it shows, e.g. "Frog day · from schedule until 18:00"."""
        state = self.state
        playlist_id = state.effective_playlist(connector)
        if connector in state.manual:
            return "Your pick" if playlist_id == "quick" else f"{state.playlist(playlist_id).name} · your pick"
        resolution = state.resolution(connector)
        name = state.playlist(playlist_id).name
        if resolution.rule is not None:
            why = f"{name} · from schedule"
        elif state.assigned.get(connector) == playlist_id:
            why = f"{name} · this display’s playlist"
        else:
            why = f"{name} · nothing scheduled"
        if resolution.until:
            why += f" until {resolution.until}"
        return why

    def refresh(self) -> None:
        self._building = True
        state = self.state
        targets = state.targets()
        shown = [state.current[t] for t in targets]
        unique = list(dict.fromkeys(shown))

        child = self._thumbs.get_first_child()
        while child:
            self._thumbs.remove(child)
            child = self._thumbs.get_first_child()
        # Two displays showing different wallpapers get two overlapping thumbs.
        for index, wid in enumerate(unique[:2]):
            thumb = ui.thumbnail(state.wallpaper(wid), 72, 40, 7)
            if len(unique) > 1:
                thumb.add_css_class("stacked-thumb")
            self._thumbs.put(thumb, index * 26, index * 6 if len(unique) > 1 else 3)

        if not state.service_running:
            self._title.set_label("Wallpaper service isn't running")
            self._why.set_label("Your last wallpaper stays on screen")
        else:
            # "·" separates facts in the line below, so names are joined in words.
            self._title.set_label(_and_join([state.wallpaper(wid).name for wid in unique]))
        self._title.set_tooltip_text(
            "\n".join(f"{connector}: {state.wallpaper(state.current[connector]).name}" for connector in targets)
        )

        connector = targets[0]
        playlist_id = state.effective_playlist(connector)
        if state.service_running:
            reasons = {target: self._reason(target) for target in targets}
            if len(set(reasons.values())) > 1:
                # Two displays playing for different reasons: say so rather
                # than describing only the first one.
                why = "Different on each display"
                self._why.set_tooltip_text("\n".join(f"{target}: {text}" for target, text in reasons.items()))
            else:
                why = reasons[connector]
                self._why.set_tooltip_text(None)
            if state.playback == "paused":
                why = "Paused · " + why
            elif state.playback == "stopped":
                why = "Still only · " + why
            self._why.set_label(why)
        manual_now = state.service_running and not state.following_schedule(
            state.scope if state.scope != "all" else None
        )
        self._resume.set_visible(manual_now)
        self._actions.lookup_action("resume").set_enabled(manual_now)

        self._dot.remove_css_class("paused")
        self._dot.remove_css_class("stopped")
        if not state.service_running or state.playback == "stopped":
            self._dot.add_css_class("stopped")
        elif state.playback == "paused":
            self._dot.add_css_class("paused")

        if not state.service_running:
            self._play.set_icon_name("media-playback-start-symbolic")
            self._play.set_tooltip_text("Start the wallpaper service")
        elif state.playback == "playing":
            self._play.set_icon_name("media-playback-pause-symbolic")
            self._play.set_tooltip_text("Pause animation")
        else:
            self._play.set_icon_name("media-playback-start-symbolic")
            self._play.set_tooltip_text("Resume animation")
        for child in (self._shuffle, self._rotate, self._previous, self._next):
            child.set_sensitive(state.service_running)
        self._shuffle.set_active(state.shuffle_on())
        self._rotate.set_active(state.rotate)

        if not state.service_running or playlist_id == "quick":
            self._timing.set_label("")
        elif state.rotate:
            self._timing.set_label(f"Next in {state.next_change_minutes} min")
        else:
            self._timing.set_label("Not changing")
        self._timing.set_visible(bool(self._timing.get_label()))

        self._scope.set_visible(state.display_mode == "independent")
        self._scope.set_label(state.scope_label())
        self._scope_menu_model.remove_all()
        self._scope_menu_model.append("All displays", "player.scope::all")
        for display in state.displays:
            self._scope_menu_model.append(
                f"{display.connector} — {display.model}", f"player.scope::{display.connector}"
            )
        self._building = False


def _and_join(names: list[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
