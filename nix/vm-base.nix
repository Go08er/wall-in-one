{
  config,
  lib,
  pkgs,
  wallInOnePackage,
  pluginSource,
  sampleMedia,
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
      ];
    }
  );

  schedules = pkgs.writeText "wall-in-one-vm-schedules.json" (
    builtins.toJSON {
      version = 1;
      rules = [ ];
    }
  );

  runtimeConfig = pkgs.writeText "wall-in-one-vm-runtime.toml" ''
    schema_version = 1
    default_playlist = "day"

    [settings]
    cycle_interval_seconds = 5
    cycle_enabled = true
    shuffle = false
    dynamics_enabled = true

    [renderer]
    noctalia_program = "${lib.getExe pkgs.noctalia}"
    mpvpaper_program = "${lib.getExe pkgs.mpvpaper}"
    linux_wallpaperengine_program = "${lib.getExe pkgs.linux-wallpaperengine}"
    own_scene_renderer = false
    layer = "background"
    video_when_hidden = "pause"
    video_hardware_decode = true
    video_muted = true
    video_volume = 0
    scene_fps = 30
    scene_muted = true
    scene_volume = 0
    scene_pause_when_covered = true
    scene_scaling = ""
    scene_clamp = ""

    [[playlists]]
    id = "day"
    name = "Day samples"
    [[playlists.entries]]
    id = "day-grid"
    kind = "still"
    still = "${mediaDir}/colour-grid.png"
    palette = { kind = "keep", mode = "keep" }
    [[playlists.entries]]
    id = "day-bars"
    kind = "still"
    still = "${mediaDir}/colour-bars.png"
    palette = { kind = "keep", mode = "keep" }

    [[playlists]]
    id = "night"
    name = "Night samples"
    [[playlists.entries]]
    id = "night-grid"
    kind = "still"
    still = "${mediaDir}/night-grid.png"
    palette = { kind = "keep", mode = "keep" }
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
    ${pkgs.systemd}/bin/systemctl --user restart \
      wall-in-one.service noctalia.service

    wait "$niri_pid"
  '';
in
{
  users.users.${user} = {
    isNormalUser = true;
    uid = 1000;
    home = home;
    createHome = true;
    extraGroups = [ "wheel" ];
    initialPassword = "wall-in-one";
  };

  programs.niri = {
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

  services.cage = {
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
    wallInOnePackage
    # The GTK app and Noctalia both use symbolic Adwaita names. Installing the
    # theme in the guest keeps screenshot failures about our UI rather than
    # about an intentionally sparse test image.
    pkgs.adwaita-icon-theme
    pkgs.foot
    pkgs.grim
    pkgs.jq
    pkgs.niri
    pkgs.noctalia
  ];

  systemd.user.services.wall-in-one = {
    description = "Wall-in-One wallpaper rotation service";
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    wantedBy = [ "graphical-session.target" ];
    # Palette application is intentionally optional for the packaged app, but
    # this desktop is specifically a Noctalia integration environment. Put the
    # shell CLI on the service PATH so playlist changes exercise that boundary
    # instead of silently degrading to wallpaper-only application.
    path = [ pkgs.noctalia ];
    serviceConfig = {
      ExecStart = "${wallInOnePackage}/bin/wall-in-one-service";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

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

      chown -R ${user}:users \
        "${home}/.config" \
        "${home}/.local" \
        "${home}/Pictures"
    '';
  };

  systemd.services.cage-tty1 = {
    requires = [ "wall-in-one-vm-seed.service" ];
    after = [ "wall-in-one-vm-seed.service" ];
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
