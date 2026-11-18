# Devin on agency

Devin normally works in a fresh cloud VM. `agency devin` gives it this
machine instead: a real computer with root, state that stays between
sessions, hourly backups and desktops you can watch.

## Set up

1. In Devin, make an API key. On the machine:

   ```sh
   agency secret set DEVIN_API_KEY
   ```

2. In the Tailscale admin console, make an auth key that is **ephemeral**
   and **reusable**. Ephemeral means each Devin VM drops off the tailnet
   when it shuts down.

   ```sh
   agency secret set DEVIN_TAILSCALE_AUTHKEY
   ```

## Use

```sh
agency devin run "upgrade the dashboard to the new nixpkgs and fix what breaks"
```

This makes an SSH key for that session only, adds it to
`~/.ssh/authorized_keys`, and starts a Devin session through Devin's API
with the key and the Tailscale key as session secrets. Devin's prompt tells
it to join the tailnet, log in as `agent@<hostName>`, and read
`/etc/nixos/AGENTS.md` before changing anything.

```sh
agency devin                        # each session and its status
agency devin revoke <session-id>    # take its key away
```

## How it is kept safe

- No port is opened. Devin reaches the machine only over the tailnet.
- Each session has its own key, tagged `agency-devin <time>` in
  `authorized_keys`, so revoking one leaves the others and your own keys.
- Keys travel as Devin session secrets, never in the prompt.
- If Devin breaks something, `agency rollback` and
  `agency backup restore PATH --from 2h` undo it, as for any agent.

## Not done yet

- Revoking a session's key when Devin's session finishes, without being asked.
- A dashboard page for Devin sessions.
- Tailscale ACLs that let Devin's tag reach only this machine, and only SSH.
