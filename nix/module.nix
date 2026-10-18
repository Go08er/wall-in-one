# programs.wall-in-one, for NixOS and for home-manager (docs/packaging.md).
#
# Installs the app, and with it its launcher entry, icon and user units, the
# way docs/installing.md describes a profile install; and, unless
# `manualWallpaperInstalls` is set, the two wallpaper renderers, which the app
# never bundles. Nothing is enabled or started, and no configuration is
# written.
#
# `target` only decides where the packages go: environment.systemPackages
# ("nixos") or home.packages ("home-manager"). `package` has no default here:
# the flake's wrapper gives it this flake's own package, which a file imported
# by path cannot name; the VMs set it to the package under test.
{ target }:

assert builtins.elem target [
  "nixos"
  "home-manager"
];

{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.wall-in-one;
  packages = [
    cfg.package
  ]
  ++ lib.optionals (!cfg.manualWallpaperInstalls) [
    cfg.mpvpaperPackage
    cfg.linuxWallpaperenginePackage
  ];
in
{
  options.programs.wall-in-one = {
    enable = lib.mkEnableOption "Wall-in-One, a wallpaper manager for Wayland with Noctalia palette sync";

    package = lib.mkOption {
      type = lib.types.package;
      defaultText = lib.literalExpression "wall-in-one.packages.\${pkgs.stdenv.hostPlatform.system}.default";
      description = ''
        The Wall-in-One package. It brings its own FFmpeg for thumbnails and
        stills, and no wallpaper renderer.
      '';
    };

    manualWallpaperInstalls = lib.mkOption {
      type = lib.types.bool;
      default = false;
      example = true;
      description = ''
        Whether you install the wallpaper renderers yourself. When false, the
        module installs mpvpaper (video wallpapers) and linux-wallpaperengine
        (Wallpaper Engine scenes) from {option}`programs.wall-in-one.mpvpaperPackage`
        and {option}`programs.wall-in-one.linuxWallpaperenginePackage`. When
        true it installs neither, and Wall-in-One uses whatever you have
        installed in a Nix profile or on {env}`PATH`; a renderer that is not
        there just can't play its kind of wallpaper.
      '';
    };

    mpvpaperPackage = lib.mkPackageOption pkgs "mpvpaper" {
      extraDescription = ''
        Installed unless {option}`programs.wall-in-one.manualWallpaperInstalls`
        is set. It comes from this configuration's `pkgs`, so its overlays
        apply.
      '';
    };

    linuxWallpaperenginePackage = lib.mkPackageOption pkgs "linux-wallpaperengine" {
      extraDescription = ''
        Installed unless {option}`programs.wall-in-one.manualWallpaperInstalls`
        is set. It comes from this configuration's `pkgs`, so its overlays
        apply. It is free software, but built against a prebuilt Chromium
        Embedded Framework (`cef-binary`), so a configuration that refuses
        packages not built from source refuses it too.
      '';
    };
  };

  config = lib.mkIf cfg.enable (
    if target == "nixos" then
      { environment.systemPackages = packages; }
    else
      { home.packages = packages; }
  );
}
