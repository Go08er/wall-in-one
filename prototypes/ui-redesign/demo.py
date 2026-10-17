#!/usr/bin/env python3
"""Wall-in-One UI prototype — an unwired design demo with dummy data.

Run from the repository root:

    nix develop --command python prototypes/ui-redesign/demo.py

Screenshots (headless, on a Wayland compositor of their own; used to review the
design): ``prototypes/ui-redesign/screenshots.sh`` (a throwaway profile and a
headless compositor; do not run ``--screenshot`` any other way). Each open
popover or menu is drawn over the window image at its place; one that reaches
past the window's edge is cut there.

A scene is ``[flags@]navkey[+demo]``. Flags (comma separated): light, battery,
service-off, welcome, narrow, mirrored. ``navkey`` is a sidebar key such as
``library``, ``playlist:frog-day`` or ``schedule``; ``demo`` is passed to the
page's ``demo()`` hook (for example ``library+inspector:lily-pond``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The demo runs the widgets the app ships: import them from this checkout's src.
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk
from wio_demo import noctalia_live, thumbs, ui
from wio_demo.shell import MainWindow
from wio_demo.state import AppState


def scene_flags(scene: str) -> set[str]:
    flags, _, _rest = scene.rpartition("@")
    return set(filter(None, flags.split(",")))


def apply_scene(window: MainWindow, scene: str) -> None:
    _flags, _, rest = scene.rpartition("@")
    flags_set = scene_flags(scene)
    state = window.state
    state.dark = "light" not in flags_set
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK if state.dark else Adw.ColorScheme.FORCE_LIGHT
    )
    state.on_battery = "battery" in flags_set
    state.service_running = "service-off" not in flags_set
    state.display_mode = "mirrored" if "mirrored" in flags_set else "independent"
    state.window_style = next((f for f in ("frosted", "translucent") if f in flags_set), "solid")
    # Dial positions: "bg30", "panel60", "frost0" … (percent); otherwise the defaults.
    defaults = AppState()
    state.background_opacity = dict(defaults.background_opacity)
    state.panel_opacity = dict(defaults.panel_opacity)
    state.frost = defaults.frost
    dials = {"bg": "background_alpha", "panel": "panel_alpha", "frost": "frost"}
    for flag in flags_set:
        for prefix, attribute in dials.items():
            if flag.startswith(prefix) and flag[len(prefix) :].isdigit():
                setattr(state, attribute, int(flag[len(prefix) :]) / 100)
    state.emit_changed("appearance")
    if state.display_mode == "mirrored":
        # Mirrored displays show the lead display's wallpaper and follow its pick.
        lead = state.connectors()[0]
        for connector in state.connectors()[1:]:
            state.current[connector] = state.current[lead]
            if lead in state.manual:
                state.manual[connector] = state.manual[lead]
            else:
                state.manual.pop(connector, None)
    if "evening" in flags_set:
        # Let the demo clock carry the schedule past 18:00 (Frog day → Frog night).
        import datetime as dt

        state.advance(int((dt.datetime(2026, 9, 30, 19, 10) - state.now).total_seconds() // 60))
    state.emit_changed("system", "now", "playback", "theme")
    window.root_stack.set_visible_child_name("welcome" if "welcome" in flags_set else "main")
    # Close a dialog left open by the previous scene.
    dialog = window.get_visible_dialog()
    if dialog is not None:
        dialog.force_close()
    navkey, _, demo = rest.partition("+")
    if navkey:
        window.navigate(navkey)
        # The previous page's open menu stays mapped during the crossfade.
        ui.finish_page_change(window)
        # Always call demo(): pages reset themselves first, so plain scenes don't
        # inherit state left behind by an earlier "+demo" scene.
        window.pages[navkey.partition(":")[0]].demo(demo)


def mapped_popovers(window: Gtk.Widget) -> list[Gtk.Popover]:
    """Every mapped popover (menus included) below ``window``, parents before their submenus."""
    found: list[tuple[int, Gtk.Popover]] = []
    pending: list[tuple[int, Gtk.Widget]] = [(0, window)]
    while pending:
        depth, widget = pending.pop()
        if isinstance(widget, Gtk.Popover) and widget.get_mapped():
            found.append((depth, widget))
            depth += 1
        child = widget.get_first_child()
        while child is not None:
            pending.append((depth, child))
            child = child.get_next_sibling()
    return [popover for _depth, popover in sorted(found, key=lambda item: item[0])]


def popover_origin(window: Gtk.Window, popover: Gtk.Popover) -> tuple[float, float] | None:
    """Where the popover's widget sits in the window's own coordinates, or None.

    A popover has a surface of its own, placed relative to its parent surface
    (``Gdk.Popup``), and both surfaces have a transform for the shadow margin
    around their widget (``Gtk.Native.get_surface_transform``). The window
    image starts at the window widget, so both are taken off.
    """
    popup = popover.get_surface()
    if not isinstance(popup, Gdk.Popup):
        return None
    parent = popup.get_parent()
    shadow_x, shadow_y = popover.get_surface_transform()
    if parent is None:
        return None
    # Nested popovers are placed relative to their parent popover's surface.
    parent_x, parent_y = 0.0, 0.0
    if parent != window.get_surface():
        for other in mapped_popovers(window):
            if other is not popover and other.get_surface() == parent:
                origin = popover_origin(window, other)
                if origin is None:
                    return None
                parent_x, parent_y = origin
                parent_shadow = other.get_surface_transform()
                parent_x, parent_y = parent_x - parent_shadow[0], parent_y - parent_shadow[1]
                break
        else:
            return None
    else:
        window_shadow = window.get_surface_transform()
        parent_x, parent_y = -window_shadow[0], -window_shadow[1]
    return (
        parent_x + popup.get_position_x() + shadow_x,
        parent_y + popup.get_position_y() + shadow_y,
    )


def snapshot_window(window: Gtk.Window) -> Gtk.Snapshot:
    """The window, and each mapped popover on top of it at its place.

    GTK renders the window widget itself, and a popover has a surface of its
    own, so it is not part of that image; each is drawn over it separately.
    """
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
    for popover in mapped_popovers(window):
        origin = popover_origin(window, popover)
        if origin is None:
            print(f"popover {type(popover).__name__}: no place in the window; left out", file=sys.stderr)
            continue
        snapshot.save()
        snapshot.translate(Graphene.Point().init(*origin))
        Gtk.WidgetPaintable.new(popover).snapshot(snapshot, popover.get_width(), popover.get_height())
        snapshot.restore()
    return snapshot


def run_screenshots(app: Adw.Application, window: MainWindow, outdir: Path, scenes: list[str]) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    queue = list(scenes)
    holder = {"window": window, "narrow": False}

    def ensure_window(narrow: bool) -> MainWindow:
        # GTK4 cannot resize a mapped window, so narrow scenes get a fresh one.
        if holder["narrow"] != narrow:
            old = holder["window"]
            # A new state object, so the old window's pages stop receiving signals.
            fresh = MainWindow(app, AppState())
            fresh.set_default_size(*((420, 860) if narrow else (1320, 860)))
            fresh.present()
            old.destroy()
            holder.update(window=fresh, narrow=narrow)
        return holder["window"]

    def next_scene() -> bool:
        if not queue:
            app.quit()
            return False
        scene = queue.pop(0)
        current = ensure_window("narrow" in scene_flags(scene))
        try:
            apply_scene(current, scene)
        except Exception as error:  # keep going; report at the end
            print(f"scene {scene!r} failed: {error!r}", file=sys.stderr)
            import traceback

            traceback.print_exc()
        GLib.timeout_add(900, capture, scene)
        return False

    def capture(scene: str, waited: int = 0, settled: bool = False) -> bool:
        # Thumbnails load off the main thread: wait (up to 3 s) until every
        # requested picture has arrived, then give it a frame to paint.
        if not settled:
            if thumbs.LOADER.pending() and waited < 3000:
                GLib.timeout_add(50, capture, scene, waited + 50)
                return False
            if waited:
                GLib.timeout_add(100, capture, scene, waited, True)
                return False
        current = holder["window"]
        width, height = current.get_width(), current.get_height()
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in scene)
        target = outdir / f"{name}.png"
        # GTK renders the window itself: no display server to grab pixels from.
        snapshot = snapshot_window(current)
        node, renderer = snapshot.to_node(), current.get_renderer()
        if node is None or renderer is None:
            print(f"scene {scene!r}: nothing to render", file=sys.stderr)
        else:
            area = Graphene.Rect().init(0, 0, width, height)
            renderer.render_texture(node, area).save_to_png(str(target))
            print(f"saved {target}")
        GLib.timeout_add(100, next_scene)
        return False

    GLib.timeout_add(1500, next_scene)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshot", metavar="DIR", help="render scenes to PNG files and exit")
    parser.add_argument(
        "--light", action="store_true", help="start in light style (when following Noctalia, its mode wins)"
    )
    parser.add_argument(
        "--style", choices=("solid", "translucent", "frosted"), default="solid", help="window style to start with"
    )
    parser.add_argument(
        "--simulated",
        action="store_true",
        help="don't follow the real Noctalia; take colors from the demo's wallpapers instead",
    )
    parser.add_argument("scenes", nargs="*", help="scenes to capture with --screenshot")
    args = parser.parse_args()

    # Same app id as the real app, so compositor rules (e.g. your niri blur rule)
    # apply to this window too. NON_UNIQUE: it never claims the real app's D-Bus
    # name, so a running Wall-in-One is unaffected.
    app = Adw.Application(application_id="dev.goober.WallInOne", flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = AppState()
    state.dark = not args.light
    state.window_style = args.style
    if not args.screenshot:
        # Read-only: follow the real desktop's palette, mode and wallpaper.
        # Screenshots stay on simulated colors so the gallery is reproducible.
        state.attach_live(noctalia_live.LiveNoctalia(), follow=not args.simulated)

    def activate(application: Adw.Application) -> None:
        ui.load_css()
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if state.dark else Adw.ColorScheme.FORCE_LIGHT
        )
        window = MainWindow(application, state)
        window.present()
        if args.screenshot:
            run_screenshots(application, window, Path(args.screenshot), args.scenes or ["library"])

    app.connect("activate", activate)
    os.environ.setdefault("GTK_A11Y", "none")
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
