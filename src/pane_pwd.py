#!/usr/bin/env python3
"""pane-pwd: keep every Herdr pane's border label showing that pane's cwd.

Herdr renders a pane's *manual label* on its split border, and exposes each
pane's live directory as `foreground_cwd` on `herdr pane list`. This script is
the loop between those two facts.

Subcommands:
    sync      one full reconciliation over the live pane list
    start     spawn the detached watcher and exit (used by the startup hook)
    stop      stop the watcher and clear every label this plugin owns
    clear     clear owned labels, leave the watcher alone
    status    print watcher state, resolved config, and owned labels
    daemon    the watcher loop itself; `start` spawns this, don't run it by hand

Ownership: a label is only overwritten while it still matches what this plugin
last wrote. Rename a pane yourself and the plugin hands that pane back to you
permanently (until the label is cleared), so `herdr pane rename` keeps working
as a manual override.
"""

import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ID = "pane-pwd"

# Herdr's own pane-id shape, used to reject anything unexpected from the API
# before it is passed back into a mutating command.
PANE_ID_RE = re.compile(r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")

CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")

DEFAULTS = {
    # Poll interval. Herdr emits no cwd-change event, so this is the ceiling on
    # how stale a label can be after a plain `cd`.
    "interval_ms": "1000",
    # Label style for every pane: basename | repo | short | full.
    "style": "short",
    # Label style for the focused pane. Set it to something longer than
    # `style` if you want more context only where you are actually looking.
    "focused_style": "short",
    # Truncated from the left, keeping the most specific trailing components.
    "max_len": "40",
    # Show the git branch after the path: off | on | focused.
    "branch": "off",
    # How the branch is appended; `{branch}` is replaced with its name.
    "branch_format": " ({branch})",
    # Truncated from the right, keeping the leading `feature/ABC-1234` part.
    "branch_max_len": "20",
    # Literal text prepended after truncation, e.g. "> " or an icon.
    "prefix": "",
    # How $HOME itself is rendered.
    "home_label": "~",
    # Leave panes you renamed by hand alone.
    "respect_manual": "true",
    # Comma-separated pane ids to never touch.
    "skip_panes": "",
    # A single-pane tab has no border to draw on, so fall back to the tab
    # label there: single | always | off.
    "tab_labels": "single",
    # Consecutive `herdr pane list` failures before the watcher gives up. A
    # stopped or version-mismatched server should end the loop, not spin on it.
    "max_failures": "10",
}

VALID_STYLES = ("basename", "repo", "short", "full")
VALID_TAB_MODES = ("single", "always", "off")
VALID_BRANCH_MODES = ("off", "on", "focused")

DEFAULT_BRANCH_FORMAT = " ({branch})"

# A path keeps at least this many characters before the branch is shortened
# further to fit MAX_LEN, so a long branch cannot squeeze the path to nothing.
MIN_PATH_LEN = 8


class HerdrError(RuntimeError):
    """A `herdr` CLI call failed or returned something unusable."""


# --------------------------------------------------------------------------
# Environment and paths
# --------------------------------------------------------------------------

def plugin_root():
    root = os.environ.get("HERDR_PLUGIN_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent


def _dir_from_env(var, fallback_name):
    raw = os.environ.get(var)
    path = Path(raw) if raw else plugin_root() / ".local" / fallback_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir():
    return _dir_from_env("HERDR_PLUGIN_CONFIG_DIR", "config")


def state_dir():
    return _dir_from_env("HERDR_PLUGIN_STATE_DIR", "state")


def pid_file():
    return state_dir() / "watcher.pid"


def log_file():
    return state_dir() / "watcher.log"


def state_file():
    return state_dir() / "owned.json"


def lock_file():
    return state_dir() / "sync.lock"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    """Defaults, overlaid by config.env, overlaid by HERDR_PANE_PWD_* env vars."""
    values = dict(DEFAULTS)

    path = config_dir() / "config.env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key in values:
                values[key] = value.strip().strip('"').strip("'")

    for key in list(values):
        override = os.environ.get("HERDR_PANE_PWD_" + key.upper())
        if override is not None:
            values[key] = override

    cfg = {
        "interval_ms": _clamp_int(values["interval_ms"], 100, 3600000, 1000),
        "style": _one_of(values["style"], VALID_STYLES, "short"),
        "focused_style": _one_of(values["focused_style"], VALID_STYLES, "short"),
        "max_len": _clamp_int(values["max_len"], 4, 80, 40),
        "branch": _one_of(values["branch"], VALID_BRANCH_MODES, "off"),
        "branch_format": _branch_format(values["branch_format"]),
        "branch_max_len": _clamp_int(values["branch_max_len"], 4, 80, 20),
        "prefix": CONTROL_CHARS_RE.sub("", values["prefix"]),
        "home_label": CONTROL_CHARS_RE.sub("", values["home_label"]) or "~",
        "respect_manual": _truthy(values["respect_manual"]),
        "skip_panes": {p.strip() for p in values["skip_panes"].split(",") if p.strip()},
        "tab_labels": _one_of(values["tab_labels"], VALID_TAB_MODES, "single"),
        "max_failures": _clamp_int(values["max_failures"], 1, 10000, 10),
    }
    return cfg


def _clamp_int(raw, low, high, fallback):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, value))


def _one_of(raw, allowed, fallback):
    value = str(raw).strip().lower()
    return value if value in allowed else fallback


def _truthy(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _unescape(raw):
    """Turn `\\uXXXX` and `\\UXXXXXXXX` into the characters they name.

    Lets a config name a Nerd Font glyph by code point instead of pasting a
    character that many editors and chat tools silently drop. Anything that is
    not a valid code point is left as typed.
    """
    def replace(match):
        code = int(match.group(1) or match.group(2), 16)
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return match.group(0)
        return chr(code)
    return UNICODE_ESCAPE_RE.sub(replace, raw)


def _branch_format(raw):
    # Control characters are stripped after unescaping, so `\u001b` cannot
    # smuggle an escape byte onto the border.
    value = CONTROL_CHARS_RE.sub("", _unescape(raw))
    # Without the placeholder the branch would silently never appear.
    return value if "{branch}" in value else DEFAULT_BRANCH_FORMAT


# --------------------------------------------------------------------------
# Herdr CLI
# --------------------------------------------------------------------------

def herdr(args, timeout=15):
    """Run a herdr CLI command, returning stdout. Raises HerdrError on failure."""
    exe = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        proc = subprocess.run(
            [exe] + list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except OSError as exc:
        raise HerdrError("cannot run %s: %s" % (exe, exc))
    except subprocess.TimeoutExpired:
        raise HerdrError("herdr %s timed out" % " ".join(args))

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise HerdrError("herdr %s failed (%d): %s" % (" ".join(args), proc.returncode, detail[:400]))
    return proc.stdout.decode("utf-8", "replace")


def list_panes():
    """Every pane in the session, as PaneInfo dicts."""
    raw = herdr(["pane", "list"])
    try:
        panes = json.loads(raw)["result"]["panes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HerdrError("unreadable pane list: %s" % exc)
    if not isinstance(panes, list):
        raise HerdrError("pane list result is not an array")
    return panes


def list_workspaces():
    raw = herdr(["workspace", "list"])
    try:
        return json.loads(raw)["result"]["workspaces"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HerdrError("unreadable workspace list: %s" % exc)


def list_tabs(workspace_id):
    raw = herdr(["tab", "list", "--workspace", workspace_id])
    try:
        return json.loads(raw)["result"]["tabs"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HerdrError("unreadable tab list: %s" % exc)


def rename_tab(tab_id, label):
    herdr(["tab", "rename", tab_id, label])


def rename_pane(pane_id, label):
    herdr(["pane", "rename", pane_id, label])


def clear_pane(pane_id):
    herdr(["pane", "rename", pane_id, "--clear"])


# --------------------------------------------------------------------------
# Label rendering
# --------------------------------------------------------------------------

def _home():
    try:
        return Path(os.path.expanduser("~")).resolve()
    except OSError:
        return None


def _collapse_home(path):
    home = _home()
    if home is None:
        return str(path)
    try:
        if path == home:
            return None  # caller substitutes home_label
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _checkout_root(path):
    """The nearest directory at or above `path` holding a `.git` entry.

    Walks for `.git` rather than shelling out to git: this runs once per pane
    per tick, and a subprocess per pane would dominate the cost.
    """
    for candidate in [path] + list(path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _repo_label(path):
    """`<repo>/<subpath>` when inside a git checkout, else None."""
    root = _checkout_root(path)
    if root is None:
        return None
    if root == path:
        return root.name
    return root.name + "/" + str(path.relative_to(root))


def _git_branch(path):
    """The checked-out branch, a short commit hash when detached, else None.

    Reads HEAD directly for the same reason `_checkout_root` avoids git. In a
    worktree or submodule `.git` is a file holding `gitdir: <path>`, and HEAD
    lives in that directory instead.
    """
    root = _checkout_root(path)
    if root is None:
        return None
    dotgit = root / ".git"
    try:
        if dotgit.is_file():
            pointer = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return None
            dotgit = root / pointer[len("gitdir:"):].strip()
        head = (dotgit / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        return head[:7]
    return None


def _branch_suffix(path, text_len, cfg):
    """The formatted branch suffix, shortened to leave the path some room."""
    branch = _git_branch(path)
    if not branch:
        return ""
    branch = CONTROL_CHARS_RE.sub("", branch)
    overhead = len(cfg["branch_format"]) - len("{branch}")
    room = cfg["max_len"] - min(text_len, MIN_PATH_LEN) - overhead
    budget = min(cfg["branch_max_len"], room)
    if budget < 2:
        return ""  # not even one character plus "…" fits
    if len(branch) > budget:
        # Elide from the right: the leading `feature/ABC-1234` identifies it.
        branch = branch[:budget - 1] + "…"
    return cfg["branch_format"].replace("{branch}", branch)


def render_label(cwd, style, cfg, focused=False):
    """Turn an absolute directory into the text drawn on the pane border."""
    try:
        path = Path(cwd)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        return None

    home = _home()
    if style == "basename":
        if home is not None and path == home:
            text = cfg["home_label"]
        else:
            text = path.name or str(path)
    elif style == "repo":
        text = _repo_label(path)
        if text is None:
            collapsed = _collapse_home(path)
            text = cfg["home_label"] if collapsed is None else collapsed
    elif style == "full":
        text = str(path)
    else:  # short
        collapsed = _collapse_home(path)
        text = cfg["home_label"] if collapsed is None else collapsed

    text = CONTROL_CHARS_RE.sub("", text).strip()
    if not text:
        text = str(path)

    suffix = ""
    if cfg["branch"] == "on" or (cfg["branch"] == "focused" and focused):
        suffix = _branch_suffix(path, len(text), cfg)

    budget = cfg["max_len"] - len(suffix)
    if len(text) > budget:
        # Elide from the left: the trailing components identify the folder.
        text = "…" + text[-(budget - 1):]

    label = cfg["prefix"] + text + suffix
    # A leading dash would be read as a flag by `herdr pane rename`.
    label = label.lstrip("-").strip()
    return label or None


# --------------------------------------------------------------------------
# Owned-label state
# --------------------------------------------------------------------------

def _load_map(data, key):
    section = data.get(key) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if isinstance(k, str) and isinstance(v, str)}


def load_owned():
    path = state_file()
    if not path.is_file():
        return {}
    try:
        return _load_map(json.loads(path.read_text(encoding="utf-8")), "labels")
    except (ValueError, OSError):
        return {}


def load_owned_tabs():
    path = state_file()
    if not path.is_file():
        return {}
    try:
        return _load_map(json.loads(path.read_text(encoding="utf-8")), "tabs")
    except (ValueError, OSError):
        return {}


def load_tab_previous():
    """The generated tab name each claimed tab had before pane-pwd took it."""
    path = state_file()
    if not path.is_file():
        return {}
    try:
        return _load_map(json.loads(path.read_text(encoding="utf-8")), "tab_previous")
    except (ValueError, OSError):
        return {}


def save_owned(labels, tabs=None, tab_previous=None):
    if tabs is None:
        tabs = load_owned_tabs()
    if tab_previous is None:
        tab_previous = load_tab_previous()
    path = state_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"labels": labels, "tabs": tabs, "tab_previous": tab_previous},
                              indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

class sync_lock(object):
    """Serialize reconciliation across processes.

    The watcher, the `sync` action, and three event hooks can all fire at
    once, and an unlocked pass reads `owned.json` before a concurrent pass
    writes it. The stale reader then sees its own labels as unrecognized,
    concludes they were set by hand, and releases those panes for good.
    """

    def __enter__(self):
        self.handle = lock_file().open("w")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        return False


def sync(cfg):
    """One full pass: every pane's label made to match its directory.

    Returns (renamed, skipped_manual, total, tabs_renamed).
    """
    with sync_lock():
        return _sync_locked(cfg)


def _sync_locked(cfg):
    panes = list_panes()
    owned = load_owned()
    renamed = 0
    manual = 0
    live = set()

    for pane in panes:
        pane_id = pane.get("pane_id")
        if not isinstance(pane_id, str) or not PANE_ID_RE.match(pane_id):
            continue
        live.add(pane_id)
        if pane_id in cfg["skip_panes"]:
            continue

        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        if not cwd:
            continue

        focused = bool(pane.get("focused"))
        style = cfg["focused_style"] if focused else cfg["style"]
        desired = render_label(cwd, style, cfg, focused)
        if desired is None:
            continue

        current = pane.get("label") or ""
        mine = owned.get(pane_id)

        if current and current != mine and current != desired:
            # Someone renamed this pane by hand. Release it for good.
            if cfg["respect_manual"]:
                owned.pop(pane_id, None)
                manual += 1
                continue
        elif current == desired:
            # Already reads correctly. Claim it even if state has no record,
            # so a lost or stale owned.json re-adopts rather than abandons.
            owned[pane_id] = desired
            continue

        try:
            rename_pane(pane_id, desired)
        except HerdrError as exc:
            # A pane can close between the list and the rename; that is normal.
            log("rename %s failed: %s" % (pane_id, exc))
            owned.pop(pane_id, None)
            continue
        owned[pane_id] = desired
        renamed += 1

    for pane_id in [p for p in owned if p not in live]:
        owned.pop(pane_id)

    tabs_renamed = _sync_tabs(cfg, panes)
    save_owned(owned)
    return renamed, manual, len(panes), tabs_renamed


def _tab_is_auto_named(tab):
    """True when the tab still carries a Herdr-generated name.

    Generated names are a plain display counter. They are NOT the `number`
    field: a tab can report number=4 while Herdr shows it as "2", so any
    all-digit label counts as automatic rather than hand-chosen.
    """
    label = (tab.get("label") or "").strip()
    return label == "" or label.isdigit()


def _sync_tabs(cfg, panes):
    """Label tabs that have no pane border to draw on.

    A single-pane tab reserves no border (its pane rect fills the whole tab
    area), so the tab label is the only always-visible surface for it.
    """
    mode = cfg["tab_labels"]
    if mode == "off":
        return 0

    by_tab = {}
    for pane in panes:
        by_tab.setdefault(pane.get("tab_id"), []).append(pane)

    owned_tabs = load_owned_tabs()
    previous = load_tab_previous()
    renamed = 0
    live = set()

    for workspace in list_workspaces():
        ws_id = workspace.get("workspace_id")
        if not isinstance(ws_id, str):
            continue
        for tab in list_tabs(ws_id):
            tab_id = tab.get("tab_id")
            if not isinstance(tab_id, str):
                continue
            live.add(tab_id)
            members = by_tab.get(tab_id) or []
            if mode == "single" and len(members) != 1:
                # A split tab shows the directory on its pane borders already.
                if tab_id in owned_tabs and _restore_tab(tab, owned_tabs, previous):
                    renamed += 1
                continue
            if not members:
                continue

            # The focused pane speaks for the tab; otherwise the only one does.
            source = next((m for m in members if m.get("focused")), members[0])
            cwd = source.get("foreground_cwd") or source.get("cwd")
            if not cwd:
                continue
            desired = render_label(cwd, cfg["focused_style"], cfg, focused=True)
            if desired is None:
                continue

            current = (tab.get("label") or "").strip()
            mine = owned_tabs.get(tab_id)
            if current == desired:
                owned_tabs[tab_id] = desired
                continue
            if current and current != mine and not _tab_is_auto_named(tab):
                if cfg["respect_manual"]:
                    owned_tabs.pop(tab_id, None)
                    continue
            try:
                rename_tab(tab_id, desired)
            except HerdrError as exc:
                log("tab rename %s failed: %s" % (tab_id, exc))
                owned_tabs.pop(tab_id, None)
                continue
            # Remember the generated name so the tab can be handed back to it
            # exactly, rather than to a guess derived from `number`.
            previous.setdefault(tab_id, current or str(tab.get("number")))
            owned_tabs[tab_id] = desired
            renamed += 1

    for tab_id in [t for t in owned_tabs if t not in live]:
        owned_tabs.pop(tab_id)
    for tab_id in [t for t in previous if t not in live]:
        previous.pop(tab_id)

    save_owned(load_owned(), owned_tabs, previous)
    return renamed


def _restore_tab(tab, owned_tabs, previous):
    """Hand a tab back to the generated name it had before we claimed it."""
    tab_id = tab["tab_id"]
    if (tab.get("label") or "").strip() != owned_tabs.get(tab_id):
        owned_tabs.pop(tab_id, None)
        previous.pop(tab_id, None)
        return False
    try:
        rename_tab(tab_id, previous.get(tab_id) or str(tab.get("number")))
    except HerdrError as exc:
        log("tab restore %s failed: %s" % (tab_id, exc))
        return False
    owned_tabs.pop(tab_id, None)
    previous.pop(tab_id, None)
    return True


def clear_owned(cfg):
    """Drop every label this plugin still owns, leaving manual ones intact."""
    with sync_lock():
        return _clear_owned_locked()


def _clear_owned_locked():
    owned = load_owned()
    if not owned and not load_owned_tabs():
        return 0
    try:
        current = {
            p.get("pane_id"): (p.get("label") or "")
            for p in list_panes()
            if isinstance(p.get("pane_id"), str)
        }
    except HerdrError as exc:
        log("clear: %s" % exc)
        save_owned({})
        return 0

    cleared = 0
    for pane_id, label in owned.items():
        if current.get(pane_id) != label:
            continue  # gone, or re-renamed by hand
        try:
            clear_pane(pane_id)
        except HerdrError as exc:
            log("clear %s failed: %s" % (pane_id, exc))
            continue
        cleared += 1

    owned_tabs = load_owned_tabs()
    previous = load_tab_previous()
    if owned_tabs:
        for workspace in list_workspaces():
            ws_id = workspace.get("workspace_id")
            if not isinstance(ws_id, str):
                continue
            for tab in list_tabs(ws_id):
                if tab.get("tab_id") in owned_tabs and _restore_tab(tab, owned_tabs, previous):
                    cleared += 1

    save_owned({}, {}, {})
    return cleared


# --------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------

_stop = False


def _handle_signal(_signum, _frame):
    global _stop
    _stop = True


def read_pid():
    try:
        return int(pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        with log_file().open("a", encoding="utf-8") as handle:
            handle.write(line)
            # Keep the log from growing without bound across long sessions.
            if handle.tell() > 256 * 1024:
                handle.truncate(0)
    except OSError:
        pass


def socket_gone():
    path = os.environ.get("HERDR_SOCKET_PATH")
    # On Windows this is a named pipe, not a filesystem path; the plugin is
    # unix-only, but don't treat a non-path as a dead server either way.
    return bool(path) and path.startswith("/") and not os.path.exists(path)


def daemon(cfg):
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    me = os.getpid()
    pid_file().write_text(str(me), encoding="utf-8")
    log("watcher %d started (interval %dms)" % (me, cfg["interval_ms"]))

    failures = 0
    interval = cfg["interval_ms"] / 1000.0

    while not _stop:
        if read_pid() != me:
            log("watcher %d superseded" % me)
            break
        if socket_gone():
            log("herdr socket gone; watcher %d exiting" % me)
            break
        try:
            sync(cfg)
            failures = 0
        except HerdrError as exc:
            failures += 1
            log("sync failed (%d/%d): %s" % (failures, cfg["max_failures"], exc))
            if failures >= cfg["max_failures"]:
                log("giving up after %d consecutive failures" % failures)
                break

        # Sleep in slices so a SIGTERM is acted on promptly.
        deadline = time.time() + interval
        while not _stop and time.time() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.time())))

    if read_pid() == me:
        try:
            pid_file().unlink()
        except OSError:
            pass
    log("watcher %d stopped" % me)
    return 0


def start(cfg, force):
    existing = read_pid()
    if pid_alive(existing):
        if not force:
            print("pane-pwd: watcher already running (pid %d)" % existing)
            return 0
        stop(cfg, clear=False)

    # Label everything once up front so the first tick is not a visible delay.
    try:
        sync(cfg)
    except HerdrError as exc:
        print("pane-pwd: initial sync failed: %s" % exc, file=sys.stderr)

    handle = log_file().open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            cwd=str(plugin_root()),
            env=os.environ.copy(),
            start_new_session=True,
        )
    finally:
        handle.close()
    print("pane-pwd: watcher started (pid %d)" % proc.pid)
    return 0


def stop(cfg, clear=True):
    pid = read_pid()
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 3.0
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.05)
    try:
        pid_file().unlink()
    except OSError:
        pass

    if clear:
        cleared = clear_owned(cfg)
        print("pane-pwd: watcher stopped, %d label(s) cleared" % cleared)
    return 0


def status(cfg):
    pid = read_pid()
    print(json.dumps({
        "plugin_id": PLUGIN_ID,
        "running": pid_alive(pid),
        "pid": pid,
        "config": {k: (sorted(v) if isinstance(v, set) else v) for k, v in cfg.items()},
        "config_dir": str(config_dir()),
        "state_dir": str(state_dir()),
        "owned": load_owned(),
    }, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------

def main(argv):
    command = argv[0] if argv else "sync"
    force = "--force" in argv[1:]
    cfg = load_config()

    try:
        if command == "sync":
            renamed, manual, total, tabs = sync(cfg)
            print("pane-pwd: %d/%d pane(s) relabelled, %d tab(s), %d left to manual names"
                  % (renamed, total, tabs, manual))
            return 0
        if command == "daemon":
            return daemon(cfg)
        if command == "start":
            return start(cfg, force)
        if command == "stop":
            return stop(cfg)
        if command == "clear":
            print("pane-pwd: %d label(s) cleared" % clear_owned(cfg))
            return 0
        if command == "status":
            return status(cfg)
    except HerdrError as exc:
        print("pane-pwd: %s" % exc, file=sys.stderr)
        return 1

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
