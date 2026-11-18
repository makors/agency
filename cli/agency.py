"""agency: look after this machine.

Run `agency` for a summary of the machine and `agency help` for the commands.
It runs as the agent account and uses sudo where it must. Every change it
makes to /etc/nixos is committed, so `git log` there shows what happened.
"""

import argparse
import getpass
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path("/etc/nixos")
SETTINGS = REPO / "settings.nix"
FLAKE = f"path:{REPO}#agency"
USER = os.environ.get("AGENCY_USER", "agent")
DEFAULT_REPO = "https://github.com/makors/agency"
# Parts of the repo the running system never uses: the download page, docs,
# READMEs and CI. Changes to only these need no rebuild, and the dashboard
# does not offer them as updates. Keep in step with dashboard/server.py.
NOT_SYSTEM = ("site", "docs", ".github", "**/README.md")

# Written by the installer: the commit of this repo the machine came from.
SOURCE_REV = Path("/var/lib/agency/source-rev")
FIRSTBOOT = Path("/var/lib/agency/firstboot")
FIRSTBOOT_FAILED = Path("/var/lib/agency/firstboot-failed")
RECOVERY_KEY = Path("/root/luks-recovery-key.txt")
# The page the laptop's own display shows instead of the status screen
# (`agency screen show URL`); the dashboard's /screen reads it.
SCREEN_URL = Path("/var/lib/agency/screen-url")

# Backups (modules/backup.nix).
BACKUP_UNIT = "restic-backups-home.service"
BACKUP_LAST = Path("/var/lib/agency/backup-last")
BACKUP_PASSWORD = Path("/var/lib/agency/backup-password")
BACKUP_PASSWORD_SAVED = Path("/var/lib/agency/backup-password-saved")

SECRETS = REPO / "secrets" / "agency.sops.json"
SOPS_CONFIG = REPO / ".sops.yaml"
HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key")
AUTHORIZED_KEYS = Path("/etc/agency/authorized_keys")
SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


# ------------------------------------------------------------- helpers ---


def say(msg=""):
    print(msg, flush=True)


def die(msg):
    print(f"agency: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, check=True, capture=False, stdin=None, env=None, cwd=None):
    """Run a command. With check, stop agency if it fails."""
    result = subprocess.run(
        [str(c) for c in cmd],
        input=stdin,
        env=env,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            sys.stderr.write(result.stderr)
        die(f"this failed: {' '.join(str(c) for c in cmd)}")
    return result


def output(cmd):
    """A command's output, or "" if it fails."""
    result = run(cmd, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def tool(name):
    """Full path of a tool, for running it under sudo."""
    path = shutil.which(name)
    if not path:
        die(f"{name} is missing")
    return path


def root_exists(path):
    """Whether a path exists, also inside folders only root can read."""
    try:
        return path.exists()
    except PermissionError:
        return run(["sudo", "-n", "test", "-e", path], check=False, capture=True).returncode == 0


def root_read(path):
    """A file's text, also when only root can read it; "" if it is missing."""
    try:
        return path.read_text() if path.exists() else ""
    except PermissionError:
        result = run(["sudo", "-n", "cat", path], check=False, capture=True)
        return result.stdout if result.returncode == 0 else ""


def git(*args, **kw):
    return run(["git", "-C", REPO, *args], **kw)


def host():
    return socket.gethostname()


def desktop_url(n):
    """Where a person opens desktop :n: the tailnet name `tailscale serve`
    uses (see desktop.nix), or the plain host name before Tailscale is up."""
    try:
        base = Path("/run/agency/dashboard-url").read_text().strip()
    except OSError:
        base = f"https://{host()}"
    return f"{base}:{6080 + n}/"


def ask(question):
    if not sys.stdin.isatty():
        die("this needs a person at the keyboard")
    return input(question).strip()


# ------------------------------------------------------------ settings ---
# settings.nix keeps the values agency edits on one line each.


def read_setting(key):
    match = re.search(rf"^  {key} = (.*);$", SETTINGS.read_text(), re.M)
    return match.group(1) if match else None


def write_setting(key, value):
    text, n = re.subn(
        rf"^  {key} = .*;$", f"  {key} = {value};", SETTINGS.read_text(), count=1, flags=re.M
    )
    if n == 0:
        die(f"settings.nix has no one-line `{key} = ...;` to change")
    SETTINGS.write_text(text)


def setting_string(key):
    value = read_setting(key)
    return json.loads(value) if value and value.startswith('"') else ""


def setting_list(key):
    return re.findall(r'"([^"]*)"', read_setting(key) or "")


def channel():
    """The nixpkgs this machine follows: "stable" or "unstable" (settings.nix)."""
    return setting_string("channel") or "stable"


def channel_input():
    """The flake input that `channel` selects (see flake.nix)."""
    return "nixpkgs-unstable" if channel() == "unstable" else "nixpkgs"


def nix_list(items):
    return "[ " + "".join(f'"{i}" ' for i in items) + "]"


def desktop_count():
    value = read_setting("desktops")
    return int(value) if value and value.isdigit() else 1


# ---------------------------------------------------------------- repo ---


def setup_identity():
    if not output(["git", "-C", REPO, "config", "user.email"]):
        git("config", "user.name", f"agency on {host()}")
        git("config", "user.email", f"{USER}@{host()}")


def ensure_repo():
    """Put /etc/nixos under git, on top of the commit it was installed from.

    The installer copies the repo as plain files. This makes it a clone of
    `repo` in settings.nix with this machine's settings as one commit on
    top, so `agency update` can pull new versions in.
    """
    if (REPO / ".git").is_dir():
        if git("rev-parse", "--abbrev-ref", "@{u}", check=False, capture=True).returncode == 0:
            return
        # A link that stopped halfway: no upstream. Keep it aside and start
        # again; the machine's settings are in the files themselves.
        aside = Path.home() / f"nixos-git-unlinked-{int(time.time())}"
        shutil.move(str(REPO / ".git"), aside)
        say(f"/etc/nixos was only partly linked. Its old .git is in {aside}.")
    url = setting_string("repo") or DEFAULT_REPO
    say(f"Linking /etc/nixos to {url} ...")
    run(["sudo", "chown", "-R", f"{USER}:users", REPO])
    try:
        link(url)
    except BaseException:
        # Leave no half-made repo behind, or the next run trusts it.
        shutil.rmtree(REPO / ".git", ignore_errors=True)
        raise
    say("Done. /etc/nixos is now a git repo.")


def link(url):
    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", url)
    if git("fetch", "-q", "origin", "main", check=False).returncode != 0:
        die(f"could not fetch {url}. Check the network and try again.")
    setup_identity()

    base = root_read(SOURCE_REV).strip()
    known = base and git("cat-file", "-e", f"{base}^{{commit}}", check=False, capture=True).returncode == 0
    if not known:
        base = "origin/main"
        say("  The commit this machine was installed from is not known, so its files")
        say("  are compared with the newest version. Check `git -C /etc/nixos show`.")
    git("reset", "-q", "--mixed", base)
    git("branch", "-q", "--set-upstream-to", "origin/main")
    commit("This machine's settings", ".")


def commit(message, *paths):
    git("add", "-A", "--", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        git("commit", "-q", "-m", message)


def uncommitted():
    return output(["git", "-C", REPO, "status", "--porcelain"]) if (REPO / ".git").is_dir() else ""


SWITCH_UNIT = "nixos-rebuild-switch-to-configuration.service"


def switch_running():
    return output(["systemctl", "is-active", SWITCH_UNIT]) in ("active", "activating", "deactivating")


def wait_for_switch(message):
    """Wait until no system switch is running.

    nixos-rebuild runs the switch itself as SWITCH_UNIT, so Ctrl-C or a
    failed nixos-rebuild does not stop a switch that has started: it goes on
    and changes /run/current-system later. Anything that reads what is
    running after a rebuild fails must wait for it first.
    """
    if not switch_running():
        return
    say(message)
    while True:
        try:
            while switch_running():
                time.sleep(1)
            return
        except KeyboardInterrupt:
            say("The switch cannot be stopped halfway. Still waiting for it to finish ...")


def clear_switch_unit():
    """nixos-rebuild runs the switch as a transient unit with a fixed name.

    If another switch is running, wait for it. If an earlier one failed or
    was interrupted and its unit is still loaded, clear it, or this switch
    fails with "Unit ... was already loaded".
    """
    wait_for_switch("Another system switch is running. Waiting for it to finish ...")
    if output(["systemctl", "show", "-p", "LoadState", "--value", SWITCH_UNIT]) == "loaded":
        run(["sudo", "systemctl", "reset-failed", SWITCH_UNIT], check=False, capture=True)
        run(["sudo", "systemctl", "stop", SWITCH_UNIT], check=False, capture=True)


def rebuild(mode="switch"):
    clear_switch_unit()
    say(f"Rebuilding the system ({mode}) ...")
    before = output(["readlink", "-f", "/run/current-system"])
    result = run(["sudo", tool("nixos-rebuild"), mode, "--flake", FLAKE], check=False)
    if result.returncode != 0:
        wait_for_switch("Waiting for the system switch to finish ...")
        now = output(["readlink", "-f", "/run/current-system"])
        if mode == "switch" and now != before and now == output(["readlink", "-f", "/nix/var/nix/profiles/system"]):
            die("the new system is running, but the switch reported a problem (see above).\n"
                "Check `systemctl --failed`. `agency rollback` goes back if something is wrong.")
        die("the rebuild failed. The running system did not change.")
    if mode == "switch":
        say("Done. The new system is running and is the newest boot menu entry.")


def battery_status():
    """One line on each battery's charge and its charge limit, or ''."""
    parts = []
    for bat in sorted(glob.glob("/sys/class/power_supply/*")):
        def read(name):
            try:
                with open(os.path.join(bat, name)) as f:
                    return f.read().strip()
            except OSError:
                return ""
        if read("type") != "Battery":
            continue
        line = f"{read('capacity') or '?'}% {read('status').lower()}"
        end = read("charge_control_end_threshold")
        if end and end not in ("0", "100"):
            line += f", stops at {end}%"
        elif "[Long_Life]" in read("charge_types"):
            line += ", long-life charging"
        elif any(open(m).read().strip() == "1" for m in
                 glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode")):
            line += ", conservation mode"
        else:
            line += ", no charge limit"
        parts.append(line)
    return "; ".join(parts)


# ------------------------------------------------------------ commands ---


def cmd_status(args):
    say(f"agency  {host()}")
    ts_ip = output(["tailscale", "ip", "-4"]).split("\n")[0]
    say(f"  ssh        ssh {USER}@{host()}" + (f"   (tailnet {ts_ip})" if ts_ip else "   (not on a tailnet)"))

    for n in range(1, desktop_count() + 1):
        state = output(["systemctl", "is-active", f"agency-display@{n}.service"]) or "stopped"
        say(f"  desktop :{n}  {desktop_url(n)}   {state}")

    profile = output(["readlink", "/nix/var/nix/profiles/system"])
    generation = re.search(r"system-(\d+)-link", profile)
    head = output(["git", "-C", REPO, "log", "-1", "--format=%h %s (%cr)"]) or "not linked yet, run: agency update"
    say(f"  system     generation {generation.group(1) if generation else '?'}, repo at {head}")
    say(f"  apps       {' '.join(setting_list('apps')) or 'none'}")
    battery = battery_status()
    if battery:
        say(f"  battery    {battery}")
    say(f"  backups    {backup_status()}")
    names = secret_names()
    say(f"  secrets    {', '.join(names) if names else 'none'}")

    notes = []
    if output(["systemctl", "is-failed", "agency-firstboot.service"]) == "failed":
        notes.append("First-start setup failed several times and stopped; it tries again at the next start. "
                     "See: journalctl -u agency-firstboot")
    elif root_exists(FIRSTBOOT):
        notes.append("First-start setup is still running: journalctl -fu agency-firstboot")
    # Lines "ID PACKAGE". One installed since then is no longer listed.
    try:
        failed_lines = [line.split() for line in FIRSTBOOT_FAILED.read_text().splitlines()]
    except OSError:
        failed_lines = []
    bun_modules = Path.home() / ".bun/install/global/node_modules"
    failed_apps = [f[0] for f in failed_lines if len(f) == 2 and not (bun_modules / f[1]).exists()]
    if failed_apps:
        names = " ".join(dict.fromkeys(failed_apps))
        notes.append(f"These apps did not install on the first start: {names}. "
                     f"Try again with: agency apps remove {names} && agency apps add {names}")
    if run(["sudo", "-n", "test", "-e", RECOVERY_KEY], check=False, capture=True).returncode == 0:
        notes.append("The disk recovery key is still on disk. A person should run: agency recovery-key")
    try:
        last = AUTO_UPDATE_LOG.read_text().strip()
    except OSError:
        last = "not run yet"
    say(f"  updates    weekly nixpkgs ({channel()}) update; last: {last}")
    if uncommitted():
        notes.append("/etc/nixos has changes that are not committed.")
    if backups_on():
        if output(["systemctl", "is-failed", BACKUP_UNIT]) == "failed":
            notes.append("The last backup failed: journalctl -u restic-backups-home")
        if backup_remote() and not BACKUP_PASSWORD_SAVED.exists():
            notes.append("The backup password is only on this machine. A person should run: agency backup password")
    if output(["systemctl", "is-failed", "agency-auto-update.service"]) == "failed":
        notes.append("The last automatic update failed: journalctl -u agency-auto-update")
    for note in notes:
        say(f"  ! {note}")


def cmd_update(args):
    ensure_repo()
    if uncommitted():
        commit("Local changes, saved before an update", ".")
        say("Committed the local changes in /etc/nixos first.")
    url = setting_string("repo") or DEFAULT_REPO
    if output(["git", "-C", REPO, "remote", "get-url", "origin"]) != url:
        git("remote", "set-url", "origin", url)
        say(f"Updating from {url} (repo in settings.nix).")
    say("Fetching ...")
    git("fetch", "-q", "origin")
    behind = int(output(["git", "-C", REPO, "rev-list", "--count", "HEAD..@{u}"]) or 0)
    before = output(["git", "-C", REPO, "rev-parse", "HEAD"])
    if behind:
        say(f"{behind} new commit(s):")
        git("log", "--format=  %h %s", "HEAD..@{u}")
        if git("rebase", "-q", "@{u}", check=False).returncode != 0:
            git("rebase", "--abort", check=False)
            die(
                "your local commits conflict with the update. Nothing changed.\n"
                "Resolve it by hand: cd /etc/nixos && git rebase @{u}"
            )
    if args.packages:
        say("Updating nixpkgs and the other inputs ...")
        run(["nix", "flake", "update", "--flake", f"path:{REPO}"])
        commit("Update flake inputs", "flake.lock")
    if not behind and not args.packages:
        say("Already up to date. (`agency update --packages` also updates nixpkgs.)")
        return
    if behind and not args.packages:
        system = ["--", ".", *(f":(exclude,glob){p}" for p in NOT_SYSTEM)]
        if git("diff", "--quiet", before, "HEAD", *system, check=False).returncode == 0:
            say("Nothing the system uses changed. Nothing to rebuild.")
            return
    rebuild()


def cmd_rebuild(args):
    if uncommitted():
        say("Note: /etc/nixos has changes that are not committed. Commit them when they work.")
    rebuild("test" if args.test else "switch")
    if args.test:
        say("Applied until the next reboot. Run `agency rebuild` to keep it.")


AUTO_UPDATE_LOG = Path("/var/lib/agency-auto-update/last")


def cmd_auto_update(args):
    """What the weekly timer runs (modules/autoupdate.nix): newer nixpkgs
    only, never the agency repo, and only from a clean, committed /etc/nixos
    so half-done work is never applied unattended."""
    def record(line):
        say(line)
        try:
            AUTO_UPDATE_LOG.write_text(f"{time.strftime('%Y-%m-%d %H:%M')}  {line}\n")
        except OSError:
            pass

    ensure_repo()
    if uncommitted():
        record("skipped: /etc/nixos has uncommitted changes")
        return
    say(f"Updating nixpkgs ({channel()}) ...")
    run(["nix", "flake", "update", channel_input(), "--flake", f"path:{REPO}"])
    if git("diff", "--quiet", "--", "flake.lock", check=False).returncode == 0:
        record("nixpkgs already up to date")
        return
    try:
        rebuild()
    except SystemExit:
        git("checkout", "--", "flake.lock", check=False)
        record("FAILED: the rebuild with newer nixpkgs failed; see journalctl -u agency-auto-update")
        raise
    commit("Update nixpkgs (weekly auto-update)", "flake.lock")
    record("updated nixpkgs and switched to it")


def channel_trial():
    """The channel settings.nix tries without having committed it, or None.

    `agency channel X --test` leaves `channel` changed and uncommitted (and
    perhaps flake.lock). Any other uncommitted change is not a trial.
    """
    shown = git("show", "HEAD:settings.nix", capture=True, check=False)
    if shown.returncode != 0:
        return None
    committed = shown.stdout
    text = SETTINGS.read_text()
    trying = channel()
    if text == committed or trying == committed_channel(committed):
        return None
    as_committed = re.sub(r'^  channel = .*;$', f'  channel = {json.dumps(trying)};',
                          committed, count=1, flags=re.M)
    return trying if as_committed == text else None


def committed_channel(text):
    match = re.search(r'^  channel = "([^"]*)";$', text, re.M)
    return match.group(1) if match else "stable"


def trial_running():
    """Whether the running system is a `--test` one, not the boot default."""
    return (output(["readlink", "-f", "/run/current-system"])
            != output(["readlink", "-f", "/nix/var/nix/profiles/system"]))


def cmd_channel(args):
    trial = channel_trial()
    if not args.channel:
        if trial:
            committed = committed_channel(git("show", "HEAD:settings.nix", capture=True).stdout)
            state = "running until the next reboot" if trial_running() else "not running (rebooted since)"
            say(f"This machine follows {committed} nixpkgs, and is trying {trial}: {state}.")
            say(f"Keep it: agency channel {trial}. Undo: agency channel {committed}")
        else:
            say(f"This machine follows {channel()} nixpkgs ({channel_input()} in flake.nix).")
            say("Switch with: agency channel stable|unstable [--test]")
        return
    ensure_repo()
    changed = git("status", "--porcelain", "--untracked-files=all", "--no-renames", "-z",
                  capture=True).stdout.split("\0")
    changed = {entry[3:] for entry in changed if entry}
    if changed - {"settings.nix", "flake.lock"} or (changed and not trial):
        die("/etc/nixos has changes that are not committed. Commit them first.")
    committed = committed_channel(git("show", "HEAD:settings.nix", capture=True).stdout)
    target = args.channel
    if not trial and target == committed:
        say(f"Already on {target}.")
        return
    if trial and target == committed:
        # Undo the trial: back to what is committed.
        say(f"Leaving the {trial} trial, back to {committed} ...")
        git("checkout", "--", "settings.nix", "flake.lock")
        rebuild("test" if args.test else "switch")
        return
    if target == "stable" and trial != "stable":
        say("Note: going back to stable moves packages to older versions. Most services")
        say("cope; a few (databases above all) refuse data written by a newer version.")
    files = {p: (REPO / p).read_text() for p in ("settings.nix", "flake.lock")}
    before = output(["readlink", "-f", "/run/current-system"])
    mode = "test" if args.test else "switch"
    try:
        if target != trial:
            write_setting("channel", json.dumps(target))
            # The target input's pin has not moved while this machine followed
            # the other channel (auto-update only updates the one in use).
            say(f"Updating {channel_input()} ...")
            run(["nix", "flake", "update", channel_input(), "--flake", f"path:{REPO}"])
        rebuild(mode)
    except BaseException:  # also Ctrl-C: never leave settings.nix half-switched
        # A switch that had started goes on without us; see what it ends on.
        wait_for_switch("Stopping. The system switch had already started; waiting for it to finish ...")
        now = output(["readlink", "-f", "/run/current-system"])
        if now == before:
            for p, text in files.items():
                (REPO / p).write_text(text)
        elif mode == "switch" and now == output(["readlink", "-f", "/nix/var/nix/profiles/system"]):
            # The new system is running even though the switch reported a
            # problem, so settings.nix must say so. rebuild() has explained.
            commit(f"Follow {target} nixpkgs", "settings.nix", "flake.lock")
        raise
    if args.test:
        say(f"Running {target} until the next reboot. Keep it: agency channel {target}")
        say(f"Undo now: agency channel {committed}")
        return
    commit(f"Follow {target} nixpkgs", "settings.nix", "flake.lock")


def cmd_rollback(args):
    say("Switching back to the previous system ...")
    run(["sudo", tool("nixos-rebuild"), "switch", "--rollback"])
    say("Done. /etc/nixos did not change: revert the commit that broke things,")
    say("or the next rebuild brings the problem back.")


def catalog():
    result = run(["nix", "eval", "--json", f"path:{REPO}#appCatalog"], capture=True)
    return json.loads(result.stdout)


def npm_apps(by_id, adds, removes):
    """Install and remove the npm-registry apps among adds and removes;
    return the ids that failed to install. Bun installs them; npm is only
    used to remove copies it installed before Bun was the installer."""
    # Set the locations here too: outside a login shell (the dashboard, a
    # service) Bun and npm would otherwise write somewhere else.
    bun_home = Path.home() / ".bun"
    prefix = Path.home() / ".npm-global"
    env = dict(os.environ, BUN_INSTALL=str(bun_home), NPM_CONFIG_PREFIX=str(prefix))
    env["PATH"] = f"{bun_home / 'bin'}:{prefix / 'bin'}:{env.get('PATH', '')}"
    failed = []
    for app_id in removes:
        app = by_id[app_id]
        if app["kind"] == "npm":
            say(f"Removing {app['label']} ...")
            if app.get("teardown"):
                run(["sh", "-c", app["teardown"]], env=env, check=False)
            if (bun_home / "install/global/node_modules" / app["npm"]).exists():
                run(["bun", "remove", "-g", app["npm"]], env=env, check=False)
            if (prefix / "lib/node_modules" / app["npm"]).exists():
                run(["npm", "uninstall", "-g", app["npm"]], env=env, check=False)
    for app_id in adds:
        app = by_id[app_id]
        if app["kind"] == "npm":
            say(f"Installing {app['label']} ...")
            ok = run(["bun", "add", "-g", "--trust", app["npm"]], env=env, check=False).returncode == 0
            if ok and app.get("setup"):
                ok = run(["sh", "-c", app["setup"]], env=env, check=False).returncode == 0
            if not ok:
                failed.append(app_id)
            elif app.get("signin"):
                say(f"To sign {app['label']} in, run: {app['signin']}")
    return failed


def cmd_apps(args):
    apps = catalog()
    chosen = setting_list("apps")
    if args.action == "list":
        if args.json:
            print(json.dumps([dict(app, on=app["id"] in chosen) for app in apps]))
            return
        for app in apps:
            mark = "*" if app["id"] in chosen else " "
            say(f" {mark} {app['id']:<14} {app['label']}")
        say("\n* is on. Change with: agency apps add|remove <id>...")
        return

    if args.action == "change":
        adds, removes = args.add or [], args.remove or []
    elif args.action == "add":
        adds, removes = args.ids + (args.add or []), args.remove or []
    else:
        adds, removes = args.add or [], args.ids + (args.remove or [])
    adds, removes = list(dict.fromkeys(adds)), list(dict.fromkeys(removes))
    if not adds and not removes:
        die("which app? See: agency apps")
    by_id = {app["id"]: app for app in apps}
    for app_id in adds + removes:
        if app_id not in by_id:
            die(f"no app called {app_id}. See: agency apps")
    if set(adds) & set(removes):
        die("an app can't be added and removed at once")
    # Already in the state asked for: say so and skip it, so a batch from
    # the dashboard still does the rest.
    for app_id in [a for a in adds if a in chosen]:
        say(f"{app_id} is already on.")
    for app_id in [r for r in removes if r not in chosen]:
        say(f"{app_id} is already off.")
    adds = [a for a in adds if a not in chosen]
    removes = [r for r in removes if r in chosen]
    if not adds and not removes:
        return

    ensure_repo()
    before = SETTINGS.read_text()
    chosen = [c for c in chosen if c not in removes] + adds
    write_setting("apps", nix_list(chosen))

    # System apps first, in one rebuild. If it fails, settings.nix goes back
    # to how it was, so no app is recorded as on that is not installed.
    if any(by_id[a]["kind"] != "npm" for a in adds + removes):
        try:
            rebuild()
        except SystemExit:
            SETTINGS.write_text(before)
            die("nothing was changed: the rebuild failed (see above).")

    # Flathub apps: the rebuild starts agency-flatpak-apps without waiting
    # for it. Run it again and wait, so a download that fails is reported.
    if any(by_id[a]["kind"] == "flatpak" for a in adds + removes):
        say("Installing and removing Flathub apps ...")
        if run(["sudo", tool("systemctl"), "restart", "agency-flatpak-apps"], check=False).returncode != 0:
            commit(f"Apps: {', '.join(adds + removes)}", "settings.nix")
            die("the Flathub download failed. See: journalctl -u agency-flatpak-apps\n"
                "It tries again at the next boot, or now with: sudo systemctl restart agency-flatpak-apps")

    # Then agent CLIs, installed with Bun into this account. One that fails
    # to install is left off.
    failed = npm_apps(by_id, adds, removes)
    if failed:
        chosen = [c for c in chosen if c not in failed]
        write_setting("apps", nix_list(chosen))

    done = [a for a in adds if a not in failed]
    # npm apps said this as they installed (npm_apps); the rest say it here.
    for app_id in done:
        app = by_id[app_id]
        if app["kind"] != "npm" and app.get("signin"):
            say(f"To sign {app['label']} in, run: {app['signin']}")
    parts = ([f"add {', '.join(done)}"] if done else []) + ([f"remove {', '.join(removes)}"] if removes else [])
    if parts:
        commit(f"Apps: {'; '.join(parts)}", "settings.nix")
    if failed:
        die(f"could not install {', '.join(by_id[a]['label'] for a in failed)} (see above); "
            "it is left off.")
    say(f"Done: {', '.join(by_id[a]['label'] for a in adds + removes)}.")


def cmd_desktop(args):
    count = desktop_count()
    if args.action == "list":
        for n in range(1, count + 1):
            state = output(["systemctl", "is-active", f"agency-display@{n}.service"]) or "stopped"
            say(f"  :{n}  {desktop_url(n)}   {state}")
        return
    ensure_repo()
    if args.action == "add":
        count += 1
        write_setting("desktops", str(count))
        commit(f"Add desktop :{count}", "settings.nix")
        rebuild()
        say(f"Desktop :{count} is ready. Use it with: gui -d {count} <app>   or   DISPLAY=:{count}")
        say(f"Watch it at {desktop_url(count)}")
    else:
        if count == 1:
            die("desktop :1 always stays")
        write_setting("desktops", str(count - 1))
        commit(f"Remove desktop :{count}", "settings.nix")
        rebuild()
        say(f"Removed desktop :{count}.")


def cmd_screen(args):
    if args.action == "show":
        url = (args.url or "").strip()
        if not re.match(r"^https?://\S+$", url):
            die("usage: agency screen show http://...   (an http or https address)")
        run(["sudo", "tee", SCREEN_URL], stdin=url + "\n", capture=True)
        say(f"The screen shows {url} within ten seconds. `agency screen reset` brings the status screen back.")
    elif args.action == "reset":
        run(["sudo", "rm", "-f", SCREEN_URL])
        say("The screen shows the status screen again within ten seconds.")
    else:
        url = root_read(SCREEN_URL).strip()
        say(f"The screen shows {url}" if url else "The screen shows the status screen.")


def cmd_recovery_key(args):
    if run(["sudo", "test", "-e", RECOVERY_KEY], check=False).returncode != 0:
        say("The recovery key is no longer on this machine. If it is lost, make a new one")
        say("(this also removes the old one):")
        say("  sudo systemd-cryptenroll --wipe-slot=recovery --recovery-key <the LUKS partition>")
        return
    if not sys.stdin.isatty():
        die("only a person should see the recovery key. Run this yourself over SSH.")
    key = run(["sudo", "cat", RECOVERY_KEY], capture=True).stdout.strip()
    say("Disk recovery key. If the TPM stops unlocking the disk (a BIOS update, a new")
    say("motherboard), this is the only way in. Store it in your password manager.\n")
    say(f"  {key}\n")
    run(["qrencode", "-t", "UTF8", "-m", "2", key], check=False)
    if ask('Type "saved" once it is stored, and agency deletes it from this disk: ').lower() == "saved":
        run(["sudo", "shred", "-u", RECOVERY_KEY])
        say("Deleted.")
    else:
        say("Kept. Run `agency recovery-key` again when it is stored.")


# ------------------------------------------------------------- backups ---
# Hourly restic backups of the home folder (modules/backup.nix). restic runs
# as root through `restic-home`, the wrapper NixOS makes with the
# repository, password and keys already set.


def backups_on():
    return output(["systemctl", "show", "-p", "LoadState", "--value", BACKUP_UNIT]) == "loaded"


def backup_remote():
    repo = setting_string("backupRepo")
    return bool(repo) and not repo.startswith("/")


def ago(seconds):
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def backup_status():
    if not backups_on():
        return "off (backupRepo = null in settings.nix)"
    where = setting_string("backupRepo") or "this disk"
    if output(["systemctl", "is-active", BACKUP_UNIT]) in ("active", "activating"):
        last = "running now"
    else:
        try:
            last = ago(time.time() - int(BACKUP_LAST.read_text()))
        except (OSError, ValueError):
            last = "none yet"
    return f"hourly to {where}; last: {last}"


def restic(*args, capture=True, check=True):
    return run(["sudo", tool("restic-home"), *args], capture=capture, check=check)


def snapshots():
    """Oldest first."""
    # The password, and with it the repository, comes with the first backup.
    if not BACKUP_PASSWORD.exists():
        return []
    found = json.loads(restic("snapshots", "--json", "--no-lock", "--host", host()).stdout or "[]")
    for snap in found:
        # "2026-11-15T10:00:01.123456789-05:00": restic gives nanoseconds,
        # Python reads at most microseconds.
        stamp = re.sub(r"(\.\d{6})\d+", r"\1", snap["time"]).replace("Z", "+00:00")
        when = datetime.fromisoformat(stamp)
        snap["when"] = when.timestamp()
        snap["label"] = when.astimezone().strftime("%Y-%m-%d %H:%M")
    return sorted(found, key=lambda s: s["when"])


AGE = re.compile(r"^(\d+)([mhdw])$")


def pick_snapshot(spec):
    """latest, a snapshot ID, or an age like 2h: the newest one at least that old."""
    found = snapshots()
    if not found:
        die("there are no backups yet. Make one with: agency backup now")
    if spec in (None, "latest"):
        return found[-1]
    match = AGE.match(spec)
    if match:
        seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        cutoff = time.time() - seconds
        older = [s for s in found if s["when"] <= cutoff]
        if not older:
            die(f"no backup is that old. The oldest is from {found[0]['label']}.")
        return older[-1]
    matches = [s for s in found if s["id"].startswith(spec)]
    if len(matches) != 1:
        die(f"{spec}: not a snapshot ID (see `agency backup`) or an age like 30m, 2h, 3d, 1w")
    return matches[0]


def cmd_backup(args):
    if not backups_on():
        die("backups are off. Set backupRepo in settings.nix (see modules/backup.nix) and rebuild.")

    if args.action == "now":
        say("Backing up the home folder ...")
        if run(["sudo", "systemctl", "start", BACKUP_UNIT], check=False).returncode != 0:
            die("the backup failed: journalctl -u restic-backups-home")
        say("Done.")
        return

    if args.action == "password":
        password = run(["sudo", "cat", BACKUP_PASSWORD], check=False, capture=True).stdout.strip()
        if not password:
            die("there is no password yet; it is made with the first backup: agency backup now")
        if not sys.stdin.isatty():
            die("only a person should see the backup password. Run this yourself over SSH.")
        say("Backup password. Without it no one can read the backups, so if this machine")
        say("dies, it is the only way to restore them elsewhere. Store it in your password manager.\n")
        say(f"  {password}\n")
        say(f"  repository: {setting_string('backupRepo') or '/var/lib/agency/backup (this disk)'}\n")
        if ask('Type "saved" once it is stored: ').lower() == "saved":
            run(["sudo", "touch", BACKUP_PASSWORD_SAVED])
        return

    if args.action == "restore":
        return backup_restore(args)

    # list
    say(backup_status())
    found = snapshots()
    if not found:
        say("No backups yet. Make one with: agency backup now")
        return
    say("")
    for snap in reversed(found):
        size = snap.get("summary", {}).get("data_added")
        added = f"   +{size_text(size)}" if size is not None else ""
        say(f"  {snap['short_id']}  {snap['label']}  {ago(time.time() - snap['when'])}{added}")
    say("\nBring something back with: agency backup restore PATH [--from 2h]")


def backup_restore(args):
    if not args.path:
        die("usage: agency backup restore PATH [--from latest|ID|2h] [--to DEST]")
    path = Path(os.path.abspath(os.path.expanduser(args.path)))
    home = Path(f"/home/{USER}")
    if path != home and home not in path.parents:
        die(f"only the home folder is backed up, and {path} is not in it")
    snap = pick_snapshot(args.source)

    listing = restic("ls", "--json", "--no-lock", snap["id"], path).stdout.splitlines()
    node = None
    for line in listing:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("struct_type", "node") == "node" and entry.get("path") == str(path):
            node = entry
    if node is None:
        die(f"{path} is not in the backup from {snap['label']}")

    if args.to:
        dest = Path(os.path.abspath(os.path.expanduser(args.to)))
    elif not path.exists() and not path.is_symlink():
        dest = path
    else:
        dest = path.with_name(f"{path.name}.restored-{snap['label'].replace(' ', '-').replace(':', '')}")
    if dest.exists() or dest.is_symlink():
        die(f"{dest} is already there. Move it, or name another place with --to")
    dest.parent.mkdir(parents=True, exist_ok=True)

    say(f"Restoring {path} from {snap['label']} ({snap['short_id']}) to {dest} ...")
    if node["type"] == "dir":
        restic("restore", "--no-lock", f"{snap['id']}:{path}", "--target", dest, capture=False)
        run(["sudo", "chown", f"{USER}:", dest])
    else:
        with open(dest, "wb") as f:
            result = subprocess.run(
                ["sudo", tool("restic-home"), "dump", "--no-lock", snap["id"], str(path)], stdout=f
            )
        if result.returncode != 0:
            dest.unlink(missing_ok=True)
            die("the restore failed")
    if "mode" in node:
        os.chmod(dest, node["mode"] & 0o7777)
    say("Done.")


# ------------------------------------------------------------- secrets ---
# Secrets live encrypted in secrets/agency.sops.json, so they can be
# committed and pushed. The machine's SSH host key decrypts them at boot
# (sops-nix, see modules/secrets.nix); the ed25519 keys that can log in can
# decrypt them too, so a copy of the repo is never the only way back.


def secret_names():
    if not SECRETS.exists():
        return []
    return sorted(k for k in json.loads(SECRETS.read_text()) if k != "sops")


def recipients():
    keys = [output(["ssh-to-age", "-i", f"{HOST_KEY}.pub"])]
    if AUTHORIZED_KEYS.exists():
        for line in AUTHORIZED_KEYS.read_text().splitlines():
            if line.startswith("ssh-ed25519 "):
                result = run(["ssh-to-age"], stdin=line + "\n", check=False, capture=True)
                keys.append(result.stdout.strip() if result.returncode == 0 else "")
    keys = list(dict.fromkeys(k for k in keys if k.startswith("age1")))
    if not keys:
        die(f"no age key: is {HOST_KEY}.pub there?")
    return keys


def sops_env():
    identity = run(["sudo", tool("ssh-to-age"), "-private-key", "-i", HOST_KEY], capture=True)
    return dict(os.environ, SOPS_AGE_KEY=identity.stdout.strip())


def load_secrets():
    if not SECRETS.exists():
        return {}
    result = run(
        ["sops", "decrypt", "--input-type", "json", "--output-type", "json", SECRETS],
        env=sops_env(),
        capture=True,
    )
    data = json.loads(result.stdout)
    data.pop("sops", None)
    return data


def save_secrets(data):
    SOPS_CONFIG.write_text(
        "# Who can read secrets/: this machine's SSH host key and the ed25519 keys\n"
        "# that can log in. Written by `agency secret`.\n"
        "creation_rules:\n"
        "  - path_regex: secrets/.*\\.sops\\.json$\n"
        f"    age: {','.join(recipients())}\n"
    )
    if not data:
        SECRETS.unlink(missing_ok=True)
        return
    result = run(
        [
            "sops", "encrypt",
            "--input-type", "json", "--output-type", "json",
            "--filename-override", SECRETS.relative_to(REPO),
            "/dev/stdin",
        ],
        stdin=json.dumps(data),
        cwd=REPO,
        capture=True,
    )
    SECRETS.parent.mkdir(exist_ok=True)
    SECRETS.write_text(result.stdout)


def cmd_secret(args):
    if args.action == "list":
        names = secret_names()
        say("\n".join(names) if names else "No secrets yet. Add one with: agency secret set NAME")
        return
    if args.action == "get":
        data = load_secrets()
        if args.name not in data:
            die(f"no secret called {args.name}")
        print(data[args.name])
        return

    if not SECRET_NAME.match(args.name):
        die("use a name like OPENAI_API_KEY: capitals, digits and _")
    ensure_repo()
    data = load_secrets()
    if args.action == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"Value for {args.name} (not shown): ")
        else:
            value = sys.stdin.read().rstrip("\n")
        if not value:
            die("empty value, nothing changed")
        data[args.name] = value
        message = f"Set secret {args.name}"
    else:
        if data.pop(args.name, None) is None:
            die(f"no secret called {args.name}")
        message = f"Remove secret {args.name}"
    save_secrets(data)
    commit(message, ".sops.yaml", "secrets")
    if args.no_rebuild:
        say("Saved. It reaches /run/secrets after the next `agency rebuild`.")
        return
    rebuild()
    if args.action == "set":
        say(f"{args.name} is in /run/secrets/{args.name}, and in ${args.name} in new shells.")


# ------------------------------------------------------ export / import ---
# `agency export` puts this machine's setup in one file, encrypted with a
# passphrase; `agency import` on another machine sets that one up the same
# way. What makes a machine itself stays behind: its name, its disks and
# hardware (hosts/), its SSH host key, its Tailscale node and its disk
# encryption. The new machine keeps its own, so both can run side by side.
#
# The file is a zstd tar, encrypted with age (a passphrase, scrypt):
#   agency/manifest.json   what is in it, and where it came from
#   agency/settings.nix    this machine's settings.nix
#   agency/changes.diff    local changes to /etc/nixos (not settings, secrets or hosts/)
#   agency/secrets.json    the secrets, decrypted (the file itself is encrypted)
#   home/...               the agent's home folder, if asked for

EXPORT_FORMAT = 1
# The settings.nix lines that belong to the machine, not to its setup. An
# import keeps the new machine's own.
OWN_SETTINGS = ("hostName", "user", "unattended", "disk", "vendorMatch")
# Left out of changes.diff: settings and secrets travel on their own, and
# hosts/ describes this machine's disks and hardware.
REPO_LEFT_OUT = ("settings.nix", ".sops.yaml", "secrets", "hosts")
# Left out of the home folder: caches, what apps reinstall, and what names
# this machine (T3's environment id, Chromium's lock on this host name).
HOME_LEFT_OUT = [
    ".cache", ".npm", ".npm-global", ".bun", ".nix-profile", ".local/state/nix",
    ".local/share/Trash", ".t3", ".local/state/agency/t3-connected", ".dbus",
    "nixos-git-unlinked-*", "nixos-git-broken-*", ".config/chromium/Singleton*",
]
HOME_LEFT_OUT_ANYWHERE = ["node_modules"]
MIN_PASSPHRASE = 10


def read_passphrase(confirm):
    """From the keyboard, or the first line of stdin (the dashboard)."""
    if sys.stdin.isatty():
        value = getpass.getpass("Passphrase (not shown): ")
        if confirm and getpass.getpass("The same again: ") != value:
            die("the two passphrases differ")
    else:
        value = sys.stdin.readline().rstrip("\n")
    if not value:
        die("no passphrase")
    return value


def age(passphrase, *args, **kw):
    """Start age with the passphrase on a pipe, never in argv or the environment."""
    r, w = os.pipe()
    os.write(w, passphrase.encode())
    os.close(w)
    env = dict(os.environ, AGE_PASSPHRASE_FD=str(r))
    try:
        return subprocess.Popen([tool("age"), "-j", "batchpass", *args], env=env, pass_fds=(r,), **kw)
    finally:
        os.close(r)


def stage_dir():
    """A private folder in RAM for the small, secret parts."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(tempfile.mkdtemp(prefix="agency-transfer-", dir=base if os.path.isdir(base) else None))


def repo_changes():
    """Committed local changes to /etc/nixos, as a diff from the upstream
    commit they sit on, without the parts that do not travel."""
    if not (REPO / ".git").is_dir():
        return "", ""
    base = output(["git", "-C", REPO, "merge-base", "HEAD", "@{u}"])
    if not base:
        return "", ""
    left_out = [f":(exclude){p}" for p in REPO_LEFT_OUT]
    diff = run(["git", "-C", REPO, "diff", "--binary", base, "HEAD", "--", ".", *left_out], capture=True).stdout
    return diff, base


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def size_text(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000


def cmd_export(args):
    passphrase = read_passphrase(confirm=True)
    if len(passphrase) < MIN_PASSPHRASE:
        die(f"use a passphrase of at least {MIN_PASSPHRASE} characters")
    out = Path(args.output or f"agency-{host()}-{time.strftime('%Y%m%d')}.agency").expanduser().resolve()
    if out.is_dir():
        die(f"{out} is a folder; name a file")
    partial = out.with_name(out.name + ".part")
    home = Path.home()

    if uncommitted():
        say("Note: /etc/nixos has uncommitted changes. They are not in the export; commit them first to include them.")
    stage = stage_dir()
    try:
        part = stage / "agency"
        part.mkdir()
        (part / "settings.nix").write_text(SETTINGS.read_text())
        diff, base = repo_changes()
        (part / "changes.diff").write_text(diff)
        secrets = load_secrets()
        write_private(part / "secrets.json", json.dumps(secrets))
        (part / "manifest.json").write_text(json.dumps({
            "format": EXPORT_FORMAT,
            "from": host(),
            "made": int(time.time()),
            "user": USER,
            "base": base,
            "secrets": sorted(secrets),
            "home": bool(args.home),
        }, indent=2))

        cmd = ["tar", "--zstd", "-cf", "-", "--transform", r"s,^\./,home/,;s,^\.$,home,"]
        if args.home:
            cmd += ["--ignore-failed-read", "--warning=no-failed-read", "--warning=no-file-changed",
                    "--warning=no-file-removed", "--warning=no-file-ignored", "--anchored"]
            for name in HOME_LEFT_OUT:
                cmd.append(f"--exclude=./{name}")
            for p in (out, partial):
                if p.is_relative_to(home):
                    cmd.append(f"--exclude=./{p.relative_to(home)}")
            cmd.append("--no-anchored")
            cmd += [f"--exclude={name}" for name in HOME_LEFT_OUT_ANYWHERE]
        cmd += ["-C", stage, "agency"]
        if args.home:
            cmd += ["-C", home, "."]

        say(f"Exporting this machine{' and its home folder' if args.home else ''} ...")
        if args.home:
            say("  The home folder can take a while. Left out: " + ", ".join(HOME_LEFT_OUT[:6]) + " and other caches.")
        partial.unlink(missing_ok=True)
        tar = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        enc = age(passphrase, "-e", "-o", partial, stdin=tar.stdout)
        tar.stdout.close()
        enc_code, tar_code = enc.wait(), tar.wait()
        # tar's 1 means a file changed while it was read: the export is still good.
        if enc_code != 0 or tar_code not in (0, 1):
            partial.unlink(missing_ok=True)
            die("the export failed (see above)")
        os.chmod(partial, 0o600)
        partial.rename(out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    say(f"Done: {out} ({size_text(out.stat().st_size)}).")
    say(f"  In it: settings and apps, {len(secrets)} secret(s)"
        + (", changes to /etc/nixos" if diff else "")
        + (", the home folder" if args.home else "") + ".")
    say("  Not in it: the name, disks and hardware, the SSH host key, the Tailscale login.")
    say("  On the other machine: agency import <file>, or Settings in the dashboard.")


def unpack(src, passphrase, dest, members, strip=0):
    """Decrypt src and extract members into dest. Say why if it fails."""
    dec = age(passphrase, "-d", src, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    cmd = ["tar", "--zstd", "-xpf", "-", "-C", dest]
    if strip:
        cmd.append(f"--strip-components={strip}")
    tar = subprocess.Popen([*cmd, *members], stdin=dec.stdout, stderr=subprocess.PIPE, text=True)
    dec.stdout.close()
    tar_err = tar.communicate()[1]
    dec_err = dec.stderr.read().decode(errors="replace")
    dec_code = dec.wait()
    if dec_code != 0:
        if "incorrect passphrase" in dec_err:
            die("that passphrase does not open this file")
        if "header" in dec_err or "no identity matched" in dec_err or "batchpass" in dec_err:
            die("this is not an agency export, or it is damaged")
        die(f"could not decrypt it: {dec_err.strip().splitlines()[0] if dec_err.strip() else 'age failed'}")
    if tar.returncode != 0:
        sys.stderr.write(tar_err)
        die("could not unpack it (see above)")


def merged_settings(theirs, ours):
    """Their settings.nix, with this machine's own lines kept."""
    for key in OWN_SETTINGS:
        mine = re.search(rf"^  {key} = .*;$", ours, re.M)
        if mine:
            theirs = re.sub(rf"^  {key} = .*;$", lambda _: mine.group(0), theirs, count=1, flags=re.M)
    return theirs


def cmd_import(args):
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"no file at {src}")
    passphrase = read_passphrase(confirm=False)
    ensure_repo()
    if uncommitted():
        commit("Local changes, saved before an import", ".")
        say("Committed the local changes in /etc/nixos first.")

    stage = stage_dir()
    try:
        say("Decrypting ...")
        unpack(src, passphrase, stage, ["agency"])
        part = stage / "agency"
        try:
            manifest = json.loads((part / "manifest.json").read_text())
        except (OSError, ValueError):
            die("this is not an agency export")
        if manifest.get("format") != EXPORT_FORMAT:
            die("this export was made by a different version of agency. Run `agency update` on both machines.")
        say(f"An export of {manifest.get('from', '?')}, made {time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest.get('made', 0)))}.")

        before = output(["git", "-C", REPO, "rev-parse", "HEAD"])
        apps_before = setting_list("apps")

        def undo(why):
            git("reset", "-q", "--hard", before, check=False)
            die(f"nothing was changed: {why}")

        # Settings: theirs, but this machine keeps its name, account and disks.
        SETTINGS.write_text(merged_settings((part / "settings.nix").read_text(), SETTINGS.read_text()))

        # Their local changes to /etc/nixos, onto ours.
        diff = part / "changes.diff"
        if diff.stat().st_size:
            git("fetch", "-q", "origin", check=False)
            if git("apply", "--3way", "--whitespace=nowarn", diff, check=False).returncode != 0:
                undo("their changes to /etc/nixos do not fit onto this machine's (see above)")
            say("Applied their changes to /etc/nixos.")

        # Secrets: theirs are added to ours, and win where both have one.
        theirs = json.loads((part / "secrets.json").read_text())
        data = load_secrets()
        if any(data.get(k) != v for k, v in theirs.items()):
            data.update(theirs)
            save_secrets(data)
        if theirs:
            say(f"Secrets: {', '.join(sorted(theirs))}.")

        commit(f"Import the setup of {manifest.get('from', 'another machine')}", ".")
        try:
            rebuild()
        except SystemExit:
            undo("the rebuild failed (see above)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # Agent CLIs come from Bun, not the rebuild.
    apps_now = setting_list("apps")
    adds = [a for a in apps_now if a not in apps_before]
    removes = [a for a in apps_before if a not in apps_now]
    by_id = {app["id"]: app for app in catalog()}
    failed = npm_apps(by_id, [a for a in adds if a in by_id], [r for r in removes if r in by_id])
    if failed:
        write_setting("apps", nix_list([a for a in apps_now if a not in failed]))
        commit(f"Apps: could not install {', '.join(failed)}", "settings.nix")
        say(f"Could not install {', '.join(failed)}; it is left off.")

    if manifest.get("home") and not args.no_home:
        if manifest.get("user") != USER:
            say(f"Skipped the home folder: it is {manifest.get('user')}'s, and this machine's account is {USER}.")
        else:
            say("Restoring the home folder (files with the same name are replaced) ...")
            unpack(src, passphrase, Path.home(), ["home"], strip=1)
            say("Restored. Open a new shell to pick it up.")

    say(f"Done. This machine is set up like {manifest.get('from', 'the other one')}, and keeps its own name ({host()}).")
    say("  Sign-ins that are not in the home folder (and Tailscale) are this machine's own.")


def cmd_setup_repo(args):
    ensure_repo()


# ---------------------------------------------------------------- main ---


def main():
    if os.geteuid() == 0:
        # Run as the agent account so files in /etc/nixos stay theirs.
        os.execvp("runuser", ["runuser", "-u", USER, "--", "agency", *sys.argv[1:]])

    parser = argparse.ArgumentParser(
        prog="agency",
        description="Look after this agency machine. With no command: a summary.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("status", help="summary of this machine (the default)")

    p = sub.add_parser("update", help="pull the newest agency from `repo` in settings.nix and rebuild")
    p.add_argument("--packages", action="store_true", help="also update nixpkgs and other inputs")

    p = sub.add_parser("rebuild", help="apply the files in /etc/nixos")
    p.add_argument("--test", action="store_true", help="only until the next reboot (a safe trial)")

    sub.add_parser("rollback", help="go back to the previous system")
    sub.add_parser("auto-update", help="update nixpkgs and rebuild (what the weekly timer runs)")

    p = sub.add_parser("channel", help="show or switch the nixpkgs this machine follows: stable or unstable")
    p.add_argument("channel", nargs="?", choices=["stable", "unstable"])
    p.add_argument("--test", action="store_true", help="only until the next reboot (a safe trial)")

    p = sub.add_parser("apps", help="list, add or remove optional software")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "change"])
    p.add_argument("ids", nargs="*", metavar="id")
    p.add_argument("--add", action="append", metavar="ID", help="with change: an app to add (repeat for more)")
    p.add_argument("--remove", action="append", metavar="ID", help="with change: an app to remove (repeat for more)")
    p.add_argument("--json", action="store_true", help="list as JSON, for the dashboard")

    p = sub.add_parser("desktop", help="list, add or remove virtual desktops")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])

    p = sub.add_parser("secret", help="encrypted secrets that agents can read")
    p.add_argument("action", nargs="?", default="list", choices=["list", "set", "get", "remove"])
    p.add_argument("name", nargs="?")
    p.add_argument("--no-rebuild", action="store_true", help="save without applying yet")

    p = sub.add_parser("export", help="save this machine's setup to one encrypted file, for `agency import`")
    p.add_argument("-o", "--output", metavar="FILE", help="where to write it (default: agency-<name>-<date>.agency here)")
    p.add_argument("--home", action="store_true", help="also the home folder: sign-ins, repos, dotfiles")

    p = sub.add_parser("import", help="set this machine up like the one in an export (keeps its own name)")
    p.add_argument("file")
    p.add_argument("--no-home", action="store_true", help="leave the home folder as it is, even if the export has one")

    p = sub.add_parser("backup", help="hourly backups of the home folder: list, now, restore, password")
    p.add_argument("action", nargs="?", default="list", choices=["list", "now", "restore", "password"])
    p.add_argument("path", nargs="?", help="with restore: the file or folder to bring back")
    p.add_argument("--from", dest="source", metavar="WHEN", help="with restore: latest (default), a snapshot ID, or an age like 2h or 3d")
    p.add_argument("--to", metavar="DEST", help="with restore: where to put it (default: its own place if it is gone, else next to it)")

    p = sub.add_parser("screen", help="show a web page on the laptop's own display, or the status screen")
    p.add_argument("action", nargs="?", default="status", choices=["status", "show", "reset"])
    p.add_argument("url", nargs="?", help="with show: the page, e.g. http://127.0.0.1:8080/")

    sub.add_parser("recovery-key", help="show the disk recovery key once, then delete it")
    sub.add_parser("setup-repo", help=argparse.SUPPRESS)
    sub.add_parser("help", help="show this help")

    args = parser.parse_args()
    if args.command == "help":
        parser.print_help()
        return
    if args.command == "secret" and args.action != "list" and not args.name:
        die(f"usage: agency secret {args.action} NAME")

    handlers = {
        None: cmd_status,
        "status": cmd_status,
        "update": cmd_update,
        "auto-update": cmd_auto_update,
        "channel": cmd_channel,
        "rebuild": cmd_rebuild,
        "rollback": cmd_rollback,
        "apps": cmd_apps,
        "desktop": cmd_desktop,
        "secret": cmd_secret,
        "export": cmd_export,
        "import": cmd_import,
        "backup": cmd_backup,
        "screen": cmd_screen,
        "recovery-key": cmd_recovery_key,
        "setup-repo": cmd_setup_repo,
    }
    handlers[args.command](args)
    if args.command is None:
        say("\nCommands: update, rebuild, rollback, channel, apps, desktop, screen, secret, backup, export, import, recovery-key. See: agency help")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
