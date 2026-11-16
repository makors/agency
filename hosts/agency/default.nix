# The machine. Each import is one concern; read them in this order.
#
# A machine put on disk by the installer uses hardware.nix and disko.nix.
# To run agency on a machine that already has NixOS, with its own disks and
# boot loader, add hosts/local/default.nix: it is used in their place (see
# docs/existing-nixos.md).
{ lib, settings, ... }:
let
  local = ../local/default.nix;
  hasLocal = builtins.pathExists local;
in
{
  imports =
    (
      if hasLocal then
        [ local ] # this machine's own hardware, disks and boot loader
      else
        [
          ./hardware.nix # what the laptop is made of, and how it boots
          ./disko.nix # how the disk is laid out
        ]
    )
    ++ [
      ./vm.nix # `nix run .#vm`: the same machine in a window, to try it
      ../../modules/agents.nix # the agent account and its tools
      ../../modules/apps.nix # optional software chosen in settings.nix
      ../../modules/firstboot.nix # finishes the installer's choices on first boot
      ../../modules/remote.nix # how you and agents reach the machine
      ../../modules/desktop.nix # the virtual desktop for GUI apps
      ../../modules/laptop.nix # run lid-closed, never sleep
      ../../modules/battery.nix # cap the battery charge
      ../../modules/cli.nix # the `agency` command
      ../../modules/autoupdate.nix # weekly nixpkgs updates
      ../../modules/secrets.nix # encrypted secrets for agents
      ../../modules/backup.nix # hourly backups of the home folder
      ../../modules/skills.nix # what agent CLIs are told about this machine
      ../../modules/agent-status.nix # agent CLIs report what they are doing
      ../../modules/dashboard.nix # the web dashboard and terminal, over Tailscale
      ../../modules/screen.nix # the status screen on the laptop's own display
    ];

  # When empty, the name comes from /etc/hostname, which the installer
  # writes with the name chosen during setup.
  networking.hostName = settings.hostName;
  time.timeZone = settings.timeZone;
  i18n.defaultLocale = "en_US.UTF-8";

  # A machine with its own hardware file logs in with the keys in
  # settings.nix only: the installer, which fetches them from GitHub, never ran.
  warnings = lib.optional (hasLocal && settings.sshKeys == [ ])
    "agency: sshKeys in settings.nix is empty, and password login over SSH is off. Add your public key there, or you will not be able to log in as ${settings.user}.";

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [
      "root"
      settings.user
    ];
    auto-optimise-store = true;
  };
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };
  nixpkgs.config.allowUnfree = true;

  # Do not change after install. It records which NixOS release first
  # created this machine's state; it is not the version you are running.
  system.stateVersion = lib.mkDefault "26.05";
}
