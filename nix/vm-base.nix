{
  config,
  lib,
  pkgs,
  wallInOnePackage,
  pluginSource,
  sampleMedia,
  noctaliaProbe ? null,
  ...
}:

let
  user = "wallpaper";
  home = "/home/${user}";
  mediaDir = "${home}/Pictures/Wallpapers";
  runtimeDir = "/run/user/1000";

  appSettings = pkgs.writeText "wall-in-one-vm-settings.toml" ''
    roots = ["${mediaDir}"]
    opacity = 0.94
    preview_scheme = "vibrant"
    follow_noctalia_palette = false
    cycle_interval = 5
    cycle_enabled = true
    shuffle = false
    dynamics_enabled = true
    video_muted = true
    video_volume = 0
    video_when_hidden = "pause"
    video_hardware_decode = false
    video_interpolation = "oversample"
    scene_fps = 30
    cycle_favourites_only = false
    active_playlist = "day"
    scan_workshop = false
    own_scene_renderer = true
    output = ""
  '';

  playlists = pkgs.writeText "wall-in-one-vm-playlists.json" (
    builtins.toJSON {
      version = 1;
      playlists = [
        {
          id = "day";
          name = "Day samples";
          entries = [
            {
              id = "day-grid";
              source = "${mediaDir}/colour-grid.png";
            }
            {
              id = "day-bars";
              source = "${mediaDir}/colour-bars.png";
            }
          ];
        }
        {
          id = "night";
          name = "Night samples";
          entries = [
            {
              id = "night-grid";
              source = "${mediaDir}/night-grid.png";
            }
          ];
        }
        {
          id = "video";
          name = "Video samples";
          entries = [
            {
              id = "video-grid";
              source = "${mediaDir}/moving-grid.mp4";
            }
          ];
        }
      ];
    }
  );

  schedules = pkgs.writeText "wall-in-one-vm-schedules.json" (
    builtins.toJSON {
      version = 1;
      rules = [ ];
    }
  );

  # Deliberately the previously shipped contract. ExecStartPre must replace
  # these bytes from the authoring stores before current-schema Rust is allowed to
  # start; otherwise this VM reproduces the real upgrade outage.
  runtimeConfig = pkgs.writeText "wall-in-one-vm-runtime-v1.toml" ''
    schema_version = 1
    upgrade_fixture = "must be replaced headlessly"
  '';

  noctaliaSettings = pkgs.writeText "wall-in-one-vm-noctalia.toml" ''
    config_version = 8

    [shell]
    offline_mode = true
    telemetry_enabled = false
    setup_wizard_enabled = false
    clipboard_enabled = true

    [plugins]
    enabled = ["goober/wall-in-one"]
    auto_update = false

    [[plugins.source]]
    name = "wall-in-one-vm"
    kind = "path"
    location = "${pluginSource}"
    enabled = true

    [plugin_settings."goober/wall-in-one"]
    binary_path = "${wallInOnePackage}/bin/wall-in-one"
    refresh_interval_seconds = 5

    [widget.wall-in-one]
    type = "goober/wall-in-one:wall-in-one"
    display_mode = "always"

    [bar.wall-in-one-vm]
    enabled = true
    position = "top"
    start = ["wall-in-one"]
    center = []
    end = []
    reserve_space = true
    hover_highlight = true
  '';

  niriConfig = pkgs.writeText "wall-in-one-vm-niri.kdl" ''
    hotkey-overlay {
        skip-at-startup
    }

    prefer-no-csd

    window-rule {
        match app-id="dev.goober.WallInOne"
        open-maximized true
    }

    binds {
        Mod+Return { spawn "${lib.getExe pkgs.foot}"; }
        Mod+Q { close-window; }
        Mod+Shift+E { quit; }
    }
  '';

  # Niri intentionally skips software EGL renderers on its TTY backend. The
  # NixOS test QEMU has a display-only virtio GPU, so give Cage that device and
  # run the real niri session as Cage's sole, full-screen client. Applications
  # and Noctalia connect to niri's nested socket, not to Cage. This is the same
  # software-rendered shape for the interactive VM and the screenshot test.
  nestedNiriSession = pkgs.writeShellScript "wall-in-one-nested-niri" ''
    set -eu

    export HOME=${lib.escapeShellArg home}
    export USER=${lib.escapeShellArg user}
    export XDG_CONFIG_HOME=${lib.escapeShellArg "${home}/.config"}
    export XDG_STATE_HOME=${lib.escapeShellArg "${home}/.local/state"}
    export XDG_CACHE_HOME=${lib.escapeShellArg "${home}/.cache"}
    export XDG_DATA_HOME=${lib.escapeShellArg "${home}/.local/share"}
    export XDG_RUNTIME_DIR=${lib.escapeShellArg runtimeDir}
    export DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus

    ${pkgs.niri}/bin/niri --config ${niriConfig} &
    niri_pid=$!
    trap 'kill "$niri_pid" 2>/dev/null || true' EXIT INT TERM

    for _ in $(${pkgs.coreutils}/bin/seq 1 100); do
      niri_socket=$(${pkgs.findutils}/bin/find ${runtimeDir} -maxdepth 1 -type s -name 'niri*.sock' -print -quit)
      if [ -S ${runtimeDir}/wayland-1 ] && [ -n "$niri_socket" ]; then
        break
      fi
      ${pkgs.coreutils}/bin/sleep 0.1
    done
    if [ ! -S ${runtimeDir}/wayland-1 ] || [ -z "''${niri_socket:-}" ]; then
      echo "nested niri did not expose its Wayland and IPC sockets" >&2
      exit 1
    fi

    export WAYLAND_DISPLAY=wayland-1
    export NIRI_SOCKET="$niri_socket"
    export XDG_CURRENT_DESKTOP=niri
    export XDG_SESSION_DESKTOP=niri
    export XDG_SESSION_TYPE=wayland

    ${pkgs.systemd}/bin/systemctl --user import-environment \
      WAYLAND_DISPLAY NIRI_SOCKET XDG_CURRENT_DESKTOP \
      XDG_SESSION_DESKTOP XDG_SESSION_TYPE
    # The VM activates the package's installed user unit verbatim.  Put the
    # instrumented Noctalia wrapper in the user manager's environment instead
    # of shadowing that unit merely to add a NixOS `path` entry.
    ${pkgs.systemd}/bin/systemctl --user set-environment \
      PATH=${lib.escapeShellArg "${if noctaliaProbe == null then pkgs.noctalia else noctaliaProbe}/bin:/run/current-system/sw/bin"}
    ${pkgs.systemd}/bin/systemctl --user restart \
      wall-in-one.service noctalia.service

    wait "$niri_pid"
  '';

  # Which desktop the VM boots (wallInOneVm.compositor below). niri is the
  # default every existing VM uses; sway is the second compositor, with no
  # niri installed at all.
  niri = config.wallInOneVm.compositor == "niri";
  sway = config.wallInOneVm.compositor == "sway";

  # Sway on its headless backend: no GPU, no input, no cage. It starts with one
  # output, HEADLESS-1; a test adds more with `swaymsg create_output`.
  #
  # niri-session puts the session's WAYLAND_DISPLAY in the systemd user
  # manager's environment; Sway does not. Without it, a user unit that is a
  # Wayland client (Noctalia's, and wall-in-one.service, which asks the
  # compositor for its outputs and runs the renderers) starts with no display.
  # So this does what docs/installing.md tells a Sway user to do: import it,
  # then start the session target, in one `exec` (separate execs run
  # concurrently), so nothing starts the units before the import. The target
  # is the one NixOS's programs.sway defines, bound to graphical-session.target,
  # which wants both units.
  swayConfig = pkgs.writeText "wall-in-one-vm-sway.conf" ''
    xwayland disable
    output HEADLESS-1 mode 1280x720
    exec "systemctl --user import-environment WAYLAND_DISPLAY; systemctl --user start sway-session.target"
  '';
in
{
  # The app and its wallpaper renderers, installed the way a NixOS user does:
  # the module puts all three in environment.systemPackages (the renderers
  # from this machine's own pkgs), so the compiler finds them in
  # /run/current-system/sw/bin.
  imports = [
    ./nixos-module.nix
    {
      options.wallInOneVm.compositor = lib.mkOption {
        type = lib.types.enum [
          "niri"
          "sway"
        ];
        default = "niri";
        internal = true;
        description = "The compositor the VM's session runs.";
      };
    }
  ];

  programs.wall-in-one = {
    enable = true;
    package = wallInOnePackage;
  };

  users.users.${user} = {
    isNormalUser = true;
    uid = 1000;
    home = home;
    createHome = true;
    extraGroups = [ "wheel" ];
    initialPassword = "wall-in-one";
    # The headless Sway session has no login of its own: the user manager
    # starts at boot instead, as a login would start it, before Sway.
    linger = lib.mkIf sway true;
  };

  programs.niri = lib.mkIf niri {
    enable = true;
    package = pkgs.niri;
    useNautilus = false;
  };

  programs.noctalia = {
    enable = true;
    package = pkgs.noctalia;
    systemd.enable = true;
    recommendedServices.enable = false;
  };

  services.cage = lib.mkIf niri {
    enable = true;
    user = user;
    program = nestedNiriSession;
  };

  services.dbus.enable = true;
  # No credentials are used in this offline fixture. Leaving the default
  # keyring daemon enabled produces a first-login password dialog over every
  # screenshot when Noctalia probes libsecret.
  services.gnome.gnome-keyring.enable = lib.mkForce false;
  services.timesyncd.enable = false;
  hardware.graphics.enable = true;
  fonts.packages = [ pkgs.dejavu_fonts ];

  environment.systemPackages = [
    # The GTK app and Noctalia both use symbolic Adwaita names. Installing the
    # theme in the guest keeps screenshot failures about our UI rather than
    # about an intentionally sparse test image.
    pkgs.adwaita-icon-theme
    pkgs.foot
    pkgs.grim
    pkgs.jq
  ]
  ++ lib.optional niri pkgs.niri
  ++ lib.optional sway pkgs.sway
  ++ [ pkgs.noctalia ]
  # The compiler looks for its programs in the profiles before any store
  # directory on PATH, so the instrumented Noctalia has to be the system
  # profile's `noctalia`, not only first on the user manager's PATH. The
  # shell's own unit still runs the real package (programs.noctalia).
  ++ lib.optional (noctaliaProbe != null) (lib.hiPrio noctaliaProbe);

  # The module put the package in environment.systemPackages, whose
  # share/systemd/user directory is on the user manager's unit search path.
  # Only add the activation edge here: defining a NixOS service with the same
  # name would shadow the installed unit and let the VM validate a hand-built
  # approximation instead of what profile users actually receive.
  systemd.user.targets.graphical-session.wants = [ "wall-in-one.service" ];

  # Everything the guest sees is constructed here. Nothing is mounted from the
  # host, and the app is explicitly pointed at this generated media directory.
  systemd.services.wall-in-one-vm-seed = {
    description = "Seed the isolated Wall-in-One VM home";
    wantedBy = [ "multi-user.target" ];
    before = [ "cage-tty1.service" ];
    serviceConfig.Type = "oneshot";
    script = ''
      install -d -m 0755 \
        "${home}/.config/niri" \
        "${home}/.config/wall-in-one" \
        "${home}/.local/state/noctalia" \
        "${home}/.local/state/wall-in-one" \
        "${mediaDir}"

      install -m 0644 ${niriConfig} "${home}/.config/niri/config.kdl"
      install -m 0644 ${appSettings} "${home}/.config/wall-in-one/settings.toml"
      install -m 0644 ${playlists} "${home}/.local/state/wall-in-one/playlists.json"
      install -m 0644 ${schedules} "${home}/.local/state/wall-in-one/schedules.json"
      install -m 0644 ${runtimeConfig} "${home}/.local/state/wall-in-one/runtime.toml"
      install -m 0644 ${noctaliaSettings} "${home}/.local/state/noctalia/settings.toml"
      touch "${home}/.local/state/noctalia/.setup-complete"
      cp --no-preserve=mode,ownership ${sampleMedia}/* "${mediaDir}/"
      # A deterministic representative still lets the headless compiler fully
      # resolve the video pairing without running ffmpeg during login.
      cp "${mediaDir}/colour-grid.png" "${mediaDir}/moving-grid.png"

      chown -R ${user}:users \
        "${home}/.config" \
        "${home}/.local" \
        "${home}/Pictures"
    '';
  };

  systemd.services.cage-tty1 = lib.mkIf niri {
    requires = [ "wall-in-one-vm-seed.service" ];
    after = [ "wall-in-one-vm-seed.service" ];
  };

  # Sway runs as the user, against the user manager the linger started, with
  # the environment a login gives it. Its config imports WAYLAND_DISPLAY and
  # starts the session (swayConfig above); nothing else starts the units.
  systemd.services.wall-in-one-vm-sway = lib.mkIf sway {
    description = "Headless Sway session for the Wall-in-One VM";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "wall-in-one-vm-seed.service"
      "user@1000.service"
    ];
    after = [
      "wall-in-one-vm-seed.service"
      "user@1000.service"
      "systemd-user-sessions.service"
    ];
    # Sway runs each `exec` through `sh -c`, found on PATH, as a login's PATH
    # would have it; systemctl comes with the service's default PATH.
    path = [ pkgs.bash ];
    environment = {
      HOME = home;
      USER = user;
      XDG_CONFIG_HOME = "${home}/.config";
      XDG_STATE_HOME = "${home}/.local/state";
      XDG_CACHE_HOME = "${home}/.cache";
      XDG_DATA_HOME = "${home}/.local/share";
      XDG_RUNTIME_DIR = runtimeDir;
      DBUS_SESSION_BUS_ADDRESS = "unix:path=${runtimeDir}/bus";
      WLR_BACKENDS = "headless";
      WLR_HEADLESS_OUTPUTS = "1";
      WLR_RENDERER = "pixman";
      WLR_LIBINPUT_NO_DEVICES = "1";
    };
    serviceConfig = {
      User = user;
      Group = "users";
      ExecStart = "${lib.getExe pkgs.sway} --config ${swayConfig}";
    };
  };

  # What NixOS's programs.sway defines for its own config's `exec`, without
  # the rest of that module (portals, polkit, Xwayland).
  systemd.user.targets.sway-session = lib.mkIf sway {
    description = "Sway compositor session";
    documentation = [ "man:systemd.special(7)" ];
    bindsTo = [ "graphical-session.target" ];
    wants = [ "graphical-session-pre.target" ];
    after = [ "graphical-session-pre.target" ];
  };

  # A login session creates this, but spelling it here documents the socket
  # namespace used by both the service and the test driver.
  systemd.tmpfiles.rules = [ "d ${runtimeDir} 0700 ${user} users -" ];

  networking.networkmanager.enable = true;
  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = true;
  };

  security.sudo.wheelNeedsPassword = false;
  time.timeZone = "America/Chicago";
  system.stateVersion = "26.05";
}
