"""The new interface's widget styles: shapes, spacing and badges, no palette.

Colours come from the application's own stylesheet (`wall_in_one.theme.css`,
at USER + 1), so these rules only use libadwaita's variables. This sits at
APPLICATION priority, below it, once per display.
"""

from __future__ import annotations

from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

STYLE: Final = """
/* ---------- cards ---------- */
.wp-card { padding: 4px; border-radius: 16px; }
.wp-frame {
  border-radius: 12px;
  box-shadow: 0 1px 2px alpha(black, 0.18), 0 2px 8px alpha(black, 0.10);
  transition: box-shadow 150ms ease-out;
}
.wp-card:hover .wp-frame, .wp-card.force-hover .wp-frame {
  box-shadow: 0 0 0 2px alpha(var(--accent-bg-color), 0.6), 0 6px 16px alpha(black, 0.22);
}
.wp-frame.selected, .wp-frame.checked {
  box-shadow: 0 0 0 3px var(--accent-bg-color), 0 6px 16px alpha(black, 0.25);
}

/* Round pick badge (selection mode, pickers): ring, then a filled tick or number. */
.wio-pick-badge {
  min-width: 26px; min-height: 26px; border-radius: 999px;
  font-weight: 800; font-size: 0.9em; font-feature-settings: "tnum";
  color: white; background-color: alpha(black, 0.30);
  box-shadow: inset 0 0 0 2px alpha(white, 0.92);
}
.wio-pick-badge.picked {
  color: var(--accent-fg-color); background-color: var(--accent-bg-color);
  box-shadow: 0 0 0 2px alpha(white, 0.92), 0 2px 6px alpha(black, 0.30);
}
.wp-name { font-weight: 600; }

.hover-only { opacity: 0; transition: opacity 120ms ease-out; }
.wp-card:hover .hover-only, .wp-card.force-hover .hover-only,
.wp-card:focus-within .hover-only { opacity: 1; }

button.on-image {
  background-color: alpha(black, 0.45);
  color: white;
  min-width: 28px; min-height: 28px; padding: 0;
}
button.on-image:hover { background-color: alpha(black, 0.65); }
button.fav { color: #ffd54a; }
button.apply-button { padding: 4px 14px; font-weight: 700; }

/* ---------- pills and badges ----------
   pill() badges are boxes. Scoped to box.pill so libadwaita's button.pill
   keeps normal button text. */
box.pill {
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 0.82em;
  font-weight: 700;
}
box.pill.on-image { background-color: alpha(black, 0.55); color: white; }
box.pill.accent { background-color: var(--accent-bg-color); color: var(--accent-fg-color); }
box.pill.warning { background-color: #e5a50a; color: black; }
box.pill.subtle { background-color: alpha(currentColor, 0.10); }
box.pill.success { background-color: alpha(#26a269, 0.18); color: #26a269; }

/* ---------- filter chips ---------- */
.chip {
  border-radius: 999px;
  padding: 4px 12px;
  min-height: 0;
}
.chip:checked { background-color: var(--accent-bg-color); color: var(--accent-fg-color); }

/* ---------- player bar ---------- */
.playerbar {
  border-top: 1px solid alpha(currentColor, 0.12);
  background-color: var(--headerbar-bg-color);
  padding: 6px 12px;
}
.playerbar .now-title { font-weight: 700; }
.play-button {
  min-width: 42px; min-height: 42px; padding: 0;
  border-radius: 999px;
}
.player-control { min-width: 34px; min-height: 34px; padding: 0; border-radius: 999px; }
.player-control:checked { color: var(--accent-color); }
.status-dot { min-width: 8px; min-height: 8px; border-radius: 999px; background-color: #26a269; }
.status-dot.paused { background-color: #e5a50a; }
.status-dot.stopped { background-color: alpha(currentColor, 0.4); }
.stacked-thumb {
  box-shadow: 0 0 0 2px var(--headerbar-bg-color), 0 2px 6px alpha(black, 0.35);
  border-radius: 7px;
}

/* ---------- inspector ---------- */
.inspector { background-color: var(--sidebar-bg-color); }
.inspector-title { font-size: 1.35em; font-weight: 800; }
.inspector-preview-badges { margin: 10px; }
.section-label {
  font-size: 0.78em; font-weight: 800; letter-spacing: 0.06em;
  opacity: 0.6;
}
.inspector-section { margin-top: 6px; }
.scheme-name { font-weight: 700; }
.frame-time { font-feature-settings: "tnum"; }
.problem-card {
  border-radius: 12px; padding: 12px;
  background-color: alpha(#e5a50a, 0.14);
  border: 1px solid alpha(#e5a50a, 0.45);
}
.choice-card {
  padding: 8px; border-radius: 12px;
  border: 1px solid alpha(currentColor, 0.10);
  background-color: alpha(currentColor, 0.03);
}
.choice-card:hover { background-color: alpha(currentColor, 0.07); }
.choice-card:checked, .choice-card.selected {
  border-color: var(--accent-bg-color);
  box-shadow: inset 0 0 0 1px var(--accent-bg-color);
  background-color: alpha(var(--accent-bg-color), 0.10);
}

/* ---------- sidebar ---------- */
.sidebar-count { font-size: 0.85em; opacity: 0.55; font-feature-settings: "tnum"; }
"""

_installed: set[int] = set()
_providers: list[Gtk.CssProvider] = []


def install(display: Gdk.Display | None = None) -> None:
    """Load `STYLE` for ``display`` (the default one) once."""
    target = display or Gdk.Display.get_default()
    if target is None or hash(target) in _installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(STYLE)
    Gtk.StyleContext.add_provider_for_display(
        target, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _installed.add(hash(target))
    _providers.append(provider)
