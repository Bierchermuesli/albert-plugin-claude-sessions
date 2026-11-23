# Claude Sessions

Albert plugin to find Claude Code sessions across all project directories and get back into them.

- `cs <words>` searches titles, first prompts, project paths, branches and tags (all words must match).
- Running sessions come first (marked `●`); Enter focuses the kitty window they run in.
- Other sessions: Enter resumes them in a new kitty tab in their original directory
  (`claude --resume <id>`, the shell stays open afterwards).
- Alt actions: fork session, open shell in project dir, copy resume command / path / session id.
- `cs !<words>` lists only running sessions waiting for input, the most recently finished first.
- `cs <words> rename <title>`, `cs <words> tag <tag>`, `cs <words> untag` rename or tag a matching
  session. This appends an entry to the session's jsonl file; file and directory names stay, since
  `claude --resume` finds sessions by them. Running sessions can't be renamed from outside (the
  CLI re-writes its own title), use `/rename` inside them.

Session metadata comes from `claude_agent_sdk.list_sessions()`, running sessions from
`~/.claude/sessions/<pid>.json`.

## Setup

    pip install claude-agent-sdk psutil   # into Albert's python venv

kitty remote control, for focusing windows and opening tabs:

    allow_remote_control yes
    listen_on unix:@kitty-{kitty_pid}

Without it, sessions are resumed in a new kitty instance.
