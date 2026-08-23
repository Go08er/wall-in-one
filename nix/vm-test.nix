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
            "grep -Fx 'schema_version = 4' ${home}/.local/state/wall-in-one/runtime.toml"
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

    with subtest("corrupt authoring keeps a valid last-known-good runtime alive"):
        playlist_store = "${home}/.local/state/wall-in-one/playlists.json"
        runtime_document = "${home}/.local/state/wall-in-one/runtime.toml"
        backup = "/tmp/wall-in-one-vm-playlists-good.json"
        before = machine.succeed(f"sha256sum {runtime_document}").split()[0]
        machine.succeed(as_user(f"cp {playlist_store} {backup}"))
        machine.succeed(as_user(f"printf '{{ broken' > {playlist_store}"))
        machine.succeed(as_user("systemctl --user restart wall-in-one.service"))
        machine.wait_until_succeeds(
            as_user("systemctl --user is-active wall-in-one.service"), timeout=30
        )
        machine.wait_for_file("${runtimeDir}/wall-in-one-runtime.sock")
        machine.wait_until_succeeds(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.playlist_id == \"day\" and .last_error == \"\"'"
            ),
            timeout=20,
        )
        after = machine.succeed(f"sha256sum {runtime_document}").split()[0]
        assert after == before, (before, after)
        machine.succeed(
            "journalctl -b _SYSTEMD_USER_UNIT=wall-in-one.service --no-pager "
            "| grep -F 'playlists.json' | grep -F 'left untouched'"
        )

        # Restore authoring state for the remainder of the interactive test and
        # prove the same compiler can recover without touching the GUI.
        machine.succeed(as_user(f"mv {backup} {playlist_store}"))
        machine.succeed(as_user("${app} --write-config"))
        machine.succeed(as_user("${app} ctl reload"))

    with subtest("companion plugin loads from an isolated path source"):
        # Deliberately not asserting the entry count. It used to insist on
        # "(4 entries)", which broke the moment the plugin dropped its Control
        # Center shortcut in another repository -- a change this repository has
        # no say in and no reason to track. An exact count here means every
        # plugin restructure fails the app's own test suite for no defect.
        #
        # What is worth pinning is that the plugin loads at all, and that the
        # one entry the app actually depends on comes up: the singleton service
        # is what drives `wall-in-one ctl`, so a widget or panel may come and go
        # but this must not.
        machine.wait_until_succeeds(
            "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
            "| grep -F \"loaded plugin 'goober/wall-in-one'\"",
            timeout=60,
        )
        machine.wait_until_succeeds(
            "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
            "| grep -F \"started service 'goober/wall-in-one:control'\"",
            timeout=60,
        )

    with subtest("nested niri owns a visible output"):
        assert "winit" in niri("outputs")
        machine.wait_until_succeeds(
            "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
            "| grep -F 'outputs=1'",
            timeout=30,
        )
        machine.wait_until_succeeds(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} -e "
                "'.status_version == 2 and .display_mode == \"mirrored\" "
                "and (.displays | length) == 1 "
                # Mirrored mode deliberately retains one efficient mpvpaper ALL
                # route. The niri and Noctalia assertions above prove that this
                # VM's physical connector is winit; the empty discovery error
                # proves the runtime saw that live snapshot too.
                "and .displays[0].connector == \"ALL\" "
                "and .displays[0].connected == true "
                "and .output_discovery_error == \"\" "
                "and .theme_source.configured == \"\" "
                "and .theme_source.effective == null'"
            ),
            timeout=20,
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

    with subtest("packaged video renderer freezes, releases, and resumes"):
        machine.succeed(
            "printf '%s\\n' '#!/bin/sh' "
            "'printf \"%s\\t%s\\t%s\\n\" \"$$\" \"$PPID\" \"$*\" >> /tmp/mpvpaper-vm.log' "
            "'trap \"exit 0\" TERM INT' "
            "'while :; do sleep 1; done' "
            "> /tmp/fake-mpvpaper"
        )
        machine.succeed("chmod 0755 /tmp/fake-mpvpaper")
        machine.succeed(
            "sed -i 's|^mpvpaper_program = .*|mpvpaper_program = \"/tmp/fake-mpvpaper\"|' "
            "${home}/.local/state/wall-in-one/runtime.toml"
        )
        assert "reloaded" in ctl("reload")
        assert "video-grid" in ctl("playlist-use video")
        machine.wait_for_file("/tmp/mpvpaper-vm.log")
        machine.wait_until_succeeds("test $(wc -l < /tmp/mpvpaper-vm.log) -eq 1", timeout=20)
        machine.succeed(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.kind == \"video\" and .motion_active == true and .last_error == \"\"'"
            )
        )
        service_pid = machine.succeed(
            as_user("systemctl --user show -p MainPID --value wall-in-one.service")
        ).strip()
        first_child = machine.succeed(
            "pgrep -P " + service_pid + " -f /tmp/fake-mpvpaper"
        ).splitlines()[0]
        arguments = machine.succeed(
            "tr '\\0' ' ' < /proc/" + first_child + "/cmdline"
        ).strip()
        assert "moving-grid.mp4" in arguments, arguments
        assert "hwdec=no" in arguments, arguments
        assert "video-sync=display-resample" in arguments, arguments
        assert "interpolation=yes" in arguments, arguments
        assert "display-fps-override=" in arguments, arguments
        assert "tscale=oversample" in arguments, arguments

        assert ctl("pause") == "paused"
        machine.wait_until_succeeds(
            "grep -Eq '^State:[[:space:]]+T' /proc/" + first_child + "/status",
            timeout=10,
        )
        assert ctl("stop") == "stopped; paired still remains active"
        machine.wait_until_succeeds("! kill -0 " + first_child, timeout=10)
        machine.succeed(
            as_user(
                "${app} ctl status | ${lib.getExe pkgs.jq} "
                "-e '.playback_state == \"stopped\" and .motion_active == false'"
            )
        )
        assert ctl("play") == "playing"
        machine.wait_until_succeeds("test $(wc -l < /tmp/mpvpaper-vm.log) -eq 2", timeout=20)
        second_child = machine.succeed(
            "pgrep -P " + service_pid + " -f /tmp/fake-mpvpaper"
        ).splitlines()[0]
        assert second_child != first_child, (first_child, second_child)
        machine.succeed("kill -0 " + second_child)
        assert ctl("playlist-use day").startswith("playing day-")
        machine.wait_until_succeeds("! kill -0 " + second_child, timeout=10)

    with subtest("every workflow page renders"):
        # Begin the double-driver observation before the GUI exists. Holding
        # the timer keeps the screenshots deterministic; any wallpaper apply
        # during page launch/navigation would therefore be a GUI-side driver,
        # and the parent-PID audit below will reject it.
        assert ctl("cycle off") == "cycle off (manual)"
        machine.succeed("rm -f ${driverLog}")
        page_hashes = {}
        page_titles = {
            "browse": "Browse",
            "media": "Media/Pairings",
            "playlists": "Playlists",
            "schedules": "Schedules",
            "settings": "Settings",
        }
        for page, title in page_titles.items():
            expected = (
                f"launch requested for {page}" if page == "browse" else f"opened {page}"
            )
            assert ctl(f"open {page}") == expected
            machine.wait_until_succeeds(
                as_user(
                    "NIRI_SOCKET=$(ls ${runtimeDir}/niri*.sock | head -n1) "
                    "${lib.getExe pkgs.niri} msg windows "
                    "| grep -F 'dev.goober.WallInOne'"
                ),
                timeout=30,
            )
            # Prove the requested ViewStack child actually became visible.
            # Hashing five whole desktops was not enough: Noctalia's changing
            # clock could make five captures unique even if the application
            # stayed stuck on one page.
            machine.wait_until_succeeds(
                as_user(
                    "NIRI_SOCKET=$(ls ${runtimeDir}/niri*.sock | head -n1) "
                    "${lib.getExe pkgs.niri} msg windows "
                    f"| grep -F 'Wall-in-One - {title}'"
                ),
                timeout=30,
            )
            machine.sleep(2)
            full_capture = f"/tmp/wall-in-one-{page}-desktop.png"
            capture = f"/tmp/wall-in-one-{page}.png"
            machine.succeed(as_user(f"${lib.getExe pkgs.grim} {full_capture}"))
            # The app is opened maximized by vm-base.nix.  Drop the top 64
            # pixels occupied by the shell panel, including its clock, before
            # comparing pages.  Keep machine.screenshot below as the complete
            # human-review artifact.
            machine.succeed(
                f"${lib.getExe pkgs.ffmpeg} -hide_banner -loglevel error -y "
                f"-i {full_capture} -vf 'crop=iw:ih-64:0:64' {capture}"
            )
            page_hashes[page] = machine.succeed(f"sha256sum {capture}").split()[0]
            machine.screenshot(f"wall-in-one-{page}")
        assert len(set(page_hashes.values())) == len(page_hashes), page_hashes

    with subtest("closing the GUI does not stop service rotation"):
        ctl("playlist-use day")
        before = machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")).strip()
        applications_before_close = machine.succeed(
            "test ! -e ${driverLog} || cat ${driverLog}"
        ).splitlines()
        niri("action close-window")
        machine.wait_until_succeeds(
            as_user(
                "systemctl --user show -p MainPID --value wall-in-one.service "
                "| grep -Fx " + service_pid
            ),
            timeout=20,
        )
        assert ctl("cycle default") == "cycle on (config)"
        for _attempt in range(20):
            current = machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -r .entry_id")).strip()
            if current != before:
                break
            machine.sleep(1)
        else:
            raise AssertionError("cycle timer did not advance after the GUI closed")
        # Stop the next deadline before inspecting the log: the one observed
        # cursor change must correspond to exactly one wallpaper application.
        assert ctl("cycle off") == "cycle off (manual)"
        machine.succeed(as_user("${app} ctl status | ${lib.getExe pkgs.jq} -e '.cycle_enabled == false'"))
        machine.wait_for_file("${driverLog}")
        applications = machine.succeed("cat ${driverLog}").splitlines()
        assert len(applications) == len(applications_before_close) + 1, (
            applications_before_close,
            applications,
        )
        for application in applications:
            parent, command = application.split("\t", 1)
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
        machine.fail(
            "journalctl -b --no-pager "
            "| grep -E 'Gtk-CRITICAL|GLib-GObject-CRITICAL|Traceback|thread .* panicked'"
        )
  '';
}
