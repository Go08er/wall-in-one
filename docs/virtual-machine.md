# Development VM

Wall-in-One ships two NixOS VM fixtures. They are isolated from the host and
answer different questions:

- the development VM is a real desktop a person can explore;
- the automated VM test exercises the service and captures every workflow
  page without opening anything in the host session.

Both use the flake's locked niri and Noctalia packages, the Wall-in-One package
built from the checkout, and the companion plugin fetched by the locked
`noctalia-plugins` input. Neither mounts the host home directory.

## Launch the desktop

From the repository root:

```console
$ nix run .#vm
```

The guest account is `wallpaper`, with password `wall-in-one`. The graphical
session starts automatically. Wall-in-One's user service starts at login,
Noctalia loads the companion plugin from its immutable Nix-store path source,
and the app's library contains three generated stills and a four-second
generated video on first boot. No file from the host wallpaper collection is
copied or mounted.

The flake input normally uses the published companion-plugin repository. To
test an uncommitted local plugin checkout whose `wall-in-one/` directory is
under `noctalia_5/`, override just that input:

```console
$ nix run \
    --override-input noctalia-plugins \
    path:/home/goober/Documents/goober-noctalia-plugins-v5/noctalia_5 \
    .#vm
```

The VM allocates four virtual CPUs, 6 GiB of memory, and a 16 GiB virtual disk.
It uses QEMU's virtio display with software rendering. Niri deliberately
rejects software EGL on its direct TTY backend, so a minimal Cage session owns
the virtual framebuffer and runs the real niri compositor as its sole,
full-screen nested client. Noctalia, Wall-in-One, and the test controls all use
niri's nested Wayland and IPC sockets.

## Run the automated desktop test

The VM test is part of the default flake checks:

```console
$ nix flake check -L
```

It can also be built on its own:

```console
$ nix build -L .#checks.x86_64-linux.vm-test
```

The result contains PNG screenshots named `wall-in-one-media.png`,
`wall-in-one-pairings.png`, `wall-in-one-playlists.png`, and
`wall-in-one-schedules.png`. The test also verifies that:

- niri exposes a visible output and Noctalia loads the companion plugin;
- `wall-in-one-service` starts from a handwritten resolved config and owns a
  responsive runtime socket without importing Python;
- Media, Pairings, Playlists, and Display schedules open in the running app;
- closing the GUI leaves the same service process alive and rotation advances;
- a control-socket playlist switch changes the active media;
- a schedule timer observes an injected local-time boundary and applies its
  playlist.

The guest clock is changed only inside the disposable VM. All guest config,
state, cache, media, and runtime sockets live under the guest's own home or
`/run/user/1000`; the test never reads the host's Noctalia configuration,
library, Steam directory, or home directory.

## Honest limits

This fixture has one virtual output and software rendering. Steam is not
installed, no Workshop content is copied into it, and no Wallpaper Engine scene
is available to render. The app therefore reports an empty scene library and
degrades normally. The VM proves the application/service lifecycle, ordinary
still and video library wiring, Noctalia/plugin integration, schedule control,
and the four GTK pages. It does **not** prove GPU rendering, Wallpaper Engine
scene playback, Steam integration, or multi-monitor behavior.
