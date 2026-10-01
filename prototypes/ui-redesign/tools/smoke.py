"""End-to-end smoke test of the prototype (headless): drives every page and demo scene,
reports exceptions. Run with tools/smoke.sh."""

import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib
from wio_demo import art, data, thumbs, ui
from wio_demo.shell import MainWindow
from wio_demo.state import AppState

failures: list[str] = []
steps_run = 0
original_hook = sys.excepthook


def hook(kind, value, tb):  # exceptions raised inside GTK callbacks
    failures.append("callback: " + "".join(traceback.format_exception(kind, value, tb))[-600:])


sys.excepthook = hook


def step(name, fn):
    global steps_run
    steps_run += 1
    try:
        fn()
    except Exception:
        failures.append(f"{name}: " + traceback.format_exc()[-800:])


def settle(until, seconds: float = 5.0) -> None:
    """Run the main loop until ``until()`` is true (or the time is up)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)


def check_thumbnails_off_main_thread(library) -> None:
    """Rebuild the Library from a cold cache: card pictures must be drawn by
    the loader's workers, and every card must have its picture afterwards."""
    threads: list[str] = []
    render = art.render

    def recording(*args):
        if args[3:] == (480, 270):
            threads.append(threading.current_thread().name)
        return render(*args)

    art.render = recording
    try:
        art.texture.cache_clear()
        thumbs.LOADER._ready.clear()
        library._rebuild()
        settle(lambda: not thumbs.LOADER.pending())
    finally:
        art.render = render
    assert thumbs.LOADER.pending() == 0, "thumbnails still pending"
    assert threads, "no card thumbnails were drawn"
    on_main = [name for name in threads if not name.startswith("thumbnails-")]
    assert not on_main, f"card thumbnails drawn on {set(on_main)}"
    blank = [card.wallpaper.id for card in library._cards.values() if card.frame.get_child()._paintable is None]
    assert not blank, f"cards still showing placeholders: {blank}"


def inspector_colors(state, library) -> None:
    library.inspect(state.wallpaper("alpine"))
    state.set_wallpaper_colors("alpine", scheme="vibrant")
    assert state.wallpaper("alpine").scheme == "vibrant"
    state.set_wallpaper_colors("alpine", mode="palette", palette="Nord")
    state.set_wallpaper_colors("alpine", theme_mode="dark")
    wallpaper = state.wallpaper("alpine")
    assert (wallpaper.color_mode, wallpaper.palette, wallpaper.theme_mode) == ("palette", "Nord", "dark")
    state.set_wallpaper_colors("alpine", mode="adaptive", scheme=None, theme_mode="auto")
    assert state.wallpaper("alpine").scheme is None


def inspector_retry(state, library) -> None:
    library.inspect(state.wallpaper("neon-rain"))
    library.inspector._retry(state.wallpaper("neon-rain"))
    assert state.wallpaper("neon-rain").problem == ""


def library_bulk_favorite(state, library) -> None:
    library.demo("select")
    library._bulk_favorite()
    assert all(state.wallpaper(wid).favorite for wid in ("lily-pond", "golden-coast", "misty-pines"))


def palette_actions(state, settings) -> None:
    dialog = settings._open_palettes()
    dialog._duplicate(state.find_palette("Nord"))
    copy = state.find_palette("Nord copy")
    assert copy is not None and copy.editable
    state.save_palette("Nord copy", "Nord mine", copy.light, copy.dark)
    dialog._apply(state.find_palette("Nord mine"))
    assert state.applied_palette().name == "Nord mine"
    dialog.delete(state.find_palette("Nord mine"))
    assert state.applied_palette() is None and state.find_palette("Nord mine") is None
    copy, undo = state.duplicate_palette("Dracula")
    undo()
    assert state.find_palette(copy.name) is None
    undo = state.delete_palette("Ayu")
    undo()
    assert state.find_palette("Ayu") is not None
    undo = state.apply_palette("Gruvbox")
    undo()
    dialog.force_close()


def build_steps(app, holder):
    w = lambda: holder["window"]  # noqa: E731
    s = lambda: holder["window"].state  # noqa: E731
    steps = []
    for key in ("library", "store", "playlist:frog-day", "playlist:all-media", "schedule", "displays", "settings"):
        steps.append((f"navigate {key}", lambda k=key: w().navigate(k)))
    lib = lambda: w().pages["library"]  # noqa: E731
    steps.append(
        ("thumbnails off the main thread", lambda: (w().navigate("library"), check_thumbnails_off_main_thread(lib())))
    )
    for wallpaper in data.WALLPAPERS:
        steps.append((f"inspect {wallpaper.id}", lambda wp=wallpaper: (w().navigate("library"), lib().inspect(wp))))
    for mode in ("palette", "keep", "adaptive"):

        def color(m=mode):
            s().set_wallpaper_colors("alpine", mode=m)
            lib().inspector.show(s().wallpaper("alpine"))

        steps.append((f"color mode {mode}", color))
    steps.append(("inspector colors", lambda: inspector_colors(s(), lib())))
    steps.append(("inspector retry", lambda: inspector_retry(s(), lib())))
    steps.append(("library bulk favorite", lambda: library_bulk_favorite(s(), lib())))
    for key in ("still", "video", "scene", "all"):
        steps.append((f"filter {key}", lambda k=key: lib()._kinds.set_active_name(k)))
    for key in ("name", "kind", "color", "added"):
        steps.append((f"sort {key}", lambda k=key: lib()._set_sort(k)))
    steps += [
        ("size small", lambda: lib()._set_size("small")),
        ("size large", lambda: lib()._set_size("large")),
        ("search", lambda: lib().search.set_text("frog")),
        ("search clear", lambda: lib().search.set_text("")),
        ("select + bulk add", lambda: (lib().demo("select"), lib()._bulk_add("cozy-rain"))),
        ("apply all", lambda: s().apply("alpine", "all")),
        ("apply one display", lambda: s().apply("dune-sea", "HDMI-A-1")),
        ("favorite", lambda: s().toggle_favorite("dune-sea")),
        ("resume schedule", lambda: s().resume_schedule("all")),
        ("play playlist", lambda: s().play_playlist("mc-day")),
        ("next/prev/random", lambda: (s().step(1), s().step(-1), s().random())),
        ("pause/stop/play", lambda: (s().toggle_play(), s().stop(), s().toggle_play())),
        ("shuffle/rotate", lambda: (s().set_shuffle(True), s().set_rotate(False), s().set_rotate(True))),
        (
            "empty playlist play",
            lambda: (
                s().playlists.insert(0, data.Playlist("empty-x", "Empty X", [])),
                data.PLAYLIST_BY_ID.__setitem__("empty-x", s().playlists[0]),
                s().emit_changed("playlists"),
                w().navigate("playlist:empty-x"),
                s().play_playlist("empty-x"),
            ),
        ),
        ("battery on/off", lambda: (s().set_battery(True), s().set_battery(False))),
        ("service off/on", lambda: (s().set_service_running(False), s().set_service_running(True))),
        ("mirrored", lambda: (setattr(s(), "display_mode", "mirrored"), s().emit_changed("displays", "now"))),
        ("independent", lambda: (setattr(s(), "display_mode", "independent"), s().emit_changed("displays", "now"))),
        ("light", lambda: (setattr(s(), "dark", False), s().emit_changed("theme", "now"))),
        ("dark", lambda: (setattr(s(), "dark", True), s().emit_changed("theme", "now"))),
    ]
    for page, scenes in {
        "store": [
            "search:night",
            "provider:MotionBGS",
            "downloading",
            "preview:wallhaven-1",
            "error",
            "rate-limit",
            "filters",
            "select",
            "reset",
        ],
        "playlist:frog-day": [
            "picker",
            "menu:2",
            "select",
            "compact",
            "rename",
            "delete",
            "added",
            "removed",
            "drag",
            "select",
        ],
        "schedule": ["edit:weekend-days", "new", "why", "drag", "december", "rules", "pick", "reorder"],
        "displays": [
            "select:HDMI-A-1",
            "advanced",
            "pick",
            "assigned",
            "unscheduled",
            "identify",
            "forget",
            "mode:mirrored",
            "colors:eDP-1",
        ],
        "settings": [
            "search:battery",
            "group:log",
            "advanced",
            "remove-folder",
            "palettes",
            "palette-edit",
            "scheme",
            "download-folder",
            "no-key",
            "template-missing",
        ],
    }.items():
        for scene in scenes:

            def run(p=page, sc=scene):
                dialog = w().get_visible_dialog()
                if dialog is not None:
                    dialog.force_close()
                w().navigate(p)
                w().pages[p.partition(":")[0]].demo(sc)

            steps.append((f"{page}+{scene}", run))
    steps.append(
        (
            "select pill library",
            lambda: (
                w().navigate("library"),
                w().pages["library"]._select.set_active(True),
                w().pages["library"]._select.set_active(False),
            ),
        )
    )
    steps.append(
        (
            "select pill store",
            lambda: (
                w().navigate("store"),
                w().pages["store"]._select.set_active(True),
                w().pages["store"]._select.set_active(False),
            ),
        )
    )
    steps.append(
        (
            "library select all",
            lambda: (
                w().navigate("library"),
                w().pages["library"]._select.set_active(True),
                w().pages["library"].select_all(),
            ),
        )
    )
    steps.append(
        (
            "library remove two",
            lambda: w().pages["library"]._remove([s().wallpaper("dune-sea"), s().wallpaper("alpine")]),
        )
    )
    steps.append(("library select off", lambda: w().pages["library"]._select.set_active(False)))
    steps.append(
        (
            "store select all",
            lambda: (
                w().navigate("store"),
                w().pages["store"]._select.set_active(True),
                w().pages["store"].select_all(),
                w().pages["store"]._select.set_active(False),
            ),
        )
    )
    for style in ("frosted", "translucent", "frosted"):
        steps.append(
            (f"style {style}", lambda st=style: (setattr(s(), "window_style", st), s().emit_changed("appearance")))
        )
    steps.append(("panel 40%", lambda: (setattr(s(), "panel_alpha", 0.4), s().emit_changed("appearance"))))
    steps.append(("background 10%", lambda: (setattr(s(), "background_alpha", 0.1), s().emit_changed("appearance"))))
    steps.append(
        (
            "panel below background",
            lambda: (
                setattr(s(), "background_alpha", 0.8),
                setattr(s(), "panel_alpha", 0.2),
                s().emit_changed("appearance"),
            ),
        )
    )
    for frost in (0.0, 0.25, 1.0):
        steps.append((f"frost {frost}", lambda f=frost: (setattr(s(), "frost", f), s().emit_changed("appearance"))))
    steps.append(
        (
            "all clear frosted",
            lambda: (
                setattr(s(), "panel_alpha", 0.0),
                setattr(s(), "background_alpha", 0.0),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(("menu glass settings", lambda: w().activate_action("win.glass-settings", None)))

    def settings_page():
        w().navigate("settings")
        return w().pages["settings"]

    steps.append(("palette apply", lambda: (settings_page(), s().apply_palette("Nord"))))
    steps.append(("palette dialog", lambda: settings_page().demo("palettes")))
    steps.append(("palette actions", lambda: palette_actions(s(), settings_page())))
    steps.append(
        (
            "desktop colors off",
            lambda: (settings_page().values.__setitem__("desktop_colors", False), settings_page()._refresh_colors()),
        )
    )
    steps.append(
        (
            "desktop colors on",
            lambda: (settings_page().values.__setitem__("desktop_colors", True), settings_page()._refresh_colors()),
        )
    )
    steps.append(("template missing", lambda: settings_page().demo("template-missing")))
    steps.append(
        (
            "keep wallpaper",
            lambda: (s().set_wallpaper_colors("dune-sea", mode="keep"), s().apply("dune-sea", "all")),
        )
    )
    steps.append(
        (
            "style solid opacity",
            lambda: (
                setattr(s(), "window_style", "solid"),
                setattr(s(), "panel_alpha", 0.2),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(
        (
            "style translucent floor",
            lambda: (
                setattr(s(), "window_style", "translucent"),
                setattr(s(), "background_alpha", 0.0),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(
        ("style frosted again", lambda: (setattr(s(), "window_style", "frosted"), s().emit_changed("appearance")))
    )
    steps.append(("frosted light", lambda: (setattr(s(), "dark", False), s().emit_changed("theme", "now"))))
    steps.append(("frosted dark", lambda: (setattr(s(), "dark", True), s().emit_changed("theme", "now"))))
    # A simulated day with the schedule page showing, then the library.
    steps.append(("show schedule", lambda: w().navigate("schedule")))
    for tick in range(36):
        steps.append((f"tick {tick}", lambda: s().advance(40)))
    steps.append(("show library", lambda: w().navigate("library")))
    for tick in range(12):
        steps.append((f"tick L{tick}", lambda: s().advance(60)))

    def go_narrow():
        old = holder["window"]
        fresh = MainWindow(app, AppState())
        fresh.set_default_size(420, 860)
        fresh.present()
        old.destroy()
        holder["window"] = fresh

    steps.append(("narrow window", go_narrow))
    for key in ("library", "store", "playlist:frog-day", "schedule", "displays", "settings"):
        steps.append((f"narrow {key}", lambda k=key: w().navigate(k)))
    steps.append(
        ("narrow inspector", lambda: (w().navigate("library"), w().pages["library"].inspect(s().wallpaper("alpine"))))
    )
    return steps


def main():
    app = Adw.Application(application_id="dev.goober.WallInOne.Smoke")
    holder = {}

    def activate(application):
        ui.load_css()
        holder["window"] = MainWindow(application, AppState())
        holder["window"].present()
        queue = build_steps(application, holder)

        def run_next():
            if not queue:
                GLib.timeout_add(500, application.quit)
                return False
            name, fn = queue.pop(0)
            step(name, fn)
            GLib.timeout_add(60, run_next)
            return False

        GLib.timeout_add(800, run_next)

    app.connect("activate", activate)
    app.run([sys.argv[0]])
    print(f"SMOKE steps={steps_run} failures={len(failures)}")
    for failure in failures[:12]:
        print("----\n" + failure)
    Path(sys.argv[1] if len(sys.argv) > 1 else "/dev/null").write_text(str(len(failures)))


if __name__ == "__main__":
    main()
