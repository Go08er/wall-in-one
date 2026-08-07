# Translucency and blur under niri

Wall-in-One draws its own translucency. It never asks the compositor for blur.

That split is deliberate. niri exposes blur through
[`ext-background-effect-v1`](https://wayland.app/protocols/ext-background-effect-v1),
but it also lets you turn blur on from the config for any window, with no
protocol code in the application at all. Since the config route needs nothing
from us and survives the app not being able to reach the Wayland socket, the app
ships a **Window opacity** setting and this page instead of a protocol binding.

Everything below is verified against **niri 26.04** and its bundled wiki
(`share/doc/niri/wiki/Window-Effects.md`,
`Configuration:-Window-Rules.md`, `Configuration:-Miscellaneous.md`). Every
snippet passes `niri validate`.

## The app-id to match on

```
dev.goober.WallInOne
```

This is `wall_in_one.paths.APPLICATION_ID`. GtkApplication sets the Wayland
app-id from the GApplication id, so that string -- not the `wall-in-one` command
name -- is what a window rule has to match. Confirmed by reading
`niri msg -j windows` against a live instance.

## Minimum config

Set **Settings -> Appearance -> Window opacity** to something below 1.0 first:
a fully opaque window covers the blur completely and nothing will look
different. It will not go below 0.30, which is where the window stops being
legible against a busy wallpaper and no amount of blur rescues it.

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

## Tuning

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

## Rounded corners

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

## The xray caveat -- read this if you use a video wallpaper

When any background effect is active, niri turns **xray on by default**. Xray
sees through to the wallpaper only, ignoring windows underneath, and it is much
cheaper: the wallpaper is blurred once and reused, because a wallpaper normally
never changes.

Wall-in-One's whole job includes wallpapers that do change, every frame. With a
video wallpaper running, xray blur is recomputed **every frame**. It is still
computed once and shared across all windows rather than per-window, so it is not
catastrophic, but it is a real cost that a static wallpaper does not pay. The
app's `dynamics` toggle (`wall-in-one ctl dynamics off`) exists partly for this:
it pauses video wallpapers and shows their paired stills, which puts the blur
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

## If the window looks tinted

niri draws the focus ring and border as a solid rectangle *behind* the window by
default, so they show through anything semitransparent. Either set
`prefer-no-csd` at the top level of your config, or override it for this window:

```kdl
window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#
    draw-border-with-background false
}
```

## Older niri

`background-effect` is `Since: 26.04`. On anything older the window rule is a
config error, so leave it out -- the app's opacity setting still works, you just
get plain translucency with no blur behind it. Nothing in the app depends on
blur being available.

## In-app opacity vs the niri `opacity` rule

They are not the same thing, and the app's own setting is usually what you want.

- The **app's** opacity makes background colours translucent and leaves text and
  icons fully opaque, so contrast survives.
- niri's `opacity` window rule fades the *entire* window, text included, which
  gets hard to read fast.

Use the app's setting. Reach for niri's `opacity` only if you want the whole
window ghosted on purpose.
