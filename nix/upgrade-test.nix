{
  pkgs,
  oldPackage,
  newPackage,
  oldPluginSource,
  newPluginSource,
  sampleMedia,
  requireSafeRestart ? true,
}:

# Explicit A/B safe-restart gate using actual release and candidate packages.
# The user chose an announced restart with saved configuration/data preserved;
# seamless session restoration and automatic downgrade are not requirements.
let
  inherit (pkgs) lib;
  home = "/home/wallpaper";
  mediaDir = "${home}/Pictures/Original Library";
  runtimeDir = "/run/user/1000";
in
pkgs.testers.runNixOSTest {
  name = "wall-in-one-upgrade-characterization";
  globalTimeout = 1200;

  node.specialArgs = {
    wallInOnePackage = oldPackage;
    pluginSource = oldPluginSource;
    noctaliaProbe = null;
    inherit sampleMedia;
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
      environment.systemPackages = [
        pkgs.python314
        # An actual external editor for the user-operated recovery journey.
        # It is a disposable VM dependency, not part of the app's closure.
        pkgs.mousepad
      ];
      # Seed non-default paths before the old release first opens the profile.
      # Only disposable guest fixture data is changed here.
      systemd.services.wall-in-one-vm-seed.script = lib.mkAfter ''
        mv ${home}/Pictures/Wallpapers ${lib.escapeShellArg mediaDir}
        ${pkgs.gnused}/bin/sed -i \
          's@${home}/Pictures/Wallpapers@${mediaDir}@g' \
          ${home}/.config/wall-in-one/settings.toml \
          ${home}/.local/state/wall-in-one/playlists.json
      '';
    };

  testScript = ''
    import json
    import shlex
    import time
    from typing import Any

    old = "${oldPackage}"
    new = "${newPackage}"
    home = "${home}"
    media_dir = "${mediaDir}"
    runtime_dir = "${runtimeDir}"
    settings = home + "/.config/wall-in-one/settings.toml"
    runtime = home + "/.local/state/wall-in-one/runtime.toml"
    profile = home + "/.local/share/upgrade-test/current"
    report: dict[str, Any] = {"old_package": old, "new_package": new, "observations": {}, "release_blockers": []}
    q = shlex.quote

    def user(command):
        environment = (
            "HOME=${home} USER=wallpaper "
            "XDG_CONFIG_HOME=${home}/.config XDG_STATE_HOME=${home}/.local/state "
            "XDG_CACHE_HOME=${home}/.cache XDG_DATA_HOME=${home}/.local/share "
            "XDG_RUNTIME_DIR=${runtimeDir} WAYLAND_DISPLAY=wayland-1 "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
            "XDG_DATA_DIRS=" + profile + "/share:/run/current-system/sw/share "
            "PATH=" + profile + "/bin:/run/current-system/sw/bin LANG=C.UTF-8"
        )
        return "runuser -u wallpaper -- env -i " + environment + " ${lib.getExe pkgs.bash} -euo pipefail -c " + q(command)

    def run(command):
        return machine.succeed(user(command)).strip()

    def ctl(package, arguments):
        return run(package + "/bin/wall-in-one ctl " + arguments)

    def status():
        result = json.loads(ctl(new, "status"))
        assert result["config_path"] == runtime, result
        return result

    def prop(name):
        return run("systemctl --user show -p " + name + " --value wall-in-one.service")

    def wait_status(predicate):
        deadline = time.monotonic() + 20
        last = None
        while time.monotonic() < deadline:
            try:
                last = status()
                if predicate(last):
                    return last
            except (AssertionError, ValueError):
                pass
            time.sleep(0.2)
        raise AssertionError(last)

    def niri(arguments):
        return run("NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) ${lib.getExe pkgs.niri} msg " + arguments)

    def windows():
        return [window for window in json.loads(niri("--json windows")) if window.get("app_id") == "dev.goober.WallInOne"]

    def wait_gui_inventory(package):
        machine.wait_until_succeeds(user(package + "/bin/wall-in-one ctl list | grep -F "
            + q(media_dir + "/night-grid.png")), timeout=45)

    def close_gui():
        for window in windows():
            niri("action close-window --id " + str(window["id"]))
            machine.wait_until_succeeds("test ! -e /proc/" + str(window["pid"]), timeout=30)
        machine.wait_until_succeeds("test ! -e ${runtimeDir}/wall-in-one.sock", timeout=30)
        assert not windows()

    def stop_runtime():
        # Quiesce the companion too: its initial status request may launch an
        # absent runtime. Stopping the daemon while Noctalia is still loading
        # does not satisfy the documented no-competing-launcher precondition.
        run("systemctl --user stop noctalia.service")
        run("systemctl --user stop wall-in-one.service")
        run("systemctl --user stop wall-in-one-health-sync.timer wall-in-one-health-sync.service")
        assert prop("MainPID") == "0"
        machine.wait_until_succeeds("test ! -e ${runtimeDir}/wall-in-one-runtime.sock", timeout=20)

    def start_runtime():
        run("systemctl --user start wall-in-one.service")
        machine.wait_for_file("${runtimeDir}/wall-in-one-runtime.sock")
        run("systemctl --user start noctalia.service")
        machine.wait_for_file("${runtimeDir}/noctalia-wayland-1.sock")

    def check_running(package):
        pid = prop("MainPID")
        assert pid.isdigit() and int(pid) > 1, pid
        executable = machine.succeed("readlink -e /proc/" + pid + "/exe").strip()
        assert executable == package + "/bin/wall-in-one-service", (pid, executable, package)
        return pid

    def check_loaded(package):
        assert "path=" + package + "/bin/wall-in-one-service " in prop("ExecStart"), prop("ExecStart")
        assert package + "/bin/wall-in-one --service-startup-prepare" in prop("ExecStartPre")
        assert package + "/bin/wall-in-one-service --check-config" in prop("ExecStartPre")
        assert package + "/bin/wall-in-one --sync-runtime-health-on-stop" in prop("ExecStop")
        health = run("systemctl --user show -p ExecStart --value wall-in-one-health-sync.service")
        assert package + "/bin/wall-in-one --sync-runtime-health" in health, health

    def package_python(package, code):
        # Exercise the installed package's strict settings writer, not a source
        # checkout or a handcrafted equivalent. Actual compiler/startup/control
        # calls below still use each package's unmodified launcher wrapper.
        # Authoring now also performs read-only Gio service inspection. Supply
        # the same declared PyGObject dependency as the packaged application;
        # do not mistake a bare interpreter's missing dependency for a safe
        # compatibility refusal. Product CLI calls still use its real wrapper.
        return run("PYTHONPATH=" + package + "/${pkgs.python314.sitePackages} ${
          pkgs.python314.withPackages (ps: [ ps.pygobject3 ])
        }/bin/python3 -c " + q(code))

    def change_settings(package, changes):
        return package_python(package, "from pathlib import Path; from wall_in_one import config; "
            + "assert str(config.__file__).startswith(" + repr(package + "/") + "); "
            + "changes = " + repr(changes) + "; "
            + "changes.update(roots=tuple(Path(root) for root in changes['roots'])) if 'roots' in changes else None; "
            + "config.update(changes)")

    def document(path):
        return json.loads(run("${pkgs.python314}/bin/python3 -c " + q(
            "import json,tomllib; from pathlib import Path; print(json.dumps(tomllib.loads(Path(" + repr(path) + ").read_text())))")))

    def snapshot():
        # Include absent authoring stores and all media/sidecars. Do not confuse
        # regenerated runtime/cache/locks or Noctalia wallpaper output with
        # authored input. No journals are seeded or removed by this test.
        code = """
    import hashlib, json
    from pathlib import Path
    result = {}
    for path in [Path('${home}/.config/wall-in-one/settings.toml')] + [
        Path('${home}/.local/state/wall-in-one') / name for name in (
            'playlists.json', 'pairings.json', 'favourites.json', 'schedules.json',
            'displays.json', 'pending-removals.json')]:
        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    root = Path('${mediaDir}')
    for path in sorted(root.rglob('*')):
        if path.is_file():
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps(result, sort_keys=True))
    """
        return json.loads(run("${pkgs.python314}/bin/python3 -c " + q(code)))

    def record(name, observed, blocker=None):
        report["observations"][name] = observed
        if blocker:
            report["release_blockers"].append(blocker)
        print("UPGRADE_OBSERVATION " + name + " " + json.dumps(observed, sort_keys=True))

    start_all()
    machine.wait_for_unit("cage-tty1.service")
    machine.wait_for_file("${runtimeDir}/bus")
    machine.wait_until_succeeds("test -S ${runtimeDir}/wayland-1", timeout=45)
    machine.wait_until_succeeds(user("systemctl --user is-active wall-in-one.service"), timeout=45)
    machine.wait_until_succeeds(user("systemctl --user is-active noctalia.service"), timeout=45)
    machine.wait_for_file("${runtimeDir}/noctalia-wayland-1.sock")
    run("mkdir -p " + q(str(profile.rsplit('/', 1)[0])))
    run("ln -s " + q(old) + " " + q(profile))

    with subtest("old release authors real data at original and temporarily missing roots"):
        check_running(old)
        old_supports_battery = 5 in status().get("supported_config_schemas", [])
        stop_runtime()
        change_settings(old, {"roots": (media_dir, home + "/Offline Library"), "cycle_enabled": False, "dynamics_enabled": False})
        # roots is a tuple of Paths in Settings; exercise the same writer with
        # those types when assigning the fixture's non-default library paths.
        run(old + "/bin/wall-in-one --service-startup-prepare")
        start_runtime()
        wait_status(lambda value: value["playlist_id"] == "day")
        ctl(old, "open media")
        machine.wait_for_file("${runtimeDir}/wall-in-one.sock")
        machine.wait_until_succeeds(user("NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) ${lib.getExe pkgs.niri} msg --json windows | ${lib.getExe pkgs.jq} -e 'any(.[]; .app_id == \"dev.goober.WallInOne\")'"), timeout=30)
        wait_gui_inventory(old)
        ctl(old, "playlist-new Retained")
        ctl(old, "playlist-add Retained " + q(media_dir + "/night-grid.png"))
        ctl(old, "playlist-add Retained " + q(media_dir + "/night-grid.png"))
        ctl(old, "favourite " + q(media_dir + "/night-grid.png"))
        ctl(old, "palette " + q(media_dir + "/moving-grid.mp4 :: keep"))
        ctl(old, "schedule-add Retained days=mon from=02:00 to=02:01")
        assert document(settings)["roots"] == [media_dir, home + "/Offline Library"]
        assert snapshot()[home + "/.local/state/wall-in-one/pairings.json"] is not None
        old_gui = windows()[0]
        old_runtime_pid = check_running(old)

    with subtest("a new desktop launcher identifies the old GUI before activation"):
        before_launch = snapshot()
        run("ln -sfn " + q(new) + " " + q(profile))
        assert run("readlink -e " + q(profile + "/bin/wall-in-one")) == new + "/bin/wall-in-one"
        # A persistent notice inherits the launcher's descriptors. Keep its
        # output in a guest-only log file instead of holding the test driver's
        # command pipe open until the user closes the newly launched window.
        run("${pkgs.glib}/bin/gio launch " + new + "/share/applications/dev.goober.WallInOne.desktop"
            + " > /tmp/wall-in-one-update-launch.log 2>&1")
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'dev.goober.WallInOne.Update'"), timeout=30)
        notices = [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") == "dev.goober.WallInOne.Update"]
        assert len(notices) == 1, notices
        assert notices[0]["title"] == "Wall-in-One update needs an app restart", notices
        assert new in machine.succeed("tr '\\0' ' ' < /proc/" + str(notices[0]["pid"]) + "/cmdline")
        run("${pkgs.glib}/bin/gio launch " + new + "/share/applications/dev.goober.WallInOne.desktop"
            + " >> /tmp/wall-in-one-update-launch.log 2>&1")
        machine.sleep(1)
        repeated = [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") == "dev.goober.WallInOne.Update"]
        assert len(repeated) == 1 and repeated[0]["pid"] == notices[0]["pid"], repeated
        current_gui = windows()
        assert len(current_gui) == 1, current_gui
        same_gui = current_gui[0]["pid"] == old_gui["pid"]
        command = machine.succeed("tr '\\0' ' ' < /proc/" + str(current_gui[0]["pid"]) + "/cmdline")
        assert old in command if same_gui else new in command, command
        check_loaded(old)
        assert check_running(old) == old_runtime_pid
        assert same_gui
        assert snapshot() == before_launch
        machine.screenshot("update-notice")
        niri("action close-window --id " + str(notices[0]["id"]))
        machine.wait_until_succeeds("test ! -e /proc/" + str(notices[0]["pid"]), timeout=30)
        record("new_desktop_launcher_with_old_gui", {"same_old_gui": same_gui,
            "loaded_and_running_runtime": old, "update_notice_shown": True,
            "authoring_and_media_bytes_unchanged": True, "explicit_restart_required": True})
        close_gui()

    with subtest("new compiler stays schema-4 compatible while battery is off"):
        before = snapshot()
        run(new + "/bin/wall-in-one --write-config")
        assert document(runtime)["schema_version"] == 4
        run(old + "/bin/wall-in-one-service --check-config --config " + q(runtime))
        # The daemon reloads a newly compiled document asynchronously. Establish
        # that generation before testing that a refused write cannot change it.
        generation = document(runtime)["config_generation"]
        wait_status(lambda value: value["config_generation"] == generation)
        assert snapshot() == before
        record("battery_off_new_compiler_old_parser", {"accepted": True, "authored_and_media_bytes_unchanged": True})

    with subtest("new writer refuses battery enable while the old runtime is active"):
        old_status = status()
        before = snapshot()
        runtime_before = run("sha256sum " + q(runtime))
        refusal = package_python(new, """
    from wall_in_one import config
    try:
        config.update({"stop_animations_on_battery": True})
    except config.ConfigError as error:
        assert "No changes were saved" in str(error), error
        print(error)
    else:
        raise AssertionError("new writer accepted battery enable under the old runtime")
    """)
        assert snapshot() == before
        assert run("sha256sum " + q(runtime)) == runtime_before
        assert document(runtime)["schema_version"] == 4
        run(old + "/bin/wall-in-one-service --check-config --config " + q(runtime))
        current = wait_status(lambda value: value["config_generation"] == old_status["config_generation"])
        assert check_running(old) == old_runtime_pid
        assert current.get("stop_animations_on_battery", False) is False
        record("battery_on_with_old_runtime", {"refused_before_save": True,
            "settings_runtime_and_authored_bytes_unchanged": True,
            "old_generation_retained": True, "error": refusal})

    with subtest("announced A-to-B restart preserves saved configuration and library data"):
        ctl(old, "playlist-use Retained")
        ctl(old, "shuffle on")
        ctl(old, "cycle off")
        ctl(old, "pause")
        paused = status()
        assert paused["playback_state"] == "paused", paused
        stop_runtime()
        before = snapshot()
        noctalia_before = run("sha256sum ${home}/.local/state/noctalia/settings.toml").split()[0]
        # Reconcile only this test-owned set of links, using the unmodified
        # packaged units. This is a manual unit-link install, not an assertion
        # about Nix profile or Home Manager activation internals.
        unit_dir = home + "/.config/systemd/user"
        run("mkdir -p " + q(unit_dir))
        for unit in ("wall-in-one.service", "wall-in-one-health-sync.service", "wall-in-one-health-sync.timer"):
            run("ln -s " + q(new + "/share/systemd/user/" + unit) + " " + q(unit_dir + "/" + unit))
        run("systemctl --user daemon-reload")
        check_loaded(new)
        initial_report = json.loads(run(new + "/bin/wall-in-one --update-status"))
        assert initial_report["loaded_services"]["state"] == "current", initial_report
        run(new + "/bin/wall-in-one --service-startup-prepare")
        run(new + "/bin/wall-in-one-service --check-config --config " + q(runtime))
        assert snapshot() == before
        assert run("sha256sum ${home}/.local/state/noctalia/settings.toml").split()[0] == noctalia_before
        start_runtime()
        after = wait_status(lambda value: value["runtime_instance"] != paused["runtime_instance"])
        check_running(new)
        assert after["runtime_version"] == "${newPackage.version}", after
        assert after["runtime_executable"] == new + "/bin/wall-in-one-service", after
        assert snapshot() == before
        assert document(settings)["roots"] == [media_dir, home + "/Offline Library"]
        preserved = all(after[key] == paused[key] for key in ("playback_state", "playlist_id", "shuffle"))
        record("manual_upgrade", {"authored_and_media_bytes_unchanged": True, "preflight_noctalia_bytes_unchanged": True,
            "before": {key: paused[key] for key in ("playback_state", "playlist_id", "shuffle")},
            "after": {key: after[key] for key in ("playback_state", "playlist_id", "shuffle")},
            "temporary_playback_preserved": preserved, "policy": "restart using saved settings"})
        ctl(new, "open media")
        machine.wait_for_file("${runtimeDir}/wall-in-one.sock")
        wait_gui_inventory(new)
        assert media_dir + "/night-grid.png" in ctl(new, "favourites")
        new_gui = windows()[0]
        assert new in machine.succeed("tr '\\0' ' ' < /proc/" + str(new_gui["pid"]) + "/cmdline")
        run(new + "/bin/wall-in-one --open-page settings")
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'Wall-in-One - Settings'"), timeout=20)
        assert len(windows()) == 1 and windows()[0]["pid"] == new_gui["pid"]
        assert not [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") == "dev.goober.WallInOne.Update"]
        run(new + "/bin/wall-in-one --open-page media")
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'Wall-in-One - Library'"), timeout=20)
        assert snapshot() == before
        record("same_package_desktop_activation", {"same_gui_process": True,
            "requested_page_presented": True, "no_update_notice": True,
            "authored_and_media_bytes_unchanged": True})
        machine.screenshot("upgraded-library")
        close_gui()
        assert snapshot() == before

    with subtest("a current runtime cannot hide old loaded commands"):
        before = snapshot()
        pid = check_running(new)
        unit_dir = home + "/.config/systemd/user"
        for unit in ("wall-in-one.service", "wall-in-one-health-sync.service", "wall-in-one-health-sync.timer"):
            run("ln -sfn " + q(old + "/share/systemd/user/" + unit) + " " + q(unit_dir + "/" + unit))
        run("systemctl --user daemon-reload")
        check_loaded(old)
        assert check_running(new) == pid
        mixed = json.loads(run(new + "/bin/wall-in-one --update-status"))
        assert mixed["loaded_services"]["state"] == "unverified", mixed
        assert mixed["running_runtime"]["status"]["runtime_executable"] == new + "/bin/wall-in-one-service"
        assert mixed["read_only"] and not mixed["handover_ready"], mixed
        unit_refusal_code = """
    from wall_in_one import config
    try:
        config.update({"stop_animations_on_battery": True})
    except config.ConfigError as error:
        assert "next wallpaper service start" in str(error), error
        print(error)
    else:
        raise AssertionError("new writer accepted battery enable with old loaded commands")
    """
        refusal = package_python(new, unit_refusal_code)
        assert snapshot() == before
        assert check_running(new) == pid
        for unit in ("wall-in-one.service", "wall-in-one-health-sync.service", "wall-in-one-health-sync.timer"):
            run("ln -sfn " + q(new + "/share/systemd/user/" + unit) + " " + q(unit_dir + "/" + unit))
        run("systemctl --user daemon-reload")
        current_report = json.loads(run(new + "/bin/wall-in-one --update-status"))
        assert current_report["loaded_services"]["state"] == "current", current_report
        assert snapshot() == before
        # A matching main executable is insufficient if an extra lifecycle
        # hook still invokes an old writer. Never execute this test hook.
        hook = unit_dir + "/wall-in-one.service.d/90-test-old-hook.conf"
        hook_text = "[Service]\nExecStartPost=" + old + "/bin/wall-in-one --sync-runtime-health\n"
        package_python(new, "from pathlib import Path; p=Path(" + repr(hook) + "); "
            + "p.parent.mkdir(exist_ok=True); p.write_text(" + repr(hook_text) + ")")
        run("systemctl --user daemon-reload")
        hook_report = json.loads(run(new + "/bin/wall-in-one --update-status"))
        assert hook_report["loaded_services"]["state"] == "unverified", hook_report
        hook_commands = hook_report["loaded_services"]["units"][0]["commands"]["ExecStartPost"]
        assert len(hook_commands) == 1 and hook_commands[0]["executable"] == old + "/bin/wall-in-one", hook_commands
        package_python(new, unit_refusal_code)
        assert check_running(new) == pid and snapshot() == before
        run("rm " + q(hook))
        run("systemctl --user daemon-reload")
        assert json.loads(run(new + "/bin/wall-in-one --update-status"))["loaded_services"]["state"] == "current"
        record("new_runtime_old_loaded_units", {"refused_before_save": True, "error": refusal,
            "runtime_untouched": True, "authored_and_media_bytes_unchanged": True,
            "current_after_owner_restores_units": True, "old_extra_hook_refused": True})

    with subtest("app-first companion replacement loads the candidate without replacing its runtime"):
        prior_instance = status()["runtime_instance"]
        run("systemctl --user stop noctalia.service")
        # Change only the fixture's explicit source and executable selections.
        # All unrelated Noctalia settings must survive the file publication.
        code = """
    import copy, os, tomllib
    from pathlib import Path
    path = Path('${home}/.local/state/noctalia/settings.toml')
    text = path.read_text()
    before = tomllib.loads(text)
    expected = copy.deepcopy(before)
    sources = [source for source in expected['plugins']['source'] if source['name'] == 'wall-in-one-vm']
    assert len(sources) == 1 and sources[0]['location'] == '${oldPluginSource}'
    sources[0]['location'] = '${newPluginSource}'
    assert expected['plugin_settings']['goober/wall-in-one']['binary_path'] == '${oldPackage}/bin/wall-in-one'
    expected['plugin_settings']['goober/wall-in-one']['binary_path'] = '${newPackage}/bin/wall-in-one'
    assert text.count('${oldPluginSource}') == 1
    assert text.count('${oldPackage}/bin/wall-in-one') == 1
    text = text.replace('${oldPluginSource}', '${newPluginSource}').replace('${oldPackage}/bin/wall-in-one', '${newPackage}/bin/wall-in-one')
    assert tomllib.loads(text) == expected
    temporary = path.with_name('.settings.toml.upgrade-test')
    temporary.write_text(text)
    os.replace(temporary, path)
    """
        run("${pkgs.python314}/bin/python3 -c " + q(code))
        cursor = run("journalctl --user -u noctalia.service -n 0 --show-cursor --no-pager").split("-- cursor: ")[-1]
        run("systemctl --user start noctalia.service")
        machine.wait_until_succeeds(user("journalctl --user -u noctalia.service --after-cursor=" + q(cursor)
            + " --no-pager | grep -F \"started service 'goober/wall-in-one:control'\""), timeout=45)
        assert status()["runtime_instance"] == prior_instance
        check_running(new)
        record("app_first_companion_update", {"source": "${newPluginSource}", "same_candidate_runtime": True,
            "unrelated_noctalia_settings_preserved_on_publication": True})

    with subtest("battery-enabled B and the old parser respect their schema compatibility"):
        stop_runtime()
        change_settings(new, {"stop_animations_on_battery": True})
        start_runtime()
        enabled = wait_status(lambda value: value.get("stop_animations_on_battery") is True)
        check_running(new)
        assert document(runtime)["schema_version"] == 5
        stop_runtime()
        before = snapshot()
        runtime_hash = run("sha256sum " + q(runtime)).split()[0]
        parser_code, parser_output = machine.execute(user(old + "/bin/wall-in-one-service --check-config --config " + q(runtime) + " 2>&1"))
        if old_supports_battery:
            # Maintenance updates may change dependencies without changing the
            # config schema. An older compatible parser must still accept it.
            assert parser_code == 0, (parser_code, parser_output)
            record("battery_enabled_compatible_old_parser", {"parser_exit": parser_code,
                "schema_five_supported": True, "files_unchanged": True})
        else:
            compiler_code, compiler_output = machine.execute(user(old + "/bin/wall-in-one --write-config 2>&1"))
            assert compiler_code != 0 and "stop_animations_on_battery" in compiler_output, (compiler_code, compiler_output)
            assert parser_code == 78, (parser_code, parser_output)
            record("battery_enabled_package_only_rollback", {"compiler_exit": compiler_code, "parser_exit": parser_code,
                "files_unchanged": True, "compiler_error": compiler_output.strip(), "power_available": enabled["power_available"],
                "recovery": "restart the compatible newer package; no automatic data downgrade"})
        assert snapshot() == before
        assert run("sha256sum " + q(runtime)).split()[0] == runtime_hash
        machine.fail("test -e ${runtimeDir}/wall-in-one-runtime.sock")
        start_runtime()
        wait_status(lambda value: value.get("stop_animations_on_battery") is True)
        check_running(new)

    with subtest("bad settings stop the daemon once but still open graphical recovery"):
        # Cage's minimal fixture starts individual user services directly.
        # File launching also needs the normal desktop session/portal target.
        run("systemd-run --user --unit=wall-in-one-test-session "
            "--property=Type=oneshot --property=RemainAfterExit=yes "
            "--property=BindsTo=graphical-session.target ${pkgs.coreutils}/bin/true")
        run("systemctl --user is-active graphical-session.target")
        run("systemctl --user start xdg-desktop-portal.service")
        close_gui()
        stop_runtime()
        original = snapshot()
        # Leave the saved paths and settings visible in the editor. Removing
        # this single invalid final line restores their exact original bytes.
        run("printf 'future_setting = true' >> " + q(settings))
        run("${pkgs.glib.bin}/bin/gio mime application/toml org.xfce.mousepad.desktop")
        run("${pkgs.glib.bin}/bin/gio mime text/plain org.xfce.mousepad.desktop")
        broken_hash = run("sha256sum " + q(settings)).split()[0]
        runtime_hash = run("sha256sum " + q(runtime)).split()[0]
        code, output = machine.execute(user("systemctl --user start wall-in-one.service"))
        assert code != 0, output
        assert prop("MainPID") == "0"
        assert prop("ActiveState") == "failed"
        invocation, restarts = prop("InvocationID"), prop("NRestarts")
        run("nohup " + new + "/bin/wall-in-one --open-page settings > /tmp/config-recovery.log 2>&1 < /dev/null &")
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'Configuration needs attention'"), timeout=30)
        repairs = [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") == "dev.goober.WallInOne.Repair"]
        assert len(repairs) == 1, repairs
        assert not windows(), "broken settings must not silently open a default authoring profile"
        machine.screenshot("configuration-recovery")
        machine.sleep(12)
        assert prop("MainPID") == "0" and prop("InvocationID") == invocation
        assert prop("NRestarts") == restarts
        assert run("sha256sum " + q(settings)).split()[0] == broken_hash
        assert run("sha256sum " + q(runtime)).split()[0] == runtime_hash
        recovery_pid = repairs[0]["pid"]
        niri("action focus-window --id " + str(repairs[0]["id"]))
        # The selectable diagnostic starts focused. Each FlowBox child and
        # its button take a tab stop. Retry without repairing first.
        for key in ["tab"] * 6 + ["ret"]:
            machine.send_key(key, delay=0.3)
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg --json windows | ${lib.getExe pkgs.jq} -e "
            + q('any(.[]; .app_id == "dev.goober.WallInOne.Repair" and .id != ' + str(repairs[0]["id"]) + ')')), timeout=30)
        assert run("sha256sum " + q(settings)).split()[0] == broken_hash
        # Open settings via the recovery window's real Gtk.FileLauncher. No
        # fixture process writes the repair, and the app is never relaunched.
        for key in ("tab", "tab", "ret"):
            machine.send_key(key, delay=0.3)
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'mousepad'"), timeout=30)
        editors = [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") in ("org.xfce.mousepad", "mousepad")]
        assert len(editors) == 1 and "settings.toml" in editors[0]["title"], editors
        niri("action focus-window --id " + str(editors[0]["id"]))
        machine.screenshot("configuration-external-editor")
        for key in ("ctrl-end", "shift-home", "backspace", "ctrl-s"):
            machine.send_key(key, delay=0.3)
        machine.wait_until_succeeds(user("test $(sha256sum " + q(settings)
            + " | cut -d' ' -f1) = " + q(original[settings])), timeout=20)
        niri("action close-window --id " + str(editors[0]["id"]))
        repairs = [window for window in json.loads(niri("--json windows"))
            if window.get("app_id") == "dev.goober.WallInOne.Repair"]
        assert len(repairs) == 1 and repairs[0]["pid"] == recovery_pid, repairs
        niri("action focus-window --id " + str(repairs[0]["id"]))
        # Focus remains on Open settings file after returning from the editor.
        for key in ["tab"] * 4 + ["ret"]:
            machine.send_key(key, delay=0.3)
        machine.wait_until_succeeds(user(
            "NIRI_SOCKET=$(find ${runtimeDir} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            "${lib.getExe pkgs.niri} msg windows | grep -F 'Wall-in-One - Settings'"), timeout=30)
        assert len(windows()) == 1 and windows()[0]["pid"] == recovery_pid
        assert prop("MainPID") == "0", "Settings must open before the failed daemon is restarted"
        machine.screenshot("configuration-repaired-settings")
        assert snapshot() == original
        run("systemctl --user reset-failed wall-in-one.service")
        start_runtime()
        check_running(new)
        close_gui()
        record("configuration_repair", {"graphical_recovery_opened": True,
            "no_daemon_restart_loop": True, "broken_bytes_untouched": True,
            "unrepaired_retry_reopened_recovery": True, "external_editor_saved_repair": True,
            "same_process_retry_opened_settings_without_daemon": True,
            "normal_settings_and_service_recovered": True, "original_roots_and_data_preserved": True})
        run("! grep -Ei 'Traceback \\(most recent call last\\)|GLib-GObject.*CRITICAL|Gtk-CRITICAL|Adwaita-CRITICAL' /tmp/config-recovery.log")
        machine.copy_from_machine("/tmp/config-recovery.log")

    report["restart_safety_passed"] = not report["release_blockers"]
    report["not_covered"] = ["Home Manager/NixOS activation", "pending saves/downloads/crash boundaries",
        "prepared legacy migration journals", "plugin-first/pinned override matrix", "physical multiple displays"]
    print("UPGRADE_REPORT " + json.dumps(report, sort_keys=True))
    # Export structured evidence even when this characterization is used as a
    # strict acceptance gate. Expected incompatibilities are not app crashes.
    run("${pkgs.python314}/bin/python3 -c " + q("from pathlib import Path; Path('/tmp/upgrade-observations.json').write_text(" + repr(json.dumps(report, indent=2) + "\n") + ")"))
    machine.copy_from_machine("/tmp/upgrade-observations.json")
    run("! grep -Ei 'Traceback \\(most recent call last\\)|GLib-GObject.*CRITICAL|Gtk-CRITICAL|Adwaita-CRITICAL' /tmp/wall-in-one-update-launch.log")
    machine.copy_from_machine("/tmp/wall-in-one-update-launch.log")
    machine.fail("coredumpctl --json=short | grep -E 'wall-in-one|niri|noctalia'")
    ${lib.optionalString requireSafeRestart ''
      assert report["restart_safety_passed"], report["release_blockers"]
    ''}
  '';
}
