#!/usr/bin/env python3
"""omarchy-sbx-bridge: a small MCP stdio server that lets an agent running in a
Docker Sandbox (sbx) trigger a fixed set of Omarchy actions on the host.

sbx starts it on the host, outside the sandbox, with the user's permissions:

    sbx mcp add omarchy-bridge --command /usr/bin/python3 \
        --args /home/<user>/.local/share/omarchy-sbx-bridge/bridge.py

Design rules:
- Every tool runs a fixed argv. Nothing is passed through a shell, and the
  agent cannot choose the program.
- Arguments are validated against enums, bounds or strict patterns.
- Files the sandbox can write are never handed to host tools directly. They
  are copied first, without following symlinks, and the tool runs on the copy.
- Apps from the sandbox workspace run on the host only inside bubblewrap:
  their project folder, the Wayland display and the GPU; no home, no D-Bus,
  and no network unless the user registered the bridge with --app-network.
- Screenshots of anything beyond the bar are taken on an empty workspace (the
  stage, "sbx") that holds only development windows, and never while a
  sensitive overlay (notifications, clipboard, polkit, lock) is visible.
- This file must live outside every folder mounted into a sandbox; otherwise
  the agent could rewrite its own bridge.

Run `bridge.py --check` in a terminal to print the detected session.
"""

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1.1.2"
HOME = Path.home()
PLUGINS_DIR = HOME / ".config/omarchy/plugins"
STATE_DIR = HOME / ".local/state/omarchy-sbx-bridge"
CACHE_DIR = HOME / ".cache/omarchy-sbx-bridge"
LOG_FILE = STATE_DIR / "bridge.log"
QMLLINT = "/usr/lib/qt6/bin/qmllint"

MAX_TEXT = 60_000           # characters of tool output returned to the agent
BAR_HEIGHT = 60             # logical px captured by screenshot target "bar"
COPY_MAX_FILE = 20 << 20    # bytes per file when copying a plugin
COPY_MAX_TOTAL = 100 << 20  # bytes per copy in total
COPY_MAX_FILES = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

STAGE = "sbx"                         # named Hyprland workspace used as the stage
APP_MARKER = "OMARCHY_SBX_APP"        # set in the environment of every app the bridge starts
RUN_SCRIPT = "sbx-run.sh"             # entry point of an app project
WEB_PREVIEW = "web-preview"           # APPS key of the preview browser
SENSITIVE_LAYERS = ("omarchy-notifications", "omarchy-clipboard", "omarchy-polkit",
                    "omarchy-lock", "omarchy-image-selector", "hyprlock")
OWN_SUMMARIES = ("Sandbox-Screenshot", "Sandbox-App gestartet", "Sandbox-Webvorschau")
# Read-only extras so apps look like the rest of the desktop (theme, fonts, icons).
LOOK_AND_FEEL = [HOME / ".config/gtk-3.0", HOME / ".config/gtk-4.0", HOME / ".config/fontconfig",
                 HOME / ".local/share/fonts", HOME / ".local/share/icons", HOME / ".config/omarchy",
                 HOME / ".local/state/omarchy/current", HOME / ".config/foot"]
PASS_ENV = ("LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME",
            "XCURSOR_THEME", "XCURSOR_SIZE", "GTK_THEME", "QT_STYLE_OVERRIDE", "TZ")

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HYPR_OPTION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")
GEOMETRY_RE = re.compile(r"^-?\d{1,5},-?\d{1,5} \d{1,5}x\d{1,5}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SUFFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
PACKAGE_RE = re.compile(r"^[a-z0-9@._+-]{1,100}$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9._+-]{1,100}$")
URL_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/?=&%#:+,-]{0,300}$")
HYPR_QUERIES = ["monitors", "workspaces", "activeworkspace", "clients",
                "activewindow", "layers", "devices", "binds", "version",
                "configerrors", "getoption"]

OPTS = argparse.Namespace(allow_fullscreen=False, no_screenshots=False, app_network=False,
                          workspace=HOME / "Work/omarchy-sbx", sandbox_name="omarchy")
APPS = {}          # project -> Popen of the running app (bwrap) or the preview browser
LAUNCHED = set()   # pids of every process the bridge started


class ToolError(Exception):
    """A failure the agent should see as a tool result, not a protocol error."""


# ---------------------------------------------------------------- logging

def log(message):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1 << 20:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- session environment

def newest_entry(directory, predicate):
    try:
        entries = [p for p in Path(directory).iterdir() if predicate(p)]
    except OSError:
        return None
    if not entries:
        return None
    return max(entries, key=lambda p: p.lstat().st_mtime).name


def session_env():
    """Environment for host commands, rebuilt per call so a restarted
    Hyprland (new instance signature) is picked up."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")

    omarchy_path = None
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], env=env,
                             capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL).stdout
        for line in out.splitlines():
            if line.startswith("OMARCHY_PATH="):
                omarchy_path = line.split("=", 1)[1]
    except (OSError, subprocess.SubprocessError):
        pass
    omarchy_path = omarchy_path or env.get("OMARCHY_PATH") or "/usr/share/omarchy"
    env["OMARCHY_PATH"] = omarchy_path

    fixed = [f"{omarchy_path}/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    rest = [p for p in env.get("PATH", "").split(":") if p and p not in fixed]
    env["PATH"] = ":".join(fixed + rest)

    hypr_dir = Path(runtime) / "hypr"
    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature or not (hypr_dir / signature).is_dir():
        signature = newest_entry(hypr_dir, lambda p: p.is_dir())
    if signature:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
    else:
        env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)

    display = env.get("WAYLAND_DISPLAY")
    if not display or not (Path(runtime) / display).exists():
        display = newest_entry(runtime, lambda p: re.fullmatch(r"wayland-\d+", p.name) is not None)
    if display:
        env["WAYLAND_DISPLAY"] = display
    return env


# ---------------------------------------------------------------- process helpers

def run(argv, timeout=20, binary=False):
    """Run a fixed argv in its own process group; kill the group on timeout."""
    proc = subprocess.Popen(argv, env=session_env(), cwd=HOME,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        raise ToolError(f"{argv[0]} timed out after {timeout}s") from None
    err = err.decode("utf-8", "replace")
    if binary:
        return proc.returncode, out, err
    return proc.returncode, out.decode("utf-8", "replace"), err


def clip(text):
    if len(text) <= MAX_TEXT:
        return text
    return f"[... {len(text) - MAX_TEXT} earlier characters cut ...]\n" + text[-MAX_TEXT:]


def report(code, out, err):
    parts = [f"exit code: {code}"]
    if out.strip():
        parts.append(out.rstrip())
    if err.strip():
        parts.append("--- stderr ---\n" + err.rstrip())
    return clip("\n".join(parts))


def text_result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def notify(summary, body):
    """Tell the user what the sandbox just did. Short-lived, so a toast does
    not block the next screenshot for long."""
    try:
        run(["notify-send", "-a", "Claude Sandbox", "-u", "low", "-t", "2000", summary, body], timeout=5)
    except (ToolError, OSError):
        pass


def int_arg(args, name, default, low, high):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{name} must be an integer between {low} and {high}")
    return value


# ---------------------------------------------------------------- safe copies

def open_dir_nofollow(path):
    """Open a directory without following a symlink in any path component."""
    path = Path(os.path.normpath(path))
    if not path.is_absolute():
        raise ToolError(f"not an absolute path: {path}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                raise ToolError(f"cannot open {path} without following symlinks ({part}: {e.strerror})") from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def safe_copy(src, dst, include):
    """Copy the tree at src into dst without following symlinks. Symlinks and
    special files are skipped and reported. Returns (symlinks, skipped)."""
    symlinks, skipped = [], []
    budget = {"bytes": 0, "files": 0}

    def walk(dir_fd, rel):
        with os.scandir(dir_fd) as it:
            names = sorted(e.name for e in it)
        for name in names:
            relpath = f"{rel}{name}"
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                symlinks.append(relpath)
            elif stat.S_ISDIR(st.st_mode):
                if name in SKIP_DIRS:
                    continue
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    walk(sub, relpath + "/")
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                if not include(name):
                    continue
                if st.st_size > COPY_MAX_FILE:
                    skipped.append(f"{relpath} (larger than {COPY_MAX_FILE >> 20} MiB)")
                    continue
                budget["files"] += 1
                budget["bytes"] += st.st_size
                if budget["files"] > COPY_MAX_FILES or budget["bytes"] > COPY_MAX_TOTAL:
                    raise ToolError("folder too large to check (file or size limit reached)")
                # O_NONBLOCK: if the entry was swapped for a FIFO, don't hang on it.
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        skipped.append(f"{relpath} (not a regular file)")
                        continue
                    target = dst / relpath
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with os.fdopen(os.dup(fd), "rb") as fin, open(target, "wb") as fout:
                        shutil.copyfileobj(fin, fout, 1 << 20)
                finally:
                    os.close(fd)
            else:
                skipped.append(f"{relpath} (special file)")

    root = open_dir_nofollow(src)
    try:
        dst.mkdir(parents=True, exist_ok=True)
        walk(root, "")
    finally:
        os.close(root)
    return symlinks, skipped


def scratch_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return tempfile.TemporaryDirectory(dir=CACHE_DIR)


# ---------------------------------------------------------------- Hyprland helpers

def hypr_json(*what):
    code, out, err = run(["hyprctl", "-j", *what], timeout=10)
    if code != 0:
        raise ToolError(report(code, out, err))
    try:
        return json.loads(out)
    except ValueError:
        raise ToolError(f"hyprctl {' '.join(what)} returned no JSON: {out[:200]}") from None


def hypr_dispatch(lua):
    code, out, err = run(["hyprctl", "dispatch", lua], timeout=10)
    if code != 0 or "error" in out.lower() or "invalid" in out.lower():
        raise ToolError("hyprctl dispatch failed: " + report(code, out, err))


def lua_string(value):
    return json.dumps(value)  # values are validated first; JSON quoting is valid Lua here


def focus_workspace(selector):
    hypr_dispatch(f"hl.dsp.focus({{ workspace = {lua_string(selector)} }})")


def workspace_selector(workspace):
    wid = workspace.get("id", 0)
    return str(wid) if isinstance(wid, int) and wid > 0 else f"name:{workspace.get('name', '')}"


def focused_monitor():
    monitors = hypr_json("monitors")
    if not monitors:
        raise ToolError("no monitors reported by hyprctl")
    return next((m for m in monitors if m.get("focused")), monitors[0])


def monitor_geometry(m, height=None):
    width, full_height = m["width"], m["height"]
    if m.get("transform", 0) % 2 == 1:
        width, full_height = full_height, width
    scale = m.get("scale") or 1
    lw, lh = round(width / scale), round(full_height / scale)
    return f"{m['x']},{m['y']} {lw}x{height or lh}"


def find_client(address):
    return next((c for c in hypr_json("clients") if c.get("address") == address), None)


def session_locked():
    try:
        code, _out, _err = run(["omarchy-hyprland-session-locked"], timeout=5)
        return code == 0
    except (ToolError, OSError):
        return False


def sensitive_layers():
    found = set()

    def walk(node):
        if isinstance(node, dict):
            namespace = node.get("namespace")
            if isinstance(namespace, str) and namespace.startswith(SENSITIVE_LAYERS):
                found.add(namespace)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(hypr_json("layers"))
    return sorted(found)


def visible_toasts():
    """Number of toasts omapager shows right now, or None if omapager is not
    the notification daemon. Omapager keeps its layer mapped even when empty,
    so the layer check alone cannot see its toasts."""
    try:
        code, out, _err = run(["omarchy-shell", "omapager", "count"], timeout=5)
    except (ToolError, OSError):
        return None
    out = out.strip()
    return int(out) if code == 0 and out.isdigit() else None


def wait_until_private(timeout=8):
    if session_locked():
        raise ToolError("not captured: the session is locked")
    deadline = time.time() + timeout
    while True:
        busy = sensitive_layers()
        if visible_toasts():
            for summary in OWN_SUMMARIES:  # our own notices may go; others must expire
                try:
                    run(["omarchy-shell", "notifications", "dismiss", summary], timeout=5)
                except (ToolError, OSError):
                    pass
            toasts = visible_toasts()
            if toasts:
                busy.append(f"{toasts} notification(s) on screen")
        if not busy:
            return
        if time.time() > deadline:
            raise ToolError(f"not captured: a sensitive overlay is open ({', '.join(busy)}). Try again later.")
        time.sleep(0.4)


# ---------------------------------------------------------------- development windows

def parent_pid(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return int(f.read().rsplit(b")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def descends_from(pid, roots):
    for _ in range(64):
        if pid <= 1:
            return False
        if pid in roots:
            return True
        pid = parent_pid(pid)
    return False


def has_marker(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f"\0{APP_MARKER}=".encode() in b"\0" + f.read()
    except OSError:
        return False


def is_shell_process(pid, omarchy_path):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    return f"{omarchy_path}/shell".encode() in args


def dev_window(client, omarchy_path):
    """Windows the sandbox may stage and capture: apps the bridge started and
    windows of the Omarchy shell (plugin windows). Nothing else."""
    pid = client.get("pid", 0)
    if not isinstance(pid, int) or pid <= 1:
        return False
    return descends_from(pid, LAUNCHED) or has_marker(pid) or is_shell_process(pid, omarchy_path)


def move_to_stage(address):
    hypr_dispatch(f"hl.dsp.window.move({{ workspace = {lua_string('name:' + STAGE)}, "
                  f"follow = false, window = {lua_string('address:' + address)} }})")
    client = find_client(address)
    if not client or client.get("workspace", {}).get("name") != STAGE:
        raise ToolError(f"window {address} could not be moved to workspace {STAGE}")
    return client


def on_stage(action):
    """Switch to the stage, make sure it only shows development windows and no
    sensitive overlay, run action, then switch back."""
    omarchy_path = session_env()["OMARCHY_PATH"]
    foreign = [c for c in hypr_json("clients")
               if c.get("workspace", {}).get("name") == STAGE and not dev_window(c, omarchy_path)]
    if foreign:
        names = ", ".join(f"{c.get('class')} ({c.get('address')})" for c in foreign)
        raise ToolError(f"workspace {STAGE} holds windows that were not started for development: {names}. "
                        "Ask the user to move them away.")
    active = hypr_json("activeworkspace")
    previous = workspace_selector(active)
    switched = active.get("name") != STAGE
    try:
        if switched:
            focus_workspace(f"name:{STAGE}")
            time.sleep(0.7)  # let the workspace animation finish
            if hypr_json("activeworkspace").get("name") != STAGE:
                raise ToolError(f"could not switch to workspace {STAGE}")
        wait_until_private()
        return action()
    finally:
        if switched:
            try:
                focus_workspace(previous)
            except ToolError:
                pass


def capture(geometry, png=True):
    if png:
        argv, mime = ["grim", "-g", geometry, "-s", "1", "-t", "png", "-"], "image/png"
    else:
        argv, mime = ["grim", "-g", geometry, "-s", "1", "-t", "jpeg", "-q", "85", "-"], "image/jpeg"
    code, data, err = run(argv, timeout=15, binary=True)
    if code != 0 or not data:
        raise ToolError(report(code, "", err))
    return data, mime


# ---------------------------------------------------------------- tools: shell and plugins

def tool_status(_args):
    env = session_env()
    running = [f"{name} (pid {p.pid})" for name, p in APPS.items() if p.poll() is None]
    lines = [
        f"omarchy-sbx-bridge {VERSION}",
        f"OMARCHY_PATH={env.get('OMARCHY_PATH')}",
        f"XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR')}",
        f"HYPRLAND_INSTANCE_SIGNATURE={env.get('HYPRLAND_INSTANCE_SIGNATURE', '(not found)')}",
        f"WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '(not found)')}",
        f"plugins dir: {PLUGINS_DIR}",
        f"workspace: {OPTS.workspace}",
        f"stage workspace: {STAGE}",
        f"sandbox name (for ports): {OPTS.sandbox_name}",
        "screenshots: " + ("off" if OPTS.no_screenshots else
                           "bar, stage, window, screen, geometry" if OPTS.allow_fullscreen
                           else "bar, stage, window"),
        f"app network: {'on' if OPTS.app_network else 'off'}",
        f"running apps: {', '.join(running) or 'none'}",
        f"audit log: {LOG_FILE}",
    ]
    for label, argv in (("shell ping", ["omarchy-shell", "shell", "ping"]),
                        ("hyprctl version", ["hyprctl", "version"])):
        try:
            code, out, err = run(argv, timeout=10)
            first = (out.strip() or err.strip()).splitlines()[:1]
            lines.append(f"{label}: exit {code} {first[0] if first else ''}")
        except (ToolError, OSError) as e:
            lines.append(f"{label}: failed ({e})")
    return text_result("\n".join(lines))


def journal(lines, since=None, needle=None):
    fetch = min(lines * 20, 20000) if needle else lines
    argv = ["journalctl", "--user", "-t", "omarchy-shell", "--no-pager",
            "-o", "short-iso", "-n", str(fetch)]
    if since is not None:
        argv += ["--since", f"@{int(since)}"]
    code, out, err = run(argv, timeout=20)
    if code != 0:
        raise ToolError(report(code, out, err))
    rows = out.splitlines()
    if needle:
        rows = [r for r in rows if needle.lower() in r.lower()]
    rows = rows[-lines:]
    return clip("\n".join(rows)) if rows else "(no matching log lines)"


def tool_restart_shell(_args):
    started = time.time()
    code, out, err = run(["omarchy-restart-shell"], timeout=90)
    time.sleep(1.5)  # give plugins a moment to load and log
    logs = journal(80, since=started - 1)
    text = report(code, out, err) + "\n\n--- omarchy-shell log since restart ---\n" + logs
    return text_result(text, error=code != 0)


def tool_rescan_plugins(_args):
    code, out, err = run(["omarchy-shell", "shell", "rescanPlugins"], timeout=15)
    return text_result(report(code, out, err), error=code != 0)


def tool_list_plugins(_args):
    code, out, err = run(["omarchy-shell", "shell", "listPlugins"], timeout=15)
    return text_result(report(code, out, err), error=code != 0)


def tool_plugin_ipc(args):
    plugin_id = args.get("plugin_id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.match(plugin_id):
        raise ToolError("plugin_id must be a plugin folder name")
    try:
        if not stat.S_ISDIR(os.lstat(PLUGINS_DIR / plugin_id).st_mode):
            raise OSError
    except OSError:
        raise ToolError(f"{plugin_id} is not a folder in {PLUGINS_DIR}") from None
    target = plugin_id
    suffix = args.get("target_suffix")
    if suffix is not None:
        if not isinstance(suffix, str) or not SUFFIX_RE.match(suffix):
            raise ToolError("target_suffix must be a short name like jobs")
        target = f"{plugin_id}.{suffix}"
    method = args.get("method")
    if not isinstance(method, str) or not METHOD_RE.match(method):
        raise ToolError("method must be an IPC function name")
    extra = args.get("args", [])
    if (not isinstance(extra, list) or len(extra) > 4 or
            not all(isinstance(a, str) and len(a) <= 500 and "\0" not in a for a in extra)):
        raise ToolError("args must be a list of at most 4 strings (500 characters each)")
    code, out, err = run(["omarchy-shell", target, method, *extra], timeout=15)
    return text_result(report(code, out, err), error=code != 0)


def tool_shell_logs(args):
    lines = int_arg(args, "lines", 150, 1, 2000)
    needle = args.get("filter")
    if needle is not None and (not isinstance(needle, str) or len(needle) > 200):
        raise ToolError("filter must be a string of at most 200 characters")
    since = None
    if args.get("since_seconds") is not None:
        since = time.time() - int_arg(args, "since_seconds", 0, 1, 7 * 86400)
    return text_result(journal(lines, since, needle or None))


def tool_validate_plugin(args):
    plugin_id = args.get("plugin_id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.match(plugin_id):
        raise ToolError("plugin_id must be a plugin folder name like io.github.user.name")
    with scratch_dir() as tmp:
        copy = Path(tmp) / plugin_id
        symlinks, skipped = safe_copy(PLUGINS_DIR / plugin_id, copy, lambda _n: True)
        code, out, err = run(["omarchy-plugin-validate", str(copy)], timeout=30)
        text = report(code, out, err).replace(str(copy), str(PLUGINS_DIR / plugin_id))
    if code == 0 and not out.strip() and not err.strip():
        text = "exit code: 0\nvalid"
    notes = []
    if symlinks:
        notes.append("Symlinks found (the shell rejects plugins with symlinks): " + ", ".join(symlinks))
    if skipped:
        notes.append("Not checked: " + ", ".join(skipped))
    if notes:
        text += "\n\n" + "\n".join(notes)
    return text_result(text, error=code != 0 or bool(symlinks))


def tool_qmllint(args):
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        raise ToolError("path is required")
    path = Path(raw)
    if not path.is_absolute():
        path = PLUGINS_DIR / path
    path = Path(os.path.normpath(path))
    if path.suffix != ".qml":
        raise ToolError("only .qml files can be linted")
    for root in (PLUGINS_DIR, Path(OPTS.workspace)):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            break
    else:
        raise ToolError(f"path must be inside {PLUGINS_DIR}/<plugin>/ or {OPTS.workspace}/<project>/")

    unit = rel.parts[0]
    qs_import = CACHE_DIR / "import"
    qs_link = qs_import / "qs"
    qs_import.mkdir(parents=True, exist_ok=True)
    shell_dir = Path(session_env()["OMARCHY_PATH"]) / "shell"
    if not qs_link.is_symlink() or os.readlink(qs_link) != str(shell_dir):
        qs_link.unlink(missing_ok=True)
        qs_link.symlink_to(shell_dir)

    def include(name):
        return name.endswith((".qml", ".js", ".mjs")) or name == "qmldir"

    with scratch_dir() as tmp:
        copy = Path(tmp) / unit
        safe_copy(root / unit, copy, include)
        target = copy.joinpath(*rel.parts[1:])
        if not target.is_file():
            raise ToolError(f"{path} not found (or it is a symlink, which is not followed)")
        code, out, err = run([QMLLINT, "-I", str(qs_import), str(target)], timeout=60)
        text = report(code, out, err).replace(str(copy), str(root / unit))
    # qmllint exits non-zero for plain warnings too, so judge by the findings.
    categories = re.findall(r"\[([a-z][a-z-]*)\]$", out + "\n" + err, re.MULTILINE)
    if not categories and code == 0:
        return text_result("no findings")
    counts = {c: categories.count(c) for c in sorted(set(categories))}
    summary = "findings: " + (", ".join(f"{c}={n}" for c, n in counts.items()) or "none parsed")
    return text_result(summary + "\n" + text, error="syntax" in counts)


# ---------------------------------------------------------------- tools: Hyprland

def tool_hypr_query(args):
    what = args.get("what")
    if what not in HYPR_QUERIES:
        raise ToolError("what must be one of: " + ", ".join(HYPR_QUERIES))
    argv = ["hyprctl", "-j", what]
    if what == "getoption":
        option = args.get("option")
        if not isinstance(option, str) or not HYPR_OPTION_RE.match(option):
            raise ToolError("getoption needs option, e.g. general:gaps_in")
        argv.append(option)
    code, out, err = run(argv, timeout=10)
    return text_result(report(code, out, err), error=code != 0)


def tool_hypr_reload(_args):
    code, out, err = run(["hyprctl", "reload"], timeout=20)
    _c, errors, _e = run(["hyprctl", "configerrors"], timeout=10)
    text = report(code, out, err) + "\n\n--- configerrors ---\n" + (errors.strip() or "(none)")
    return text_result(text, error=code != 0)


# ---------------------------------------------------------------- tools: stage and screenshots

def tool_stage_list(_args):
    omarchy_path = session_env()["OMARCHY_PATH"]
    rows = []
    for c in hypr_json("clients"):
        on_stage_ws = c.get("workspace", {}).get("name") == STAGE
        dev = dev_window(c, omarchy_path)
        if on_stage_ws or dev:
            rows.append(f"{c.get('address')} | class={c.get('class')} | title={c.get('title', '')[:80]} | "
                        f"workspace={c.get('workspace', {}).get('name')} | pid={c.get('pid')} | "
                        f"{'development window' if dev else 'NOT a development window'}")
    return text_result("\n".join(rows) or f"no development windows and nothing on workspace {STAGE}")


def tool_stage_window(args):
    address = args.get("address")
    if not isinstance(address, str) or not ADDRESS_RE.match(address):
        raise ToolError("address must look like 0x55d0c0ffee00 (see stage_list)")
    client = find_client(address)
    if not client:
        raise ToolError(f"no window with address {address}")
    if not dev_window(client, session_env()["OMARCHY_PATH"]):
        raise ToolError("only development windows (started by the bridge, or Omarchy shell windows) "
                        "can be moved to the stage")
    move_to_stage(address)
    return text_result(f"{address} ({client.get('class')}) is on workspace {STAGE}")


def tool_screenshot(args):
    if OPTS.no_screenshots:
        raise ToolError("screenshots are turned off for this bridge (--no-screenshots)")
    target = args.get("target", "bar")
    if target not in ("bar", "stage", "window", "screen", "geometry"):
        raise ToolError("target must be bar, stage, window, screen or geometry")

    if target == "bar":
        wait_until_private()
        geometry = monitor_geometry(focused_monitor(), BAR_HEIGHT)
        data, mime = capture(geometry)
    elif target == "stage":
        geometry = monitor_geometry(focused_monitor())
        data, mime = on_stage(lambda: capture(geometry, png=False))
    elif target == "window":
        address = args.get("address")
        if not isinstance(address, str) or not ADDRESS_RE.match(address):
            raise ToolError("target=window needs address (see stage_list)")
        client = find_client(address)
        if not client or not dev_window(client, session_env()["OMARCHY_PATH"]):
            raise ToolError("target=window works only for development windows (see stage_list)")
        if client.get("workspace", {}).get("name") != STAGE:
            move_to_stage(address)

        def shoot_window():
            c = find_client(address)
            if not c:
                raise ToolError(f"window {address} disappeared")
            (x, y), (w, h) = c["at"], c["size"]
            return capture(f"{x},{y} {w}x{h}")

        data, mime = on_stage(shoot_window)
        geometry = f"window {address}"
    else:
        if not OPTS.allow_fullscreen:
            raise ToolError(f"target {target} is not allowed; use bar, stage or window. The user can "
                            "register the bridge with --allow-fullscreen to permit screen and geometry.")
        if target == "geometry":
            geometry = args.get("geometry")
            if not isinstance(geometry, str) or not GEOMETRY_RE.match(geometry):
                raise ToolError("geometry must look like 'X,Y WxH' in logical pixels")
        else:
            geometry = monitor_geometry(focused_monitor())
        wait_until_private()
        data, mime = capture(geometry, png=False)

    notify("Sandbox-Screenshot", f"{target}: {geometry}")
    return {"content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime},
                        {"type": "text", "text": f"screenshot target={target} ({geometry}, logical px)"}],
            "isError": False}


# ---------------------------------------------------------------- tools: apps

def stop_app(name):
    proc = APPS.pop(name, None)
    if not proc or proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)
    return True


def app_log_path(name):
    return STATE_DIR / "apps" / f"{name}.log"


def tail_file(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return "(no log yet)"
    return clip("\n".join(rows[-lines:])) or "(log is empty)"


def stage_new_windows(roots, before, timeout=12):
    """Wait for new windows of the given process trees and move them to the stage."""
    found, first_seen = {}, None
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in hypr_json("clients"):
            address = c.get("address")
            if address in before or address in found:
                continue
            pid = c.get("pid", 0)
            if isinstance(pid, int) and descends_from(pid, roots):
                found[address] = c
                first_seen = first_seen or time.time()
        if first_seen and time.time() - first_seen > 1.0:
            break
        if not found and all(p.poll() is not None for p in roots.values()):
            break
        time.sleep(0.25)
    rows = []
    for address, c in found.items():
        try:
            move_to_stage(address)
            rows.append(f"{address} class={c.get('class')} title={c.get('title', '')[:60]} -> {STAGE}")
        except ToolError as e:
            rows.append(f"{address} class={c.get('class')}: {e}")
    return rows


def host_color_scheme():
    """'prefer-dark', 'prefer-light' or 'default' from the host. Apps in
    bubblewrap have no D-Bus, so libadwaita cannot ask the settings portal."""
    try:
        code, out, _err = run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"], timeout=5)
    except (ToolError, OSError):
        return None
    value = out.strip().strip("'")
    return value if code == 0 and value in ("prefer-dark", "prefer-light", "default") else None


def bwrap_command(project_path, project_fd, inner, base_env, name):
    runtime = base_env["XDG_RUNTIME_DIR"]
    display = base_env.get("WAYLAND_DISPLAY")
    if not display:
        raise ToolError("no Wayland display found on the host")
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if OPTS.app_network:
        argv.append("--share-net")
    argv += ["--ro-bind", "/usr", "/usr",
             "--symlink", "usr/bin", "/bin", "--symlink", "usr/bin", "/sbin",
             "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
             "--ro-bind", "/etc", "/etc", "--ro-bind-try", "/opt", "/opt",
             "--ro-bind", "/sys", "/sys", "--proc", "/proc", "--dev", "/dev",
             "--dev-bind-try", "/dev/dri", "/dev/dri",
             "--ro-bind-try", "/var/cache/fontconfig", "/var/cache/fontconfig",
             "--tmpfs", "/tmp", "--tmpfs", "/run",
             "--perms", "0700", "--dir", runtime,
             "--bind", f"{runtime}/{display}", f"{runtime}/{display}",
             "--tmpfs", "/home", "--dir", str(HOME),
             "--bind-fd", str(project_fd), project_path]
    for extra in LOOK_AND_FEEL:
        argv += ["--ro-bind-try", str(extra), str(extra)]
    env = {"HOME": str(HOME), "USER": HOME.name, "LOGNAME": HOME.name,
           "PATH": "/usr/local/bin:/usr/bin", "XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": display,
           "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "Hyprland",
           "QT_QPA_PLATFORM": "wayland", "GDK_BACKEND": "wayland", "SDL_VIDEODRIVER": "wayland",
           "NO_AT_BRIDGE": "1", "GTK_A11Y": "none", "TERM": "xterm-256color",
           "OMARCHY_PATH": base_env["OMARCHY_PATH"], APP_MARKER: name}
    env.update({k: base_env[k] for k in PASS_ENV if k in base_env})
    scheme = host_color_scheme()
    if scheme:
        env["ADW_DEBUG_COLOR_SCHEME"] = scheme  # libadwaita follows the host's light/dark setting
    argv += ["--chdir", project_path, "--clearenv"]
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return argv + ["--", *inner]


def tool_launch_app(args):
    project = args.get("project")
    if not isinstance(project, str) or not PROJECT_RE.match(project) or project == WEB_PREVIEW:
        raise ToolError("project must be a folder name in the workspace, e.g. my-app")
    terminal = args.get("terminal", False)
    if not isinstance(terminal, bool):
        raise ToolError("terminal must be true or false")

    project_path = str(Path(OPTS.workspace) / project)
    workspace_fd = open_dir_nofollow(OPTS.workspace)
    try:
        try:
            project_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
        except OSError:
            raise ToolError(f"{project_path} is not a folder (symlinks are not followed)") from None
    finally:
        os.close(workspace_fd)
    try:
        try:
            st = os.stat(RUN_SCRIPT, dir_fd=project_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise OSError
        except OSError:
            raise ToolError(f"{project_path}/{RUN_SCRIPT} is missing (it must be a regular file)") from None

        script = f"{project_path}/{RUN_SCRIPT}"
        inner = (["foot", f"--app-id=sbx.{project}", f"--title=sbx {project}", "/bin/bash", script]
                 if terminal else ["/bin/bash", script])
        argv = bwrap_command(project_path, project_fd, inner, session_env(), project)

        stop_app(project)
        log_path = app_log_path(project)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        before = {c.get("address") for c in hypr_json("clients")}
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                    pass_fds=(project_fd,), start_new_session=True, cwd="/",
                                    env={"PATH": "/usr/bin:/bin"})
    finally:
        os.close(project_fd)
    APPS[project] = proc
    LAUNCHED.add(proc.pid)

    windows = stage_new_windows({proc.pid: proc}, before)
    state = "running" if proc.poll() is None else f"exited with code {proc.returncode}"
    notify("Sandbox-App gestartet", f"{project} (Workspace {STAGE})")
    text = (f"{project}: {state} (pid {proc.pid}), isolated with bubblewrap, "
            f"network {'on' if OPTS.app_network else 'off'}\n"
            f"windows: {'; '.join(windows) or 'none yet'}\n\n--- app output ---\n{tail_file(log_path, 40)}")
    return text_result(text, error=proc.poll() is not None and proc.returncode != 0)


def tool_app_logs(args):
    name = args.get("project")
    if not isinstance(name, str) or not PROJECT_RE.match(name):
        raise ToolError("project must be a project folder name or web-preview")
    proc = APPS.get(name)
    state = ("running" if proc and proc.poll() is None else
             f"exited with code {proc.returncode}" if proc else "not started by this bridge session")
    return text_result(f"{name}: {state}\n--- output ---\n" +
                       tail_file(app_log_path(name), int_arg(args, "lines", 100, 1, 2000)))


def tool_stop_app(args):
    name = args.get("project")
    if not isinstance(name, str) or not PROJECT_RE.match(name):
        raise ToolError("project must be a project folder name or web-preview")
    return text_result(f"{name} stopped" if stop_app(name) else f"{name} was not running")


def tool_web_preview(args):
    port = int_arg(args, "port", None, 1024, 65535)
    path = args.get("path", "/")
    if not isinstance(path, str) or not URL_PATH_RE.match(path):
        raise ToolError("path must start with / and contain only URL-safe characters")

    code, out, err = run(["sbx", "ports", OPTS.sandbox_name, "--publish", f"127.0.0.1:{port}:{port}"], timeout=30)
    if code != 0 and "already" not in (out + err).lower():
        raise ToolError("publishing the port failed: " + report(code, out, err))
    deadline = time.time() + 6
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if time.time() > deadline:
                raise ToolError(f"nothing answers on 127.0.0.1:{port}. The dev server in the sandbox must "
                                f"listen on 0.0.0.0:{port} (not 127.0.0.1).") from None
            time.sleep(0.3)

    before = {c.get("address") for c in hypr_json("clients")}
    env = session_env()
    env[APP_MARKER] = WEB_PREVIEW
    log_path = app_log_path(WEB_PREVIEW)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    profile = CACHE_DIR / "browser-profile"  # separate from the user's browser: no cookies, no logins
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(["chromium", f"--user-data-dir={profile}", "--no-first-run",
                                 "--no-default-browser-check", f"--app=http://127.0.0.1:{port}{path}"],
                                env=env, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=str(CACHE_DIR))
    LAUNCHED.add(proc.pid)
    roots = {proc.pid: proc}
    previous = APPS.get(WEB_PREVIEW)
    if previous and previous.poll() is None:
        roots[previous.pid] = previous  # an existing preview browser takes over the new window
    else:
        APPS[WEB_PREVIEW] = proc
    windows = stage_new_windows(roots, before)
    notify("Sandbox-Webvorschau", f"http://127.0.0.1:{port}{path} (Workspace {STAGE})")
    return text_result(f"preview of http://127.0.0.1:{port}{path}\n"
                       f"windows: {'; '.join(windows) or 'none found yet'}")


def tool_host_check(args):
    packages = args.get("packages", [])
    commands = args.get("commands", [])
    if (not isinstance(packages, list) or len(packages) > 40 or
            not all(isinstance(p, str) and PACKAGE_RE.match(p) for p in packages)):
        raise ToolError("packages must be a list of at most 40 package names")
    if (not isinstance(commands, list) or len(commands) > 40 or
            not all(isinstance(c, str) and COMMAND_RE.match(c) for c in commands)):
        raise ToolError("commands must be a list of at most 40 command names")
    lines = []
    if packages:
        _code, out, err = run(["pacman", "-Q", *packages], timeout=20)
        lines += [f"package {row}" for row in out.splitlines()]
        lines += [row.replace("error: ", "") for row in err.splitlines() if row.strip()]
    path = session_env()["PATH"]
    for command in commands:
        lines.append(f"command {command}: {shutil.which(command, path=path) or 'not found'}")
    return text_result("\n".join(lines) or "nothing to check")


NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
PROJECT_ARG = {"type": "object", "additionalProperties": False, "required": ["project"], "properties": {
    "project": {"type": "string", "description": "Project folder name in the workspace, or web-preview."}}}

TOOLS = {
    "status": (
        "Show what the bridge detected on the host (Hyprland instance, Wayland display, OMARCHY_PATH, "
        "shell reachability, screenshot mode, app network, running apps). Use it first if a tool fails.",
        NO_ARGS, tool_status, True),
    "restart_shell": (
        "Restart the Omarchy shell on the host (omarchy-restart-shell). Required after QML changes to a "
        "plugin: the automatic reload on save can keep stale QML. Returns the shell log since the restart.",
        NO_ARGS, tool_restart_shell, False),
    "rescan_plugins": (
        "Ask the running Omarchy shell to rescan the plugin folder (omarchy-shell shell rescanPlugins).",
        NO_ARGS, tool_rescan_plugins, False),
    "list_plugins": (
        "List the plugins the running Omarchy shell knows (omarchy-shell shell listPlugins).",
        NO_ARGS, tool_list_plugins, True),
    "plugin_ipc": (
        "Call an IPC function of a user plugin in ~/.config/omarchy/plugins (omarchy-shell <target> "
        "<method> [args]), e.g. to open its panel before a screenshot. The target is the plugin id, or "
        "<plugin_id>.<target_suffix> for plugins with several IPC targets (omaRSYNC: target_suffix=jobs).",
        {"type": "object", "additionalProperties": False, "required": ["plugin_id", "method"], "properties": {
            "plugin_id": {"type": "string"}, "method": {"type": "string"},
            "target_suffix": {"type": "string", "description": "Optional, e.g. jobs -> <plugin_id>.jobs"},
            "args": {"type": "array", "items": {"type": "string"}, "maxItems": 4}}},
        tool_plugin_ipc, False),
    "shell_logs": (
        "Read the Omarchy shell log from the host journal (journalctl --user -t omarchy-shell). "
        "QML console.log output and plugin errors appear here.",
        {"type": "object", "additionalProperties": False, "properties": {
            "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 150},
            "filter": {"type": "string", "maxLength": 200, "description": "Only lines containing this text."},
            "since_seconds": {"type": "integer", "minimum": 1, "maximum": 604800}}},
        tool_shell_logs, True),
    "validate_plugin": (
        "Validate a plugin folder in ~/.config/omarchy/plugins against the manifest schema the shell "
        "enforces (omarchy-plugin-validate).",
        {"type": "object", "additionalProperties": False, "required": ["plugin_id"], "properties": {
            "plugin_id": {"type": "string", "description": "Folder name, e.g. io.github.arikisonfire.rsync"}}},
        tool_validate_plugin, True),
    "qmllint": (
        "Lint a .qml file with the host's qmllint (same Qt as the shell), with qs.* imports resolved "
        "against the Omarchy shell. The file must be inside ~/.config/omarchy/plugins/<plugin>/ or "
        "<workspace>/<project>/.",
        {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path, or relative to the plugins folder."}}},
        tool_qmllint, True),
    "hypr_query": (
        "Read-only Hyprland query as JSON (hyprctl -j <what>). Use configerrors after editing "
        "~/.config/hypr, getoption with option=<name> for a single setting.",
        {"type": "object", "additionalProperties": False, "required": ["what"], "properties": {
            "what": {"type": "string", "enum": HYPR_QUERIES},
            "option": {"type": "string", "description": "Option name for getoption, e.g. general:gaps_in"}}},
        tool_hypr_query, True),
    "hypr_reload": (
        "Reload the Hyprland configuration (hyprctl reload) and return configerrors.",
        NO_ARGS, tool_hypr_reload, False),
    "stage_list": (
        f"List development windows (apps the bridge started, Omarchy shell windows) and everything on "
        f"the stage workspace '{STAGE}', with their addresses.",
        NO_ARGS, tool_stage_list, True),
    "stage_window": (
        f"Move a development window to the stage workspace '{STAGE}' without switching to it.",
        {"type": "object", "additionalProperties": False, "required": ["address"], "properties": {
            "address": {"type": "string", "description": "Window address from stage_list, e.g. 0x55d0c0ffee00"}}},
        tool_stage_window, False),
    "screenshot": (
        "Capture the screen and return the image. target=bar: top bar strip, no workspace switch. "
        f"target=stage: briefly switches to the empty workspace '{STAGE}' (only development windows "
        "allowed there), captures the whole monitor (bar, shell panels, staged apps), switches back. "
        "target=window: same, cropped to one development window (address). screen and geometry only if "
        "the user allowed them. Waits while notifications, clipboard, polkit or lock overlays are open. "
        "The user gets a notification for every capture.",
        {"type": "object", "additionalProperties": False, "properties": {
            "target": {"type": "string", "enum": ["bar", "stage", "window", "screen", "geometry"],
                       "default": "bar"},
            "address": {"type": "string", "description": "For target=window: window address from stage_list."},
            "geometry": {"type": "string", "description": "For target=geometry: 'X,Y WxH' in logical px."}}},
        tool_screenshot, True),
    "launch_app": (
        f"Start an app from <workspace>/<project>/{RUN_SCRIPT} on the host, isolated with bubblewrap: "
        "it sees only its project folder (read-write), /usr, /etc, the Wayland display, the GPU and the "
        "desktop theme; no home, no D-Bus, no audio, no network unless the user allowed it. It runs with "
        "the host's (Arch) libraries, not the sandbox's. Its windows go to the stage workspace. "
        "terminal=true runs the script inside a foot terminal (for TUI apps). Restarts a running instance.",
        {"type": "object", "additionalProperties": False, "required": ["project"], "properties": {
            "project": {"type": "string"}, "terminal": {"type": "boolean", "default": False}}},
        tool_launch_app, False),
    "app_logs": (
        "Read the stdout/stderr of an app started with launch_app (or of web-preview).",
        {"type": "object", "additionalProperties": False, "required": ["project"], "properties": {
            "project": {"type": "string"}, "lines": {"type": "integer", "minimum": 1, "maximum": 2000}}},
        tool_app_logs, True),
    "stop_app": (
        "Stop an app started with launch_app (or the web-preview browser).",
        PROJECT_ARG, tool_stop_app, False),
    "web_preview": (
        "Show a web app whose dev server runs inside the sandbox: publishes the sandbox port to the "
        "host's 127.0.0.1 (sbx ports) and opens it in a separate Chromium profile on the stage workspace. "
        "The dev server must listen on 0.0.0.0:<port> inside the sandbox.",
        {"type": "object", "additionalProperties": False, "required": ["port"], "properties": {
            "port": {"type": "integer", "minimum": 1024, "maximum": 65535},
            "path": {"type": "string", "default": "/"}}},
        tool_web_preview, False),
    "host_check": (
        "Check which Arch packages (pacman -Q) and commands exist on the host, to build apps that run "
        "with the host's libraries.",
        {"type": "object", "additionalProperties": False, "properties": {
            "packages": {"type": "array", "items": {"type": "string"}, "maxItems": 40},
            "commands": {"type": "array", "items": {"type": "string"}, "maxItems": 40}}},
        tool_host_check, True),
}


# ---------------------------------------------------------------- MCP plumbing

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def tools_list():
    return [{"name": name, "description": desc, "inputSchema": schema,
             "annotations": {"readOnlyHint": readonly}}
            for name, (desc, schema, _fn, readonly) in TOOLS.items()]


def handle(msg):
    if not isinstance(msg, dict) or "method" not in msg:
        return None  # responses to requests we never send, or junk without an id
    method, msg_id = msg["method"], msg.get("id")
    if msg_id is None:
        return None  # notification

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def fail(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    params = msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return ok({"protocolVersion": requested if requested in PROTOCOL_VERSIONS else "2025-06-18",
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "omarchy-bridge", "version": VERSION},
                   "instructions": "Host bridge for Omarchy development from a Docker Sandbox. "
                                   "Call status first if anything fails."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools_list()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS or not isinstance(args, dict):
            return fail(-32602, f"unknown tool: {name}")
        started = time.time()
        try:
            result = TOOLS[name][2](args)
        except ToolError as e:
            result = text_result(str(e), error=True)
        except Exception as e:  # keep the server alive on unexpected failures
            result = text_result(f"internal error: {type(e).__name__}: {e}", error=True)
        log(f"{name} {json.dumps(args, ensure_ascii=False)[:300]} "
            f"-> {'error' if result.get('isError') else 'ok'} ({time.time() - started:.1f}s)")
        return ok(result)
    return fail(-32601, f"method not found: {method}")


def send(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def serve():
    log(f"started (pid {os.getpid()}, fullscreen={OPTS.allow_fullscreen}, "
        f"screenshots={'off' if OPTS.no_screenshots else 'on'}, app network={OPTS.app_network})")
    try:
        for raw in sys.stdin.buffer:
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(msg, list):
                replies = [r for r in map(handle, msg) if r]
                if replies:
                    send(replies)
            else:
                reply = handle(msg)
                if reply:
                    send(reply)
    finally:
        for name in list(APPS):
            stop_app(name)
        log("stdin closed, apps stopped, exiting")


def main():
    parser = argparse.ArgumentParser(description="Omarchy host bridge for Docker Sandboxes (MCP stdio).")
    parser.add_argument("--allow-fullscreen", action="store_true",
                        help="allow screenshot targets 'screen' and 'geometry' (whatever is on screen)")
    parser.add_argument("--no-screenshots", action="store_true", help="turn the screenshot tool off")
    parser.add_argument("--app-network", action="store_true",
                        help="give apps started with launch_app network access (default: none)")
    parser.add_argument("--workspace", type=Path, default=OPTS.workspace,
                        help="sandbox workspace with app projects (default: ~/Work/omarchy-sbx)")
    parser.add_argument("--sandbox-name", default=OPTS.sandbox_name,
                        help="sbx sandbox whose ports web_preview publishes (default: omarchy)")
    parser.add_argument("--check", action="store_true", help="print the detected session and exit")
    parsed = parser.parse_args()
    if not PROJECT_RE.match(parsed.sandbox_name):
        parser.error("invalid --sandbox-name")
    OPTS.allow_fullscreen = parsed.allow_fullscreen
    OPTS.no_screenshots = parsed.no_screenshots
    OPTS.app_network = parsed.app_network
    OPTS.workspace = parsed.workspace.expanduser()
    OPTS.sandbox_name = parsed.sandbox_name
    if parsed.check:
        print(tool_status({})["content"][0]["text"])
        return
    serve()


if __name__ == "__main__":
    main()
