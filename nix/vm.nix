{
  lib,
  pkgs,
  ...
}:

{
  imports = [ ./vm-base.nix ];

  networking.hostName = "wall-in-one-vm";

  # `system.build.vm` supplies a disposable root image automatically, but this
  # is also an exported nixosConfiguration and `nix flake check` validates it
  # as a complete machine. State the disk layout the generated QEMU runner
  # actually uses instead of relying on the VM module's late defaults.
  boot.loader.grub.devices = [ "/dev/vda" ];
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  virtualisation.vmVariant = {
    virtualisation = {
      cores = 4;
      memorySize = 6144;
      diskSize = 16384;
      graphics = true;
      # Cage owns QEMU's boot framebuffer and presents niri as its one
      # full-screen nested client. `-vga virtio` provides the display device
      # required by that outer session without pretending the VM has a GPU.
      qemu.options = [ "-vga virtio" ];
    };
  };

  # The generated runner already exposes the serial console. Keep a local
  # graphical console as well; `nix run .#vm` opens it automatically.
  services.getty.autologinUser = lib.mkDefault null;

  environment.systemPackages = [ pkgs.openssh ];
}
