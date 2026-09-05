{
  pkgs,
  oldPackage,
  newPackage,
}:

let
  withoutBytecode = pkgs.lib.fileset.fileFilter (file: !(file.hasExt "pyc" || file.hasExt "pyo"));
  source = pkgs.lib.fileset.toSource {
    root = ../.;
    fileset = pkgs.lib.fileset.unions [
      (withoutBytecode ../src)
      (withoutBytecode ../tests)
      ../pyproject.toml
    ];
  };
  python = pkgs.python314.withPackages (ps: [ ps.pytest ]);
in
pkgs.runCommand "wall-in-one-packaged-upgrade-incident"
  {
    nativeBuildInputs = [ python ];
  }
  ''
    export HOME="$TMPDIR/home"
    export XDG_CONFIG_HOME="$TMPDIR/config"
    export XDG_STATE_HOME="$TMPDIR/state"
    export XDG_CACHE_HOME="$TMPDIR/cache"
    export XDG_DATA_HOME="$TMPDIR/data"
    export XDG_RUNTIME_DIR="$TMPDIR/run"
    export WALL_IN_ONE_OLD_PACKAGE=${oldPackage}
    export WALL_IN_ONE_NEW_PACKAGE=${newPackage}
    export WALL_IN_ONE_PACKAGED_PYTHON=${pkgs.python314}/bin/python3
    unset DISPLAY WAYLAND_DISPLAY NIRI_SOCKET DBUS_SESSION_BUS_ADDRESS DBUS_SYSTEM_BUS_ADDRESS
    mkdir -p "$out"
    cd ${source}
    PYTHONPATH=$PWD:$PWD/src pytest tests/test_packaged_upgrade.py -q \
      -p no:cacheprovider --junitxml="$out/results.xml"
  ''
