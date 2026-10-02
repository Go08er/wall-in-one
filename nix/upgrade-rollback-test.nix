{
  pkgs,
  # The shipped v0.1.4 build, from the release tag's own flake and lock.
  oldPackage,
  # This build (0.2.0 once released).
  newPackage,
  pluginSource,
  sampleMedia,
  # Development only: record every expectation failure and keep going, so one
  # run shows the whole scenario. The flake check always runs strict.
  strict ? true,
}:

# Update to this build and roll back to v0.1.4 on the installed packages, with
# the golden profile, a real user session and the packaged user units.
#
# The install is modelled as a user profile: one symlink that is flipped from
# one package to the other (what `nix profile install`/`upgrade` does to
# ~/.nix-profile), with the three packaged units linked into
# ~/.config/systemd/user *through* that symlink. Flip, `daemon-reload`, and
# the next start runs the other release -- while the running service and the
# health-sync timer's next run show the mixed window a real update has.
# NixOS and Home Manager activation themselves are not exercised.
let
  inherit (pkgs) lib;
  user = "wallpaper";
  home = "/home/${user}";
  runtimeDir = "/run/user/1000";
  python = pkgs.python314;
  driverLog = "/tmp/wall-in-one-wallpaper-set.log";
  profileLink = "${home}/.local/share/wall-in-one-upgrade/current";
  units = [
    "wall-in-one.service"
    "wall-in-one-health-sync.service"
    "wall-in-one-health-sync.timer"
  ];

  # The golden profile, the harness that materializes and snapshots it, and
  # the guest-side tool, in one store path both the guest and the driver read.
  # Building it first proves the tool fails an unrelated change to Noctalia's
  # settings, so a broken write gate cannot boot a VM that passes.
  support = pkgs.runCommand "wall-in-one-upgrade-rollback-support" { } ''
    mkdir -p $out
    cp ${../tests/golden/harness.py} $out/harness.py
    cp ${./upgrade-rollback/vm_tool.py} $out/vm_tool.py
    cp -R ${../tests/golden/profile} $out/profile
    PYTHONPATH=$out PYTHONDONTWRITEBYTECODE=1 \
      ${python.interpreter} ${./upgrade-rollback/vm_tool_selfcheck.py} $out/profile
  '';

  # Records which process asked Noctalia to show a wallpaper, and whether
  # Noctalia did: the proof that a service, and which one, applied one. An
  # attempt while the shell is still starting fails, and must not count.
  noctaliaProbe = pkgs.writeShellScriptBin "noctalia" ''
    if [ "$#" -ge 2 ] && [ "$1" = msg ] && [ "$2" = wallpaper-set ]; then
      status=0
      ${lib.getExe pkgs.noctalia} "$@" || status=$?
      printf '%s\t%s\t%s\n' "$PPID" "$status" "$*" >> ${driverLog}
      exit "$status"
    fi
    exec ${lib.getExe pkgs.noctalia} "$@"
  '';
in
pkgs.testers.runNixOSTest {
  name = "wall-in-one-upgrade-rollback";
  globalTimeout = 1800;

  node.specialArgs = {
    # vm-base installs the old release system-wide too; the user profile's
    # links take precedence for the units, and every command below names its
    # package explicitly.
    wallInOnePackage = oldPackage;
    inherit pluginSource sampleMedia noctaliaProbe;
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
      environment.systemPackages = [ python ];
      # Present in the guest's store from boot, though nothing points at it
      # until the update.
      system.extraDependencies = [
        newPackage
        support
      ];

      # Replace vm-base's demo profile with the golden one before the session
      # (and so v0.1.4's service) first starts.
      systemd.services.wall-in-one-vm-seed.script = lib.mkAfter ''
        # A Wednesday in January, mid-morning: neither of the fixture's timed
        # rules (Evenings 21:00-04:00, Calm stills on summer weekends) is
        # active, so the default playlist plays whatever the host's calendar.
        date -s '2031-01-08 10:00:00'

        rm -rf ${home}/.config/wall-in-one ${home}/.local/state/wall-in-one \
          ${home}/Pictures/Wallpapers
        PYTHONPATH=${support}:${newPackage}/${python.sitePackages} \
          PYTHONDONTWRITEBYTECODE=1 \
          ${python.interpreter} ${support}/vm_tool.py seed \
            --fixture ${support}/profile --home ${home}

        install -d -m 0755 ${home}/.local/share/wall-in-one-upgrade \
          ${home}/.config/systemd/user
        ln -sfn ${oldPackage} ${profileLink}
        for unit in ${lib.escapeShellArgs units}; do
          ln -sfn ${profileLink}/share/systemd/user/$unit \
            ${home}/.config/systemd/user/$unit
        done
        # The golden profile brings its own Noctalia settings (they replace
        # vm-base's): the user template that renders palette.json on every
        # wallpaper change, and no companion plugin. Its palette hook names
        # the owner's install path; point it at this VM's, the profile.
        sed -i 's|/run/current-system/sw/bin/wall-in-one|${profileLink}/bin/wall-in-one|' \
          ${home}/.local/state/noctalia/settings.toml
        grep -F 'post_hook = "${profileLink}/bin/wall-in-one ctl reload-palette"' \
          ${home}/.local/state/noctalia/settings.toml

        chown -R ${user}:users ${home}/.config ${home}/.local ${home}/.cache \
          ${home}/Pictures
        PYTHONPATH=${support} PYTHONDONTWRITEBYTECODE=1 \
          ${python.interpreter} ${support}/vm_tool.py snapshot --home ${home} \
            --out /var/lib/wall-in-one-upgrade/seed.json
      '';
    };

  testScript = ''
    OLD = "${oldPackage}"
    NEW = "${newPackage}"
    HOME = "${home}"
    RUNTIME_DIR = "${runtimeDir}"
    PROFILE = "${profileLink}"
    DRIVER_LOG = "${driverLog}"
    NOCTALIA_PROBE = "${noctaliaProbe}"
    SUPPORT = "${support}"
    GUEST_PYTHON = "${python.interpreter}"
    PACKAGE_PYTHON = "${python.withPackages (ps: [ ps.pygobject3 ])}/bin/python3"
    SITE_PACKAGES = "${python.sitePackages}"
    BASH = "${lib.getExe pkgs.bash}"
    NIRI = "${lib.getExe pkgs.niri}"
    STRICT = ${if strict then "True" else "False"}
  ''
  + builtins.readFile ./upgrade-rollback/test_script.py;
}
