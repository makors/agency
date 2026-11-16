# Lets the dashboard and the status screen show what each agent session is
# doing: working, waiting for a person, or idle (`agency agents`).
#
# Claude Code and Codex report through hooks that run
# `agency agents hook claude|codex` with each event. They pass on which tool
# ran and its stated purpose, never prompts, commands or file contents.
# Both CLIs read these system-wide files besides a person's own settings,
# so hooks a person adds still run:
#
#   Claude Code   /etc/claude-code/managed-settings.json
#   Codex         /etc/codex/hooks.json (system hooks need no trust prompt)
#
# Other agents report with `agency agents set NAME working|waiting|idle`.
{ lib, ... }:
let
  hooks =
    tool: events:
    lib.genAttrs events (_: [
      {
        hooks = [
          {
            type = "command";
            command = "/run/current-system/sw/bin/agency agents hook ${tool}";
            # Codex allows at most 3 seconds; a report takes about 0.1.
            timeout = 3;
          }
        ];
      }
    ]);
in
{
  environment.etc."claude-code/managed-settings.json".text = builtins.toJSON {
    hooks = hooks "claude" [
      "SessionStart"
      "UserPromptSubmit"
      "PreToolUse"
      "PostToolUse"
      "PermissionRequest"
      "Notification"
      "Stop"
      "SessionEnd"
    ];
  };

  environment.etc."codex/hooks.json".text = builtins.toJSON {
    hooks = hooks "codex" [
      "SessionStart"
      "UserPromptSubmit"
      "PreToolUse"
      "PostToolUse"
      "PermissionRequest"
      "Stop"
      "SessionEnd"
    ];
  };
}
