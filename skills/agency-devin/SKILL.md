---
name: agency-devin
description: Hand a task to Devin and let it work on this agency machine over SSH. Use when a person asks for Devin, or a long task should run while you do something else.
---

# Devin

`agency devin` starts a Devin session whose workspace is this machine, not
Devin's own VM. Devin joins the tailnet with an ephemeral key, then logs in
as `agent` with an SSH key made for that session alone.

```sh
agency devin run "fix the failing nix build on unstable"   # start one
agency devin run --acu-limit 10 "..."                      # with a budget
agency devin                                               # sessions and status
agency devin revoke SESSION_ID                             # remove its key
agency devin revoke all
```

It needs two secrets, which a person stores with `agency secret set`:

- `DEVIN_API_KEY`: from Devin's settings, under API keys.
- `DEVIN_TAILSCALE_AUTHKEY`: an ephemeral, reusable key from the Tailscale
  admin console, so Devin's VM leaves the tailnet when it shuts down.

Because Devin works here, what it builds stays between sessions and is in
the hourly backups. Revoke a session's key when it is done with; a key left
in place lets that session log in again.
