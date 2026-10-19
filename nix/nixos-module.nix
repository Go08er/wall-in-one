# programs.wall-in-one for NixOS, to import by path (nix/module.nix). Through
# the flake, use nixosModules.default instead, which also defaults `package`
# to this flake's; imported by path, set programs.wall-in-one.package.
import ./module.nix { target = "nixos"; }
