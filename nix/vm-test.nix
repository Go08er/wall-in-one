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
  driverLog = "/tmp/wall-in-one-wallpaper-set.log";
  noctaliaProbe = pkgs.writeShellScriptBin "noctalia" ''
    if [ "$#" -ge 2 ] && [ "$1" = msg ] && [ "$2" = wallpaper-set ]; then
      printf '%s\t%s\n' "$PPID" "$*" >> ${driverLog}
    fi
    exec ${lib.getExe pkgs.noctalia} "$@"
  '';
in
pkgs.testers.runNixOSTest {
  name = "wall-in-one-desktop-vm";
  globalTimeout = 1200;

  node.specialArgs = {
    inherit wallInOnePackage pluginSource sampleMedia noctaliaProbe;
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
        "PATH=${noctaliaProbe}/bin:/run/current-system/sw/bin"
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

    with subtest("a v1 runtime config is regenerated headlessly during upgrade"):
        machine.succeed(
            "grep -Fx 'schema_version = 2' ${home}/.local/state/wall-in-one/runtime.toml"
        )
        machine.fail("grep -F 'upgrade_fixture' ${home}/.local/state/wall-in-one/runtime.toml")
        assert "dev.goober.WallInOne" not in niri("windows")
        machine.wait_until_succeeds(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.playlist_id == \"day\" and .last_error == \"\"'"
            ),
            timeout=20,
        )
        machine.wait_for_file("${driverLog}")
        upgrade_pid = machine.succeed(
            as_user("systemctl --user show -p MainPID --value wall-in-one.service")
        ).strip()
        applications = machine.succeed("cat ${driverLog}").splitlines()
        assert applications, applications
        for application in applications:
            parent, command = application.split("\t", 1)
            assert parent == upgrade_pid, (parent, upgrade_pid, command)
            assert command.startswith("msg wallpaper-set "), command

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

    with subtest("runtime cycle and stop controls remain independent"):
        before = machine.succeed(
            as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")
        ).strip()
        assert ctl("cycle off") == "cycle off (manual)"
        machine.sleep(7)
        held = machine.succeed(
            as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")
        ).strip()
        assert held == before, (before, held)
        machine.succeed(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.cycle_enabled == false and .cycle_default == true "
                "and .cycle_source == \"manual\" and .playback_state == \"playing\"'"
            )
        )
        assert ctl("stop") == "stopped; paired still remains active"
        machine.succeed(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.playback_state == \"stopped\" and .stopped == true "
                "and .paused == false and .motion_active == false'"
            )
        )
        assert ctl("play") == "playing"
        assert ctl("cycle default") == "cycle on (config)"
        machine.succeed(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.playback_state == \"playing\" and .cycle_enabled == true "
                "and .cycle_source == \"config\"'"
            )
        )

    with subtest("every workflow page renders"):
        for page in ("browse", "media", "playlists", "schedules", "settings"):
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
        machine.succeed("rm -f ${driverLog}")
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
        machine.wait_for_file("${driverLog}")
        applications = machine.succeed("cat ${driverLog}").splitlines()
        assert len(applications) == 1, applications
        parent, command = applications[0].split("\t", 1)
        assert parent == service_pid, (parent, service_pid, command)
        assert command.startswith("msg wallpaper-set "), command

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
