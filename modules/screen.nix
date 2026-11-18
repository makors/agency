# The laptop's own display: a status screen instead of a login prompt.
#
# It shows the machine's name, its Tailscale address, the desktops, whether
# the system is up to date, and a QR code that opens the dashboard on a
# phone. It is the page /screen of the dashboard (dashboard/ui/screen.*),
# shown fullscreen by a browser in cage, like the installer's setup screen.
#
# The page needs no login and shows nothing secret. The browser runs as its
# own unprivileged account, not as the agent account.
#
# `agency screen show URL` puts another page on the display instead, such as
# one an agent serves on this machine; `agency screen reset` brings the
# status screen back. No rebuild: the status page shows it in a frame.
#
# Ctrl+Alt+F2 switches to a text console; getty runs on tty2 and up.
{
  lib,
  pkgs,
  ...
}:
let
  url = "http://127.0.0.1:7380/screen";
  user = "agency-screen";

  # The kiosk's one program: wait for the dashboard, then show the page.
  # The browser profile lives in the session's runtime directory, so every
  # start is fresh and nothing builds up on disk.
  kiosk = pkgs.writeShellScript "agency-screen-kiosk" ''
    for _ in $(${pkgs.coreutils}/bin/seq 120); do
      ${pkgs.curl}/bin/curl -fs -o /dev/null ${url} && break
      ${pkgs.coreutils}/bin/sleep 0.5
    done
    exec ${pkgs.chromium}/bin/chromium \
      --kiosk --app=${url} \
      --ozone-platform=wayland --disable-gpu \
      --user-data-dir="''${XDG_RUNTIME_DIR:-/tmp}/agency-screen" \
      --no-first-run --noerrdialogs --disable-translate \
      --disable-features=Translate --disable-pinch \
      --overscroll-history-navigation=0
  '';
in
{
  users.users.${user} = {
    isSystemUser = true;
    group = user;
    description = "agency status screen";
    home = "/var/lib/${user}";
    createHome = true;
    extraGroups = [ "video" ];
  };
  users.groups.${user} = { };

  # cage shows one program fullscreen on tty1. "-s" keeps Ctrl+Alt+F2
  # working, and the pixman renderer needs no graphics driver beyond a screen.
  services.cage = {
    enable = true;
    inherit user;
    program = kiosk;
    extraArguments = [ "-s" ];
    environment = {
      WLR_RENDERER = "pixman";
      # A full session, not a "light" one: since systemd 258 a system
      # account gets a light session with no service manager or bus, and
      # every `nixos-rebuild switch` then fails to reload its user units.
      XDG_SESSION_CLASS = "user";
    };
  };

  # No text login on tty1. NixOS still lists getty@tty1 as wanted, so every
  # `nixos-rebuild switch` started it, and because cage conflicts with it,
  # the switch stopped the status screen and left the panel black.
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # If the browser or cage stops, start them again.
  systemd.services."cage-tty1" = {
    after = [ "agency-dashboard.service" ];
    wants = [ "agency-dashboard.service" ];
    serviceConfig = {
      Restart = lib.mkDefault "always";
      RestartSec = lib.mkDefault 5;
    };
  };
}
