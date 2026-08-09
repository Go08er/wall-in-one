{
  pkgs,
  wallInOnePackage,
  pluginSource,
  sampleMedia,
}:

let
  inherit (pkgs) lib;
  user = "wallpaper";
  uid = "1000";
  home = "/home/${user}";
  runtimeDir = "/run/user/${uid}";
  app = "${wallInOnePackage}/bin/wall-in-one";
in
pkgs.testers.runNixOSTest {
  name = "wall-in-one-desktop-vm";
  globalTimeout = 1200;

  node.specialArgs = {
    inherit wallInOnePackage pluginSource sampleMedia;
  };

  nodes.machine =
    { ... }:
    {
      imports = [ ./vm-base.nix ];

      virtualisation = {
        cores = 4;
        memorySize = 6144;
        qemu.options = [ "-vga virtio" ];
      };
    };

  testScript = ''
    user_environment = (
        "HOME=${home} "
        "USER=${user} "
        "XDG_CONFIG_HOME=${home}/.config "
        "XDG_STATE_HOME=${home}/.local/state "
        "XDG_CACHE_HOME=${home}/.cache "
        "XDG_DATA_HOME=${home}/.local/share "
        "XDG_RUNTIME_DIR=${runtimeDir} "
        "WAYLAND_DISPLAY=wayland-1 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
        "XDG_DATA_DIRS=/run/current-system/sw/share "
        "LANG=C.UTF-8 "
        "PATH=/run/current-system/sw/bin"
    )

    def as_user(command: str) -> str:
        return f"runuser -u ${user} -- env -i {user_environment} {command}"

    def ctl(arguments: str) -> str:
        return machine.succeed(as_user("${app} ctl " + arguments)).strip()

    def niri(arguments: str) -> str:
        command = (
            "NIRI_SOCKET=$(ls ${runtimeDir}/niri*.sock | head -n1) "
            "${lib.getExe pkgs.niri} msg " + arguments
        )
        return machine.succeed(as_user(command)).strip()

    start_all()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("cage-tty1.service")
    machine.wait_for_file("${runtimeDir}/bus")
    machine.wait_until_succeeds("test -S ${runtimeDir}/wayland-1", timeout=30)
    machine.wait_until_succeeds(as_user("systemctl --user is-active wall-in-one.service"), timeout=30)
    machine.wait_until_succeeds(as_user("systemctl --user is-active noctalia.service"), timeout=30)
    machine.wait_for_file("${runtimeDir}/wall-in-one-runtime.sock")

    with subtest("headless service owns a responsive socket"):
        machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -e '.cycle_enabled == true'"))
        service_pid = machine.succeed(
            as_user("systemctl --user show -p MainPID --value wall-in-one.service")
        ).strip()
        assert int(service_pid) > 1, service_pid

    with subtest("companion plugin loads from an isolated path source"):
        machine.wait_until_succeeds(
            "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
            "| grep -F \"loaded plugin 'goober/wall-in-one' (4 entries)\"",
            timeout=60,
        )

    with subtest("nested niri owns a visible output"):
        assert "winit" in niri("outputs")
        machine.wait_until_succeeds(
            "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
            "| grep -F 'outputs=1'",
            timeout=30,
        )

    with subtest("every workflow page renders"):
        for page in ("media", "pairings", "playlists", "schedules"):
            assert ctl(f"open {page}") == f"opened {page}"
            machine.wait_until_succeeds(
                as_user(
                    "NIRI_SOCKET=$(ls ${runtimeDir}/niri*.sock | head -n1) "
                    "${lib.getExe pkgs.niri} msg windows "
                    "| grep -F 'dev.goober.WallInOne'"
                ),
                timeout=30,
            )
            machine.sleep(2)
            machine.screenshot(f"wall-in-one-{page}")

    with subtest("closing the GUI does not stop service rotation"):
        ctl("playlist-use day")
        before = machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")).strip()
        niri("action close-window")
        machine.wait_until_succeeds(
            as_user(
                "systemctl --user show -p MainPID --value wall-in-one.service "
                "| grep -Fx " + service_pid
            ),
            timeout=20,
        )
        for _attempt in range(20):
            current = machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")).strip()
            if current != before:
                break
            machine.sleep(1)
        else:
            raise AssertionError("cycle timer did not advance after the GUI closed")
        machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -e '.cycle_enabled == true'"))

    with subtest("the socket switches the active playlist"):
        assert ctl("playlist-use night") == "playing night-grid"
        assert machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .still")).strip().endswith("night-grid.png")
        assert ctl("playlist-use day").startswith("playing day-")
        assert not machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .still")).strip().endswith("night-grid.png")

    with subtest("a timer observes a schedule boundary on an injected clock"):
        ctl("open schedules")
        machine.wait_for_file("${runtimeDir}/wall-in-one.sock")
        machine.succeed("date -s '2031-01-06 11:00:00'")
        ctl("schedule-follow")
        assert not machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .still")).strip().endswith("night-grid.png")
        assert ctl("schedule-add night from=12:00 to=13:00").startswith(
            "Night samples scheduled:"
        )
        machine.succeed("date -s '2031-01-06 12:00:00'")
        machine.wait_until_succeeds(
            as_user("${app} ctl status | ${lib.getExe pkgs.jq} -e '.still | endswith(\"night-grid.png\")'"),
            timeout=70,
        )
        listing = ctl("playlists")
        assert "Night samples\t1\tyes" in listing, listing

    with subtest("the desktop session stayed healthy"):
        machine.succeed("kill -0 " + service_pid)
        machine.fail("coredumpctl --json=short | grep -E 'wall-in-one|niri|noctalia'")
  '';
}
