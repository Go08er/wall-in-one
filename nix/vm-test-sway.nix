# The second compositor: Noctalia 5 on headless Sway, with no niri installed.
#
# vm-test boots niri. This boots the same machine (vm-base.nix: the app
# installed through programs.wall-in-one, its renderers by default, the
# companion, the seeded profile) on Sway instead, and checks what depends on
# the compositor: that the session's display reaches the user unit, that the
# service publishes Sway's outputs, and that wlr-output-management's switched
# off heads and hot-plug behave there as docs/display-discovery.md says.
#
# The renderers that show something are stand-ins, as in vm-test: a program
# that records its command line and waits, put in runtime.toml in place of the
# installed one. The scene is a playlist added to runtime.toml by hand, since
# authoring one needs a Steam Workshop tree and a still that
# linux-wallpaperengine renders.
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
  mediaDir = "${home}/Pictures/Wallpapers";
  runtimeDir = "/run/user/${uid}";
  stateDir = "${home}/.local/state/wall-in-one";
  runtimeDocument = "${stateDir}/runtime.toml";
  app = "${wallInOnePackage}/bin/wall-in-one";
  driverLog = "/tmp/wall-in-one-wallpaper-set.log";
  mpvpaperLog = "/tmp/mpvpaper-vm.log";
  sceneLog = "/tmp/linux-wallpaperengine-vm.log";

  # The system profile's `noctalia`, as in vm-test: it records each
  # wallpaper-set and who asked, then runs the real one.
  noctaliaProbe = pkgs.writeShellScriptBin "noctalia" ''
    if [ "$#" -ge 2 ] && [ "$1" = msg ] && [ "$2" = wallpaper-set ]; then
      printf '%s\t%s\n' "$PPID" "$*" >> ${driverLog}
    fi
    exec ${lib.getExe pkgs.noctalia} "$@"
  '';

  # vm-test's fake mpvpaper, built here: its pid, its parent and its
  # arguments, one line per start; it runs until it is stopped.
  fakeRenderer =
    name: log:
    pkgs.writeShellScript "wall-in-one-vm-fake-${name}" ''
      printf '%s\t%s\t%s\n' "$$" "$PPID" "$*" >> ${log}
      trap 'exit 0' TERM INT
      while :; do ${pkgs.coreutils}/bin/sleep 1; done
    '';
  fakeMpvpaper = fakeRenderer "mpvpaper" mpvpaperLog;
  fakeScene = fakeRenderer "linux-wallpaperengine" sceneLog;

  # One scene, in the shape the compiler writes (tests/golden's runtime.toml).
  # Its still is a seeded image, so applying it succeeds.
  scenePlaylist = pkgs.writeText "wall-in-one-vm-scene-playlist.toml" ''

    [[playlists]]
    id = "scene"
    name = "Scene samples"

    [[playlists.entries]]
    id = "scene-bars"
    kind = "scene"
    still = "${mediaDir}/colour-bars.png"
    scene_id = "2910000004"
    palette = { kind = "keep", mode = "keep" }
  '';

  # Independent mode: each display its own route. It needs a colour source,
  # which HEADLESS-1, never switched off, is.
  independentSettings = pkgs.writeText "wall-in-one-vm-independent.toml" ''
    display_mode = "independent"
    theme_source_connector = "HEADLESS-1"
  '';

  # The display the test switches off and on plays the video playlist.
  displaysStore = pkgs.writeText "wall-in-one-vm-displays.json" (
    builtins.toJSON {
      version = 2;
      displays."HEADLESS-3" = "video";
    }
  );
in
pkgs.testers.runNixOSTest {
  name = "wall-in-one-sway-vm";
  globalTimeout = 900;

  node.specialArgs = {
    inherit
      wallInOnePackage
      pluginSource
      sampleMedia
      noctaliaProbe
      ;
  };

  nodes.machine =
    { ... }:
    {
      imports = [ ./vm-base.nix ];
      wallInOneVm.compositor = "sway";

      virtualisation = {
        cores = 2;
        memorySize = 3072;
      };
    };

  testScript = ''
    import json
    import re
    import shlex
    import time

    user_environment = (
        "HOME=${home} "
        "USER=${user} "
        "XDG_CONFIG_HOME=${home}/.config "
        "XDG_STATE_HOME=${home}/.local/state "
        "XDG_CACHE_HOME=${home}/.cache "
        "XDG_DATA_HOME=${home}/.local/share "
        "XDG_RUNTIME_DIR=${runtimeDir} "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
        "XDG_DATA_DIRS=/run/current-system/sw/share "
        "LANG=C.UTF-8 "
        "PATH=/run/current-system/sw/bin"
    )

    def as_user(command: str) -> str:
        return f"runuser -u ${user} -- env -i {user_environment} {command}"

    def ctl(arguments: str) -> str:
        return machine.succeed(as_user("${app} ctl " + arguments)).strip()

    def status_matches(predicate: str) -> str:
        # As in vm-test: only jq's literal `true` passes, so an empty or
        # broken reply can never turn an assertion green.
        return as_user(
            "${app} ctl status | ${lib.getExe pkgs.jq} -e "
            + shlex.quote(predicate)
            + " | grep -Fx true"
        )

    def unit_pid(unit: str) -> str:
        pid = machine.succeed(
            as_user(f"systemctl --user show -p MainPID --value {unit}")
        ).strip()
        assert pid.isdigit() and int(pid) > 1, (unit, pid)
        return pid

    def environment_of(pid: str) -> list[str]:
        return machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ").splitlines()

    def swaymsg(arguments: str) -> str:
        socket = machine.succeed("ls ${runtimeDir}/sway-ipc.*.sock").split()
        assert len(socket) == 1, socket
        return machine.succeed(
            as_user(f"SWAYSOCK={socket[0]} ${pkgs.sway}/bin/swaymsg {arguments}")
        )

    def starts(log: str) -> list[tuple[str, str, str]]:
        """Each start a fake renderer recorded: (pid, parent, arguments)."""
        code, output = machine.execute(f"cat {log}")
        if code != 0:
            return []
        recorded = []
        for line in output.splitlines():
            pid, parent, arguments = line.split("\t", 2)
            recorded.append((pid, parent, arguments))
        return recorded

    def wait_starts(log: str, count: int) -> list[tuple[str, str, str]]:
        machine.wait_until_succeeds(
            f"test -e {log} && test $(wc -l < {log}) -ge {count}", timeout=20
        )
        recorded = starts(log)
        assert len(recorded) == count, (count, recorded)
        return recorded

    def screen_roots(arguments: str) -> list[str]:
        return re.findall(r"--screen-root (\S+)", arguments)

    def applications(output: str) -> list[str]:
        """The wallpaper-set lines the probe recorded for one connector."""
        code, log = machine.execute("cat ${driverLog}")
        assert code == 0, log
        return [line for line in log.splitlines()
                if line.split("\t", 1)[1].startswith(f"msg wallpaper-set {output} ")]

    # The service asks the compositor every five seconds. A change shows up
    # in status within one interval, plus the three seconds a question may
    # take at most, plus the time this loop takes to read status again.
    hotplug_bound = 10.0
    latencies: list[tuple[str, float]] = []

    def show_status() -> None:
        state = json.loads(ctl("status"))
        print("VM_SWAY_STATUS", json.dumps({
            "display_mode": state["display_mode"],
            "output_discovery_error": state["output_discovery_error"],
            "outputs": [(o["name"], o["enabled"]) for o in state["outputs"]],
            "displays": [{key: row[key] for key in (
                "connector", "connected", "assignment_source", "assigned_playlist_id",
                "playlist_id", "entry_id", "motion_active", "cycle_enabled", "last_error",
            )} for row in state["displays"]],
        }))

    def wait_for_status(predicate: str, timeout: int) -> None:
        try:
            machine.wait_until_succeeds(status_matches(predicate), timeout=timeout)
        except Exception:
            show_status()
            raise

    def wait_for_hotplug(what: str, predicate: str) -> None:
        started = time.monotonic()
        wait_for_status(predicate, timeout=30)
        elapsed = time.monotonic() - started
        latencies.append((what, elapsed))
        print(f"VM_SWAY_HOTPLUG {what}: {elapsed:.1f}s")
        assert elapsed <= hotplug_bound, (what, elapsed)

    def enabled_outputs(names: list[str]) -> str:
        return "([.outputs[] | select(.enabled) | .name]) == " + json.dumps(names)

    def disabled_outputs(names: list[str]) -> str:
        return "([.outputs[] | select(.enabled | not) | .name]) == " + json.dumps(names)

    all_three = ["HEADLESS-1", "HEADLESS-2", "HEADLESS-3"]
    two = ["HEADLESS-1", "HEADLESS-2"]

    start_all()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("wall-in-one-vm-sway.service")
    machine.wait_until_succeeds("ls ${runtimeDir}/sway-ipc.*.sock", timeout=60)
    machine.wait_until_succeeds(
        as_user("systemctl --user show-environment | grep -q '^WAYLAND_DISPLAY='"),
        timeout=60,
    )
    manager = machine.succeed(as_user("systemctl --user show-environment")).splitlines()
    imported = [line for line in manager if line.startswith("WAYLAND_DISPLAY=")]
    assert len(imported) == 1, manager
    display = imported[0].removeprefix("WAYLAND_DISPLAY=")
    # From here on the test's own commands (noctalia msg, grim) reach Sway too.
    user_environment += f" WAYLAND_DISPLAY={display}"
    machine.wait_until_succeeds(as_user("systemctl --user is-active wall-in-one.service"), timeout=60)
    machine.wait_until_succeeds(as_user("systemctl --user is-active noctalia.service"), timeout=60)
    machine.wait_for_file("${runtimeDir}/wall-in-one-runtime.sock")
    machine.wait_until_succeeds(
        status_matches('.playlist_id == "day" and .last_error == ""'), timeout=60
    )
    service_pid = unit_pid("wall-in-one.service")

    with subtest("Sway's config puts WAYLAND_DISPLAY in the user unit's environment"):
        # Sway is the only compositor here, so the one socket is its own.
        sockets = machine.succeed(
            "find ${runtimeDir} -maxdepth 1 -type s -name 'wayland-*' -printf '%f\\n'"
        ).split()
        assert sockets == [display], (sockets, display)
        # It got there by the import in the VM's Sway config, which also
        # starts the session target the units hang off.
        sway_unit = machine.succeed("systemctl cat wall-in-one-vm-sway.service")
        config_argument = re.search(r"--config (\S+)", sway_unit)
        assert config_argument is not None, sway_unit
        sway_config = config_argument.group(1)
        machine.succeed(
            "grep -Fx "
            + shlex.quote(
                'exec "systemctl --user import-environment WAYLAND_DISPLAY; '
                'systemctl --user start sway-session.target"'
            )
            + " " + sway_config
        )
        machine.succeed(
            as_user("systemctl --user is-active sway-session.target graphical-session.target")
        )
        # The unit's own process, and Noctalia's, started with it.
        for unit in ("wall-in-one.service", "noctalia.service"):
            environment = environment_of(unit_pid(unit))
            assert f"WAYLAND_DISPLAY={display}" in environment, (unit, environment)
            assert not any(v.startswith("NIRI_SOCKET=") for v in environment), environment
        # No niri anywhere for the app to lean on.
        machine.fail("test -e /run/current-system/sw/bin/niri")
        machine.fail(as_user("sh -c 'command -v niri'"))

    with subtest("the app and its renderers are installed through the module"):
        installed = machine.succeed(as_user("systemctl --user cat wall-in-one.service"))
        assert "${wallInOnePackage}/share/systemd/user/wall-in-one.service" in installed, installed
        # ExecStartPre compiled the seeded v1 document from the stores.
        machine.succeed("grep -Fx 'schema_version = 4' ${runtimeDocument}")
        for key, name in (
            ("noctalia_program", "noctalia"),
            ("mpvpaper_program", "mpvpaper"),
            ("linux_wallpaperengine_program", "linux-wallpaperengine"),
        ):
            program = f"/run/current-system/sw/bin/{name}"
            machine.succeed(f"grep -Fx '{key} = \"{program}\"' ${runtimeDocument}")
            machine.succeed(f"test -x {program}")
        for name in ("mpvpaper", "linux-wallpaperengine"):
            resolved = machine.succeed(f"readlink -f /run/current-system/sw/bin/{name}").strip()
            assert f"-{name}-" in resolved, resolved
            assert not resolved.startswith("${wallInOnePackage}"), resolved
        machine.succeed(status_matches('.install_kind == "nix"'))

    with subtest("Noctalia 5 runs on Sway and the service drives it"):
        assert int("${lib.versions.major pkgs.noctalia.version}") >= 5
        machine.wait_until_succeeds(
            as_user("${lib.getExe pkgs.noctalia} msg color-scheme-get"), timeout=60
        )
        for loaded in (
            "loaded plugin 'goober/wall-in-one'",
            "started service 'goober/wall-in-one:control'",
        ):
            machine.wait_until_succeeds(
                "journalctl -b _SYSTEMD_USER_UNIT=noctalia.service --no-pager "
                "| grep -F " + shlex.quote(loaded),
                timeout=60,
            )
        machine.wait_for_file("${driverLog}")
        applied = machine.succeed("cat ${driverLog}").splitlines()
        assert applied, applied
        for application in applied:
            parent, command = application.split("\t", 1)
            assert parent == service_pid, (parent, service_pid, command)
            assert command.startswith("msg wallpaper-set "), command
        machine.sleep(2)
        machine.succeed(
            as_user("${lib.getExe pkgs.grim} -o HEADLESS-1 /tmp/sway-desktop.png")
        )
        machine.succeed("test $(stat -c %s /tmp/sway-desktop.png) -gt 1000")
        machine.copy_from_machine("/tmp/sway-desktop.png")

    with subtest("status publishes Sway's output"):
        machine.succeed(
            status_matches(
                '.output_discovery_error == "" '
                + "and " + enabled_outputs(["HEADLESS-1"]) + " "
                + "and " + disabled_outputs([]) + " "
                + "and (.outputs[0] | .mode_width == 1280 and .mode_height == 720 "
                + "and .width > 0 and .height > 0) "
                + 'and .display_mode == "mirrored" and (.displays | length) == 1 '
                + 'and .displays[0].connector == "ALL" and .displays[0].connected == true'
            )
        )
        # Nothing below may advance a wallpaper on its own.
        assert ctl("cycle off") == "cycle off (manual)"

    with subtest("two outputs added with swaymsg are published"):
        swaymsg("create_output")
        swaymsg("create_output")
        outputs = json.loads(swaymsg("-r -t get_outputs"))
        assert sorted(o["name"] for o in outputs) == all_three, outputs
        assert all(o["active"] for o in outputs), outputs
        wait_for_hotplug(
            "two outputs added",
            '.output_discovery_error == "" and ' + enabled_outputs(all_three)
            + " and ([.outputs[] | .mode_width > 0 and .width > 0] | all)",
        )

    with subtest("an all-output scene covers exactly the enabled outputs"):
        machine.succeed(as_user(
            "sed -i "
            "-e 's|^mpvpaper_program = .*|mpvpaper_program = \"${fakeMpvpaper}\"|' "
            "-e 's|^linux_wallpaperengine_program = .*|linux_wallpaperengine_program = \"${fakeScene}\"|' "
            "${runtimeDocument}"
        ))
        machine.succeed(as_user(
            "sh -c 'cat ${runtimeDocument} ${scenePlaylist} > ${runtimeDocument}.vm "
            "&& mv ${runtimeDocument}.vm ${runtimeDocument}'"
        ))
        assert "reloaded" in ctl("reload")
        loaded = machine.succeed("sha256sum ${runtimeDocument}").split()[0]
        machine.succeed(status_matches(f'.loaded_config_sha256 == "{loaded}"'))
        assert ctl("playlist-use scene") == "playing scene-bars"
        [(first, parent, arguments)] = wait_starts("${sceneLog}", 1)
        assert parent == service_pid, (parent, service_pid)
        assert screen_roots(arguments) == all_three, arguments
        assert "--bg 2910000004" in arguments, arguments

        # Switched off: the head stays listed, and the scene is started again
        # on the two that are left.
        swaymsg("output HEADLESS-3 disable")
        wait_for_hotplug(
            "scene: HEADLESS-3 switched off",
            enabled_outputs(two) + " and " + disabled_outputs(["HEADLESS-3"])
            + ' and .output_discovery_error == ""',
        )
        [_, (second, parent, arguments)] = wait_starts("${sceneLog}", 2)
        assert parent == service_pid, (parent, service_pid)
        assert screen_roots(arguments) == two, arguments
        machine.wait_until_succeeds(f"! kill -0 {first}", timeout=10)
        machine.succeed(f"kill -0 {second}")
        machine.succeed(
            status_matches('.displays[0].connector == "ALL" and .displays[0].connected == true')
        )
        # Two more probes find nothing new, so nothing more is started.
        machine.sleep(11)
        assert len(starts("${sceneLog}")) == 2, starts("${sceneLog}")

        # Switched back on: all three again.
        swaymsg("output HEADLESS-3 enable")
        wait_for_hotplug(
            "scene: HEADLESS-3 switched on",
            enabled_outputs(all_three) + " and " + disabled_outputs([]),
        )
        [_, _, (third, parent, arguments)] = wait_starts("${sceneLog}", 3)
        assert parent == service_pid, (parent, service_pid)
        assert screen_roots(arguments) == all_three, arguments
        machine.wait_until_succeeds(f"! kill -0 {second}", timeout=10)

        assert ctl("schedule-follow").startswith("playing day-")
        machine.wait_until_succeeds(f"! kill -0 {third}", timeout=10)
        machine.fail("test -e ${mpvpaperLog}")

    with subtest("a switched-off display is connected: false and gets no renderer"):
        # Off before the compiler writes a document that assigns it a video,
        # so the installed mpvpaper is never started on it.
        swaymsg("output HEADLESS-3 disable")
        wait_for_hotplug("HEADLESS-3 switched off", disabled_outputs(["HEADLESS-3"]))
        settings = "${home}/.config/wall-in-one/settings.toml"
        machine.fail(f"grep -E '^(display_mode|theme_source_connector) ' {settings}")
        machine.succeed(as_user(f"sh -c 'cat ${independentSettings} >> {settings}'"))
        # Off in the settings, so a route made later starts with it off too;
        # the session's `cycle off` holds only the routes there are.
        machine.succeed(as_user(
            f"sed -i 's/^cycle_enabled = true$/cycle_enabled = false/' {settings}"
        ))
        machine.succeed(f"grep -Fx 'cycle_enabled = false' {settings}")
        machine.succeed(as_user("install -m 0644 ${displaysStore} ${stateDir}/displays.json"))
        machine.succeed(as_user("${app} --write-config"))
        machine.succeed("grep -Fx 'display_mode = \"independent\"' ${runtimeDocument}")
        machine.succeed("grep -Fx 'theme_source_connector = \"HEADLESS-1\"' ${runtimeDocument}")
        machine.succeed("grep -Fx 'connector = \"HEADLESS-3\"' ${runtimeDocument}")
        machine.succeed("grep -Fx 'cycle_enabled = false' ${runtimeDocument}")
        machine.succeed(as_user(
            "sed -i "
            "-e 's|^mpvpaper_program = .*|mpvpaper_program = \"${fakeMpvpaper}\"|' "
            "-e 's|^linux_wallpaperengine_program = .*|linux_wallpaperengine_program = \"${fakeScene}\"|' "
            "${runtimeDocument}"
        ))
        assert "reloaded" in ctl("reload")
        loaded = machine.succeed("sha256sum ${runtimeDocument}").split()[0]
        machine.succeed(status_matches(f'.loaded_config_sha256 == "{loaded}"'))

        def rows(expected: list[tuple[str, bool]]) -> str:
            listed = [{"connector": name, "connected": up} for name, up in expected]
            return (
                '.display_mode == "independent" '
                "and ([.displays[] | {connector, connected}] | sort_by(.connector)) == "
                + json.dumps(listed)
            )

        live = [("HEADLESS-1", True), ("HEADLESS-2", True)]
        detached = live + [("HEADLESS-3", False)]
        not_rendered = (
            'all(.displays[]; .connector != "HEADLESS-3" '
            "or (.connected == false and .motion_active == false)) "
            "and all(.displays[]; .cycle_enabled == false) "
            "and " + disabled_outputs(["HEADLESS-3"])
        )
        off = (
            rows(detached)
            + ' and any(.displays[]; .connector == "HEADLESS-3" '
            'and .assignment_source == "explicit" and .assigned_playlist_id == "video") '
            "and " + not_rendered
        )
        # The service makes a display's route when it first sees the display
        # connected in independent mode. Off since before then, the assignment
        # may have no row yet, although status documents one with
        # connected false; either way nothing may be put on it.
        never_seen = "(" + rows(live) + " or " + rows(detached) + ") and " + not_rendered
        wait_for_status(never_seen, timeout=20)
        state = json.loads(ctl("status"))
        if not any(row["connector"] == "HEADLESS-3" for row in state["displays"]):
            print("VM_SWAY_NOTE HEADLESS-3's assignment has no displays row while it has "
                  "not been connected in independent mode")
        # Two probes later, still nothing was started on it.
        machine.sleep(11)
        machine.succeed(status_matches(never_seen))
        machine.fail("test -e ${mpvpaperLog}")
        assert applications("HEADLESS-3") == [], applications("HEADLESS-3")

        # Switched on: its assignment is applied again, and only there.
        survivors = (applications("HEADLESS-1"), applications("HEADLESS-2"))
        swaymsg("output HEADLESS-3 enable")
        wait_for_hotplug(
            "HEADLESS-3 switched on",
            'any(.displays[]; .connector == "HEADLESS-3" and .connected == true '
            'and .playlist_id == "video" and .entry_id == "video-grid" '
            "and .motion_active == true)",
        )
        [(video, parent, arguments)] = wait_starts("${mpvpaperLog}", 1)
        assert parent == service_pid, (parent, service_pid)
        assert arguments.endswith(" HEADLESS-3 ${mediaDir}/moving-grid.mp4"), arguments
        machine.succeed(f"kill -0 {video}")
        assert len(applications("HEADLESS-3")) == 1, applications("HEADLESS-3")
        assert (applications("HEADLESS-1"), applications("HEADLESS-2")) == survivors

        # Switched off again: its renderer stops, and none starts.
        swaymsg("output HEADLESS-3 disable")
        wait_for_hotplug("HEADLESS-3 switched off again", off)
        machine.wait_until_succeeds(f"! kill -0 {video}", timeout=10)
        machine.sleep(11)
        machine.succeed(status_matches(off))
        assert len(starts("${mpvpaperLog}")) == 1, starts("${mpvpaperLog}")
        assert len(applications("HEADLESS-3")) == 1, applications("HEADLESS-3")

    with subtest("the session stayed healthy"):
        assert unit_pid("wall-in-one.service") == service_pid
        assert machine.succeed("pgrep -u ${uid} -x wall-in-one-ser").split() == [service_pid]
        machine.fail("coredumpctl --json=short | grep -E 'wall-in-one|sway|noctalia'")
        machine.fail(
            "journalctl -b --no-pager "
            "| grep -E 'Gtk-CRITICAL|GLib-GObject-CRITICAL|Traceback|thread .* panicked'"
        )
        for what, elapsed in latencies:
            print(f"VM_SWAY_HOTPLUG_SUMMARY {what}: {elapsed:.1f}s")
  '';
}
