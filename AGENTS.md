# AGENTS.md

**agency** is a NixOS laptop used only by AI agents. You are `agent`, with passwordless `sudo`.

## Changing the system

The system is the git repo in `/etc/nixos`. Edit it, then:

```sh
agency rebuild          # apply
agency rebuild --test   # try until reboot
agency rollback         # undo
```

Don't edit `/etc` by hand. Commit what you change. Test risky changes (boot, network, SSH) with `--test` first. `agency help` lists all commands.

## No rebuild needed

Your home directory is yours: `bun add -g` (use Bun over npm where you can; `npm install -g` works too), `uv`, `pip install --user`, `nix shell nixpkgs#<pkg>`, `docker`. Downloaded binaries run as-is.

`agency apps add|remove <id>` switches optional software (catalog: `apps.nix`).

`agency screen show <url>` puts a web page on the laptop's own display, for example one you serve on `127.0.0.1`; `agency screen reset` brings back the status screen.

When you add an app to the catalog, add its logo as `dashboard/ui/logos/<id>.svg`. Take it from [svgl](https://svgl.app) first (its API blocks scripts, so fetch from `static/library/` in the `pheralb/svgl` GitHub repo), else Simple Icons or the project itself, and note the source in `dashboard/ui/logos/README.md`.

## Desktops

GUI apps run on virtual desktops (`:1`, `:2`…). Start one with `gui chromium`. Humans watch them at the addresses `agency desktop list` prints. See the `agency-desktop` skill.

## Backups

The home folder is backed up every hour (restic). If you delete or break something, `agency backup` lists the backups and `agency backup restore PATH --from 2h` brings a file or folder back. It never overwrites: a path that still exists comes back next to it.

## Secrets

Never commit secrets. Use `agency secret set NAME`; it appears as `$NAME`. Don't read `/root/luks-recovery-key.txt`.

## Defaults

Claude Code starts with Remote Control on. To turn it off, set `remoteControlAtStartup` to `false` in `~/.claude/settings.json`.

## To do

1. Secure Boot (lanzaboote, then re-enrol the TPM).
2. Nvidia, only if GPU compute is needed.
