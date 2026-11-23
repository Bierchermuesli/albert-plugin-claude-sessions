# -*- coding: utf-8 -*-
"""
Find Claude Code sessions across all project directories and resume them in kitty.

Running sessions (from ~/.claude/sessions/<pid>.json) are listed first; activating one focuses
the kitty window it runs in. Others are resumed in a new kitty tab in their original directory.

Requires in kitty.conf for focusing and tabs (without it, a new kitty instance is started):
    allow_remote_control yes
    listen_on unix:@kitty-{kitty_pid}
"""
from albert import *
from functools import partial
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

import psutil

md_iid = "5.0"
md_version = "1.0"
md_name = "Claude Sessions"
md_description = "Find and resume Claude Code sessions in kitty"
md_license = "MIT"
md_url = "https://github.com/Bierchermuesli/albert-plugin-claude-sessions"
md_authors = ["@Bierchermuesli"]
md_lib_dependencies = ["claude-agent-sdk", "psutil"]

CLAUDE_DIR = Path.home() / ".claude"


def from_sdk(info):
    """Plain dict of a claude_agent_sdk.SDKSessionInfo, so items can carry extra keys."""
    return {"id": info.session_id, "summary": info.summary, "custom_title": info.custom_title,
            "first_prompt": info.first_prompt, "cwd": info.cwd, "branch": info.git_branch,
            "tag": info.tag, "mtime": info.last_modified / 1000}


def session_title(s):
    return s.get("running_name") or s.get("summary") or s.get("first_prompt") or s["id"]


def search(sessions, query):
    """Sessions whose text contains every query word (case-insensitive substring). Running
    sessions first, then title hits, then recency (sessions are passed newest first)."""
    words = query.casefold().split()
    results = []
    for s in sessions:
        title = session_title(s).casefold()
        rest = " ".join(str(s.get(k) or "") for k in
                        ("first_prompt", "summary", "custom_title", "cwd", "branch", "tag",
                         "id")).casefold()
        if not all(w in title or w in rest for w in words):
            continue
        title_hits = sum(w in title for w in words)
        results.append((not s.get("running"), -title_hits, -s["mtime"], s))
    results.sort(key=lambda r: r[:3])
    return [r[3] for r in results]


def parse_query(query):
    """(filter, mode, search, arg) of a query:
    '!<search>' lists only running sessions waiting for input; '<search> rename <title>',
    '<search> tag <tag>' and '<search> untag' rename or tag the matching sessions."""
    query = query.strip()
    waiting = query.startswith("!")
    if waiting:
        query = query[1:].strip()
    m = re.fullmatch(r"(.*?)\s*\b(rename|tag|untag)\b(?:\s+(.*))?", query)
    if m and m.group(1):
        return waiting, m.group(2), m.group(1), (m.group(3) or "").strip()
    return waiting, None, query, ""


def waiting_first(sessions):
    """Running sessions that are not busy, the most recently finished first."""
    idle = [s for s in sessions if s.get("running") and s["running"].get("status") != "busy"]
    return sorted(idle, key=lambda s: s["running"].get("statusUpdatedAt") or 0, reverse=True)


def ago(ts, now=None):
    d = int((now or time.time()) - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            return f"{d // sec}{unit} ago"
    return "just now"


def short_path(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


# --- plugin ---------------------------------------------------------------------------------

class Plugin(PluginInstance, GeneratorQueryHandler):

    _socket_prefix = "kitty-"
    _claude_command = "claude"
    _list_ttl = 2.0
    _batch = 15

    def __init__(self):
        PluginInstance.__init__(self)
        GeneratorQueryHandler.__init__(self)
        self._kitten = shutil.which("kitten") or "kitty"
        self._sdk = None
        self._sdk_loaded = threading.Event()
        self._cache = (0.0, [])
        self._icon_path = Path(__file__).parent / "icon.svg"
        self._init_configuration()
        # Importing the SDK takes about a second, keep it out of Albert's startup.
        threading.Thread(target=self._load_sdk, daemon=True).start()

    # --- configuration ------------------------------------------------------

    @property
    def socket_prefix(self):
        return self._socket_prefix

    @socket_prefix.setter
    def socket_prefix(self, value):
        self._socket_prefix = value
        self.writeConfig("socket_prefix", value)

    @property
    def claude_command(self):
        return self._claude_command

    @claude_command.setter
    def claude_command(self, value):
        self._claude_command = value
        self.writeConfig("claude_command", value)

    def _init_configuration(self):
        for key in ("socket_prefix", "claude_command"):
            conf = self.readConfig(key, str)
            if not conf:
                self.writeConfig(key, getattr(self, f"_{key}"))
            else:
                setattr(self, f"_{key}", conf)

    def configWidget(self):
        return [
            {
                "type": "label",
                "text": "Focusing running sessions and opening tabs needs kitty remote control:<br>"
                        "<tt>allow_remote_control yes<br>listen_on unix:@kitty-{kitty_pid}</tt>",
                "widget_properties": {"textFormat": "RichText"}
            },
            {"type": "lineedit", "label": "kitty socket prefix", "property": "socket_prefix"},
            {"type": "lineedit", "label": "Claude command", "property": "claude_command"},
        ]

    def defaultTrigger(self):
        return "cs "

    def synopsis(self, query):
        return "[!]<words> [rename <title> | tag <tag> | untag]   ! = waiting for input"

    # --- sessions -----------------------------------------------------------

    def _load_sdk(self):
        try:
            import claude_agent_sdk
            self._sdk = claude_agent_sdk
        except Exception as e:
            critical(f"Cannot import claude_agent_sdk: {e}")
        finally:
            self._sdk_loaded.set()

    def _sessions(self):
        """All sessions (newest first) merged with the running ones. list_sessions only reads
        head and tail of each file (~0.1s for a few hundred), a short cache covers typing."""
        stamp, sessions = self._cache
        if time.monotonic() - stamp > self._list_ttl:
            self._sdk_loaded.wait(10)
            if self._sdk is None:
                return []
            sessions = [from_sdk(i) for i in self._sdk.list_sessions()]
            self._cache = (time.monotonic(), sessions)
        sessions = [dict(s) for s in sessions]
        by_id = {s["id"]: s for s in sessions}
        for sid, info in self._running().items():
            s = by_id.get(sid)
            if s is None:  # brand new session, nothing written yet
                s = {"id": sid, "mtime": info.get("startedAt", 0) / 1000, "cwd": info.get("cwd")}
                sessions.insert(0, s)
            s["running"] = info
            if info.get("nameSource") == "user" and info.get("name"):
                s["running_name"] = info["name"]
        return sessions

    @staticmethod
    def _running():
        """sessionId -> ~/.claude/sessions/<pid>.json of live Claude processes."""
        out = {}
        for f in (CLAUDE_DIR / "sessions").glob("*.json"):
            try:
                info = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            pid = info.get("pid")
            if pid and info.get("sessionId") and psutil.pid_exists(pid):
                out[info["sessionId"]] = info
        return out

    # --- kitty --------------------------------------------------------------

    def _sockets(self):
        found = set()
        try:
            with open("/proc/net/unix") as f:
                for line in f:
                    path = line.split()[-1]
                    if path.startswith("@" + self._socket_prefix):
                        pid = path[len(self._socket_prefix) + 1:]
                        if not pid.isdigit() or Path(f"/proc/{pid}").exists():
                            found.add(f"unix:{path}")
        except OSError as e:
            warning(f"Cannot read /proc/net/unix: {e}")
        return sorted(found)

    def _rc(self, sock, *args):
        return subprocess.run([self._kitten, "@", "--to", sock, *args],
                              capture_output=True, text=True, timeout=3)

    def _kitty_windows(self):
        """[(sock, os_window)] of all reachable kitty instances."""
        out = []
        for sock in self._sockets():
            try:
                r = self._rc(sock, "ls")
                if r.returncode == 0:
                    out += [(sock, osw) for osw in json.loads(r.stdout)]
            except (subprocess.TimeoutExpired, ValueError) as e:
                warning(f"kitty @ ls via {sock} failed: {e}")
        return out

    @staticmethod
    def _ancestors(pid):
        """pid and its parent pids, to match a claude process started from a shell or wrapper."""
        try:
            proc = psutil.Process(pid)
            return {pid, *(p.pid for p in proc.parents())}
        except psutil.Error:
            return {pid}

    def _find_window(self, pid):
        """(sock, window id) of the kitty window running process pid, else None."""
        chain = self._ancestors(pid)
        for sock, osw in self._kitty_windows():
            for tab in osw.get("tabs", []):
                for win in tab.get("windows", []):
                    procs = {p.get("pid") for p in win.get("foreground_processes", [])}
                    if win.get("pid") in chain or procs & chain:
                        return sock, win["id"]
        return None

    def _active_socket(self):
        best = None
        for sock, osw in self._kitty_windows():
            if osw.get("is_focused"):
                return sock
            last = max((w.get("last_focused_at") or 0 for t in osw.get("tabs", [])
                        for w in t.get("windows", [])), default=0)
            if best is None or last > best[0]:
                best = (last, sock)
        return best[1] if best else None

    def _launch(self, cwd, cmd=None, title=None):
        """New kitty tab in cwd running cmd (a shell command string; the shell stays open after
        it), in the most recently focused kitty, or a new kitty if none is reachable."""
        shell = os.environ.get("SHELL", "/bin/sh")
        argv = [shell, "-c", f"{cmd}; exec {shlex.quote(shell)}"] if cmd else []
        if cwd and not Path(cwd).is_dir():
            self._notify(f"Directory is gone: {cwd}")
            return
        sock = self._active_socket()
        if sock is None:
            runDetachedProcess(["kitty", "--directory", cwd or str(Path.home()), *argv])
            return
        args = ["launch", "--type=tab", f"--cwd={cwd or Path.home()}"]
        if title:
            args.append(f"--tab-title={title[:40]}")
        r = self._rc(sock, *args, *(["--", *argv] if argv else []))
        if r.returncode != 0:
            self._notify(f"kitty launch failed: {r.stderr.strip()}")
        elif r.stdout.strip().isdigit():
            self._rc(sock, "focus-window", "--match", f"id:{r.stdout.strip()}")

    def _focus(self, s):
        found = self._find_window(s["running"]["pid"])
        if not found:
            self._notify("Session runs outside kitty (or remote control is off)")
            return
        sock, window_id = found
        r = self._rc(sock, "focus-window", "--match", f"id:{window_id}")
        if r.returncode != 0:
            self._notify(f"Focus failed: {r.stderr.strip()}")

    def _resume_cmd(self, s, fork=False):
        cmd = f"{self._claude_command} --resume {shlex.quote(s['id'])}"
        return cmd + " --fork-session" if fork else cmd

    def _mutate(self, s, mode, arg):
        """Rename or (un)tag a session through the SDK, which appends an entry to its jsonl
        file. File and directory names stay, claude --resume finds sessions by them."""
        try:
            if mode == "rename":
                self._sdk.rename_session(s["id"], arg, directory=s.get("cwd"))
            else:
                self._sdk.tag_session(s["id"], arg if mode == "tag" else None,
                                      directory=s.get("cwd"))
        except (ValueError, OSError) as e:
            self._notify(f"{mode} failed: {e}")
        self._cache = (0.0, [])

    def _notify(self, text):
        self._notification = Notification(title=md_name, text=text)
        self._notification.send()

    @staticmethod
    def _async(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # --- items --------------------------------------------------------------

    def _icon(self):
        return Icon.image(self._icon_path)

    def _item(self, s):
        title = session_title(s)
        cwd = s.get("cwd") or ""
        running = s.get("running")
        parts = [short_path(cwd) if cwd else "?"]
        if s.get("branch") and s["branch"] != "HEAD":
            parts.append(s["branch"])
        if s.get("tag"):
            parts.append(f"#{s['tag']}")
        if running:
            status = running.get("status") or "?"
            since = (running.get("statusUpdatedAt") or 0) / 1000
            parts.append(f"● {status} " + (ago(since) if since else ""))
        else:
            parts.append(ago(s["mtime"]))
        if cwd and not Path(cwd).is_dir():
            parts.append("directory gone")

        actions = []
        if running:
            actions.append(Action("focus", "Focus kitty window", partial(self._async, self._focus, s)))
        else:
            actions.append(Action("resume", "Resume in new kitty tab",
                                  partial(self._async, self._launch, cwd, self._resume_cmd(s), title)))
        actions += [
            Action("fork", "Fork session in new kitty tab",
                   partial(self._async, self._launch, cwd, self._resume_cmd(s, fork=True), title)),
            Action("shell", "Open shell in project directory",
                   partial(self._async, self._launch, cwd, None, short_path(cwd))),
            Action("copy-cmd", "Copy resume command",
                   partial(setClipboardText, f"cd {shlex.quote(cwd)} && {self._resume_cmd(s)}")),
            Action("copy-path", "Copy project path", partial(setClipboardText, cwd)),
            Action("copy-id", "Copy session id", partial(setClipboardText, s["id"])),
        ]

        return StandardItem(
            id=s["id"],
            text=("● " if running else "") + title,
            subtext=" · ".join(parts),
            input_action_text=self.defaultTrigger() + title,
            icon_factory=self._icon,
            actions=actions,
        )

    def _mutate_item(self, s, mode, arg):
        """Item of the rename/tag mode: a single action that applies it, or a hint why not."""
        title = session_title(s)
        if mode == "rename" and s.get("running"):
            # The CLI keeps re-appending its own title, an outside rename would be overwritten.
            return StandardItem(id=s["id"], text=title, icon_factory=self._icon,
                                subtext="Running: use /rename inside the session")
        if mode != "untag" and not arg:
            return StandardItem(id=s["id"], text=title, icon_factory=self._icon,
                                subtext=f"Type the new {'title' if mode == 'rename' else 'tag'}")
        label = {"rename": f"Rename to '{arg}'", "tag": f"Tag with #{arg}",
                 "untag": "Remove tag"}[mode]
        return StandardItem(
            id=s["id"], text=title, icon_factory=self._icon,
            subtext=f"{label} · {short_path(s.get('cwd') or '?')}",
            actions=[Action(mode, label, partial(self._mutate, s, mode, arg))])

    def items(self, ctx):
        waiting, mode, query, arg = parse_query(ctx.query)
        sessions = self._sessions()
        if waiting:
            sessions = waiting_first(sessions)
        results = search(sessions, query)
        if waiting:  # keep the recently-finished order instead of search ranking
            order = {s["id"]: i for i, s in enumerate(sessions)}
            results.sort(key=lambda s: order[s["id"]])
        make = partial(self._mutate_item, mode=mode, arg=arg) if mode else self._item
        for i in range(0, len(results), self._batch):
            if not ctx.isValid:
                return
            yield [make(s) for s in results[i:i + self._batch]]
