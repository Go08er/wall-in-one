# Compositor window rules (optional)

Wall-in-One runs on any compositor Noctalia 5 or later supports, and needs no
window rule on any of them. This page is for the one look that is the
compositor's to give: blur behind a translucent window.

Wall-in-One draws its own translucency. It never asks the compositor for blur.
Many compositors can blur behind a window from their own configuration, with no
protocol code in the application at all; since that route needs nothing from
the app and survives it not being able to reach the Wayland socket, the app
ships its own window styles and this page instead of a protocol binding.

What is the same everywhere comes first: the app-id a rule matches and the
window styles. Then, as one worked example, niri's rules. Other compositors'
rules are not listed because they have not been verified here; look up "window
rules" and "blur" in your compositor's documentation and match on the app-id
below.

## Sway and Hyprland: one line the service needs

This is not a window rule, but it is the one line that Sway, and sometimes
Hyprland, needs in its config. The wallpaper service runs as a user unit and
finds the compositor through `WAYLAND_DISPLAY`. The systemd user manager has
that variable only if the session put it there. `niri-session` does; Sway
does not; Hyprland normally does it itself. Check with
`systemctl --user show-environment | grep '^WAYLAND_DISPLAY='`, and if it is
missing:

- Sway, in its config:

  ```
  exec systemctl --user import-environment WAYLAND_DISPLAY
  ```

- Hyprland, in `hyprland.conf`:

  ```
  exec-once = dbus-update-activation-environment --systemd WAYLAND_DISPLAY
  ```

The Sway VM test runs this import; the Hyprland line is not tested here.
[Installing](installing.md#the-service-needs-your-sessions-display) says how
to start the unit on Sway, which has no session target of its own, and what to
do if the unit started before the import.

## The app-id to match on

```
dev.goober.WallInOne
```

This is `wall_in_one.paths.APPLICATION_ID`. GtkApplication sets the Wayland
app-id from the GApplication id, so that string -- not the `wall-in-one` command
name -- is what a window rule has to match. Confirmed on niri by reading
`niri msg -j windows` against a live instance.

## Window styles

Choose the **Translucent** window style first, in the main menu's **Window
style** or Settings → Appearance: the default, **Solid**, covers the blur
completely and nothing will look different. Translucent and Frosted have two
dials, in the main menu's **Opacity and frost…** and in Settings → Appearance:
**Background opacity** for the page behind the grid, and **Panel opacity** for
the sidebar, header, player bar and cards. They run from 0 to 100%, and each
style keeps its own values in `ui.toml`.

The **Frosted** style needs no compositor blur: the window draws the wallpaper
on screen behind its panels, blurred by its own **Frost** dial, so it looks the
same on any compositor.

*In the classic interface* the setting is **Settings → Appearance → Window
opacity**, which will not go below 0.30, where the window stops being legible
against a busy wallpaper and no amount of blur rescues it. In the new
interface's Settings that row is under **Classic interface**, and it changes
only the classic window.

## Example: niri

niri exposes blur through
[`ext-background-effect-v1`](https://wayland.app/protocols/ext-background-effect-v1),
and also lets you turn it on from its config for any window. Everything in this
section is verified against **niri 26.04** and its bundled wiki
(`share/doc/niri/wiki/Window-Effects.md`, `Configuration:-Window-Rules.md`,
`Configuration:-Layer-Rules.md`, `Configuration:-Miscellaneous.md`). Every
snippet passes `niri validate`.

### Minimum config

```kdl
window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#

    background-effect {
        blur true
    }
}
```

Note the `r#"..."#` raw string. KDL rejects `\.` inside a plain quoted string
(`invalid escape char`), so a regex with escaped dots must be a raw string.

### Tuning

Blur parameters are global, not per-window, and live at the top level:

```kdl
// These are niri's defaults.
blur {
    passes 3
    offset 3
    noise 0.02
    saturation 1.5
}
```

- `passes` -- dual-Kawase downsample/upsample passes. More is smoother and
  costs more GPU.
- `offset` -- pixel offset multiplier per pass. Larger is smoother at *no*
  extra cost, so reach for this before `passes`.
- `noise` -- dither, to break up colour banding.
- `saturation` -- `1` is untouched, `1.5` is niri's default lift.

`noise` and `saturation` can be overridden per window inside
`background-effect {}`. `passes` and `offset` cannot.

Adding `blur { off }` disables blur everywhere, including blur that
applications requested over the protocol.

### Rounded corners

Blur enabled by a window rule follows the window's `geometry-corner-radius`, so
give it one or the blurred region will be a hard rectangle behind libadwaita's
rounded window:

```kdl
window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#

    geometry-corner-radius 12
    clip-to-geometry true

    background-effect {
        blur true
    }
}
```

### The xray caveat -- read this if you use a video wallpaper

When any background effect is active, niri turns **xray on by default**. Xray
sees through to the wallpaper only, ignoring windows underneath, and it is much
cheaper: the wallpaper is blurred once and reused, because a wallpaper normally
never changes.

Wall-in-One's whole job includes wallpapers that do change, every frame. With a
video wallpaper running, xray blur is recomputed **every frame**. It is still
computed once and shared across all windows rather than per-window, so it is not
catastrophic, but it is a real cost that a static wallpaper does not pay. The
app's animation toggle (`wall-in-one ctl dynamics off`) exists partly for this:
it stops video and scene renderers and shows paired stills, which puts the blur
back to being computed once.

You can ask for true blur -- everything below the window, not just the
wallpaper -- with `xray false`:

```kdl
window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#

    background-effect {
        blur true
        xray false
    }
}
```

niri documents non-xray effects as **experimental**. The known limitation is
that they disappear during window open/close animations and while dragging a
tiled window. Enable it knowing that.

### If the window looks tinted

niri draws the focus ring and border as a solid rectangle *behind* the window by
default, so they show through anything semitransparent. Either set
`prefer-no-csd` at the top level of your config, or override it for this window:

```kdl
window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#
    draw-border-with-background false
}
```

### Older niri

`background-effect` is `Since: 26.04`. On anything older the window rule is a
config error, so leave it out -- the Translucent style still works, you just
get plain translucency with no blur behind it, and Frosted blurs on its own.
Nothing in the app depends on blur being available.

### In-app opacity vs the niri `opacity` rule

They are not the same thing, and the app's own setting is usually what you want.

- The **app's** opacity dials make background colours translucent and leave
  text and icons fully opaque, so contrast survives.
- niri's `opacity` window rule fades the *entire* window, text included, which
  gets hard to read fast.

Use the app's setting. Reach for niri's `opacity` only if you want the whole
window ghosted on purpose.

### The wallpaper in the overview

mpvpaper and linux-wallpaperengine draw on the layer-shell `background` layer.
niri 26.04 can also show a background-layer wallpaper in the overview's
backdrop and between workspaces, with a layer rule (`place-within-backdrop`,
since 25.05). This is optional. Find your wallpaper tool's namespace with
`niri msg layers`, then:

```kdl
layer-rule {
    // The namespace `niri msg layers` shows for the wallpaper surface.
    match namespace="^wallpaper$"

    place-within-backdrop true
}
```

niri documents that this only works for background-layer surfaces that ignore
exclusive zones, which is what wallpaper tools are.
