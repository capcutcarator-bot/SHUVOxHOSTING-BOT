import os
import sys
import json
import ast
import importlib.util
import time
import uuid
import signal
import shutil
import zipfile
import subprocess
import html
import hashlib
import socket
import re
import urllib.request
import urllib.parse
import urllib.error
import threading
import platform
import tarfile
import io
import tempfile
import contextlib
import atexit

import telebot
from telebot import types

import tursodb

# ================= HARDCODED CONFIG (edit here) =================

BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8624547534:AAE-vyRd2aE4Ze_r9NX9mPoKqx3DISP8xlU"

# Your Telegram numeric user id (get from @userinfobot). Only this id can control the bot.
ADMIN_ID = 8600328303

# Where hosted projects live (relative to bot.py, auto-created)
STORAGE_DIR = "hosted_projects"

# Where state (pids, entry files, etc) is saved
DATA_FILE = "data.json"
GITHUB_TOKEN_FILE = ".github_token"

# Max upload size accepted (Telegram bot API hard cap is 20MB for bots anyway)
MAX_FILE_MB = 20

# Python binary used to run hosted projects (Termux default is python3)
PYTHON_BIN = "python3"

# Common entry-file names to auto-detect, in priority order
ENTRY_CANDIDATES = [
    "main.py", "bot.py", "app.py", "run.py", "server.py", "start.py",
    "index.js", "server.js", "app.js", "start.js", "index.mjs", "server.mjs", "app.mjs",
]

# API hosting defaults. Projects that read PORT will receive an available port.
DEFAULT_API_PORT = 8000
API_PORT_RANGE = range(8000, 9000)
PORT_DETECT_SECONDS = 8
# Optional template for a real public URL, e.g. "https://example.com:{port}".
# Leave empty to show the local URL only.
PUBLIC_URL_TEMPLATE = ""
ENABLE_CLOUDFLARED_TUNNEL = True

# ---- Turso database: persistent state + project file backups (survives restarts/redeploys) ----
# Env vars TURSO_URL / TURSO_TOKEN override these if you ever set them on the host.
TURSO_URL = os.environ.get("TURSO_URL") or "libsql://shuvoxsite-asraful.aws-ap-northeast-1.turso.io"
TURSO_TOKEN = os.environ.get("TURSO_TOKEN") or "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTQ3MjA2ODEsImlkIjoiMDFhMTdiZmUtZWIwMS03ZWNjLTk3M2UtMGE0YWQwYjkyMWRjIiwia2lkIjoiSHZuR3FheVRscmtSQWZfbFQyaGM4aXUtNDBhZzJmMzJ2R1BCRFNlaUxqbyIsInJpZCI6IjQ1ODg0MjUwLTcxMWMtNGQ2OS1hMTBlLWRjNmZkNzY1NTQ2ZCJ9.8R8J4KejLnKDXJutIkwOkwjJtM_PqP8Jo-vgXdqVazFyy_If1pWa0cdEcdp19X4vkJ4EB8zXTEQUGLcaJ9x8Cw"

# ---- Public mode ----
# True  = anyone can use the bot; every user only sees/controls their own projects.
# False = only admins and users added with /grant. Admin can flip it live with /public on|off.
PUBLIC_MODE = True
MAX_PROJECTS_PER_USER = 5     # admins are unlimited; change one user with /setlimit USER_ID N
BACKUP_MINUTES = 5            # how often changed project files are re-saved to Turso
AUTO_UPDATE_MINUTES = 10      # how often GitHub projects with Auto-update ON are checked
MAX_BACKUP_MB = 50            # max compressed size of one project backup

# ===================================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=16)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_PATH = os.path.join(BASE_DIR, STORAGE_DIR)
DATA_PATH = os.path.join(BASE_DIR, DATA_FILE)

os.makedirs(STORAGE_PATH, exist_ok=True)

# in-memory: waiting for user input
awaiting_entry = {}
awaiting_github = set()
awaiting_github_token = set()
awaiting_env = {}   # user_id -> project id
_repo_cache = {}    # user_id -> last GitHub repo list


def admin_ids(data=None):
    data = data if data is not None else load_data()
    return {ADMIN_ID, *[int(uid) for uid in data.get("admins", []) if str(uid).isdigit()]}


def hosting_user_ids(data=None):
    data = data if data is not None else load_data()
    return {int(uid) for uid in data.get("hosting_users", []) if str(uid).isdigit()}


# ---------------- persistent state: Turso (+ local data.json mirror) ----------------
# ONE live dict (_state) is shared by every thread, so concurrent handlers can never
# overwrite each other with stale snapshots. save_data() marks it dirty and a writer
# thread pushes it to Turso (and to data.json as a local mirror) within ~half a second.

store = None
_state = None
_state_lock = threading.RLock()
_dirty = threading.Event()
_writer_started = False


def _normalize_state(d):
    if not isinstance(d, dict):
        d = {}
    d.setdefault("projects", {})
    for key in ("hosting_users", "admins", "banned"):
        d.setdefault(key, [])
    for key in ("settings", "users", "github_tokens", "limits"):
        d.setdefault(key, {})
    d.pop("github_repo_cache", None)  # kept in memory only now
    return d


def _read_local_state():
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_local_state(payload):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, DATA_PATH)


def init_storage():
    global store, _state
    with _state_lock:
        if _state is not None:
            return
        local = _read_local_state()
        raw = None
        if TURSO_URL and TURSO_TOKEN:
            store = tursodb.Store(TURSO_URL, TURSO_TOKEN)
            last_error = None
            for attempt in range(5):
                try:
                    store.init()
                    raw = store.get_kv("state")
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    print(f"Turso not reachable (try {attempt + 1}/5): {exc}")
                    time.sleep(3 * (attempt + 1))
            if last_error is not None:
                if local is None:
                    print("FATAL: cannot reach Turso and there is no local data.json. Refusing to start with "
                          "an empty state (it would overwrite your saved data). Check TURSO_URL / TURSO_TOKEN "
                          "and the internet connection.")
                    sys.exit(1)
                print("WARNING: running from local data.json because Turso is unreachable.")
        if raw:
            try:
                _state = _normalize_state(json.loads(raw))
                print(f"State loaded from Turso ({len(_state['projects'])} projects).")
            except ValueError:
                raw = None
        if not raw:
            _state = _normalize_state(local)
            if store:
                print(f"Turso state is empty - migrating local data ({len(_state['projects'])} projects).")
        # one-time migration of the old single global GitHub token file -> admin's token
        legacy = ""
        try:
            with open(os.path.join(BASE_DIR, GITHUB_TOKEN_FILE), "r", encoding="utf-8") as f:
                legacy = f.read().strip()
        except OSError:
            pass
        if legacy and str(ADMIN_ID) not in _state["github_tokens"]:
            _state["github_tokens"][str(ADMIN_ID)] = legacy
        _dirty.set()
    start_state_writer()


def _flush_state():
    payload = None
    for _ in range(10):
        try:
            with _state_lock:
                payload = json.dumps(_state)
            break
        except RuntimeError:  # another thread changed a dict while it was being serialised
            time.sleep(0.05)
    if payload is None:
        return False
    try:
        _write_local_state(payload)
    except OSError as exc:
        print(f"Local state write failed: {exc}")
    if store:
        try:
            store.set_kv("state", payload)
        except Exception as exc:
            print(f"Turso save failed (will retry): {exc}")
            return False
    return True


def _state_writer():
    while True:
        _dirty.wait()
        time.sleep(0.4)  # coalesce bursts of saves into one write
        _dirty.clear()
        if not _flush_state():
            _dirty.set()
            time.sleep(5)


def start_state_writer():
    global _writer_started
    if not _writer_started:
        _writer_started = True
        threading.Thread(target=_state_writer, daemon=True, name="state-writer").start()


def flush_data():
    _dirty.clear()
    return _flush_state()


def load_data():
    if _state is None:
        init_storage()
    return _state


def save_data(data=None):
    if data is not None and _state is not None and data is not _state:
        with _state_lock:
            _state.clear()
            _state.update(data)
    _dirty.set()


# ---------------- permissions, ownership, limits ----------------

def is_admin(uid):
    return uid in admin_ids()


def is_banned(uid):
    return str(uid) in {str(x) for x in load_data().get("banned", [])}


def is_public():
    return bool(load_data()["settings"].get("public", PUBLIC_MODE))


def can_host(uid):
    if is_admin(uid):
        return True
    if is_banned(uid):
        return False
    return is_public() or uid in hosting_user_ids()


def project_owner(proj):
    try:
        return int(proj.get("owner") or ADMIN_ID)
    except (TypeError, ValueError):
        return ADMIN_ID


def can_access(uid, proj):
    return is_admin(uid) or project_owner(proj) == uid


def visible_projects(data, uid):
    return {pid: p for pid, p in data["projects"].items() if can_access(uid, p)}


def project_limit(uid, data=None):
    if is_admin(uid):
        return None
    data = data if data is not None else load_data()
    try:
        return int(data["limits"].get(str(uid), MAX_PROJECTS_PER_USER))
    except (TypeError, ValueError):
        return MAX_PROJECTS_PER_USER


def check_project_limit(uid):
    data = load_data()
    limit = project_limit(uid, data)
    if limit is None:
        return True, ""
    owned = sum(1 for p in data["projects"].values() if project_owner(p) == uid)
    if owned >= limit:
        return False, f"❌ Project limit reached ({owned}/{limit}). Delete one first or ask the admin to raise your limit."
    return True, ""


def track_user(user):
    data = load_data()
    key = str(user.id)
    name = " ".join(filter(None, [user.first_name, user.last_name])) or (user.username or key)
    username = user.username or ""
    info = data["users"].get(key)
    if not info:
        data["users"][key] = {"name": name, "username": username, "first_seen": time.time()}
        save_data(data)
    elif info.get("name") != name or info.get("username") != username:
        info.update(name=name, username=username)
        save_data(data)


def plain_text(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or ""))


# ---------------- process helpers ----------------

def is_alive(pid):
    """Return True only when pid belongs to a live, non-zombie process."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, TypeError):
        return False

    # kill(pid, 0) also succeeds for zombie processes. Those have already
    # exited and must not be shown as running in the Telegram UI.
    stat_path = f"/proc/{int(pid)}/stat"
    try:
        stat = open(stat_path, "r").read()
        state = stat.rsplit(") ", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
        return False


def read_log_tail(proj_id, max_chars=3500):
    path = log_path(proj_id)
    if not os.path.exists(path):
        return "(no log file yet)"
    try:
        with open(path, "r", errors="ignore") as f:
            text = "".join(f.readlines()[-80:]).strip()
        if not text:
            return "(empty)"
        return text[-max_chars:]
    except OSError as exc:
        return f"Unable to read log: {exc}"


def project_path(pid):
    return os.path.join(STORAGE_PATH, pid)


def log_path(pid):
    return os.path.join(project_path(pid), "run.log")


def normalize_github_url(value):
    value = value.strip()
    if value.startswith("git@") or not re.match(r"^https?://github\.com/[^/\s]+/[^/\s]+(?:\.git)?/?$", value):
        return None
    return value.rstrip("/") if value.endswith(".git") else value.rstrip("/") + ".git"


def save_github_token(uid, token):
    data = load_data()
    data["github_tokens"][str(uid)] = token.strip()
    save_data(data)


def load_github_token(uid):
    return load_data()["github_tokens"].get(str(uid), "")


def delete_github_token(uid):
    data = load_data()
    data["github_tokens"].pop(str(uid), None)
    save_data(data)


def github_api(path, token, method="GET", body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "HostingBot/1.0",
    }
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request("https://api.github.com" + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except Exception:
            detail = {"message": str(exc)}
        return exc.code, detail
    except Exception as exc:
        return 0, {"message": str(exc)}


def github_profile(token):
    status, payload = github_api("/user", token)
    return (payload if status == 200 else None), payload.get("message", "GitHub authentication failed")


def github_repositories(token):
    status, payload = github_api("/user/repos?per_page=100&sort=updated", token)
    return (payload if status == 200 and isinstance(payload, list) else None), payload.get("message", "Unable to list repositories") if isinstance(payload, dict) else "Unable to list repositories"


GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,99}$")


@contextlib.contextmanager
def git_auth(token):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass = None
    try:
        if token:
            fd, askpass = tempfile.mkstemp(prefix=".askpass_", suffix=".sh", dir=BASE_DIR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' \"$GITHUB_CLONE_TOKEN\"\n")
            os.chmod(askpass, 0o700)
            env["GIT_ASKPASS"] = askpass
            env["GITHUB_CLONE_TOKEN"] = token
        yield env
    finally:
        if askpass:
            try:
                os.remove(askpass)
            except OSError:
                pass


def _authed_clone_url(normalized, token):
    if token:
        return normalized.replace("https://github.com/", "https://x-access-token@github.com/")
    return normalized


def git_remote_sha(url, branch=None, token=None):
    """Latest commit sha of a branch (or HEAD) without cloning. None if unknown."""
    normalized = normalize_github_url(url)
    if not normalized:
        return None
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    try:
        with git_auth(token) as env:
            result = subprocess.run(
                ["git", "ls-remote", _authed_clone_url(normalized, token), ref],
                capture_output=True, text=True, timeout=60, env=env,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def clone_github_repo(url, destination, token=None, branch=None):
    """Shallow-clone a repo. Returns (True, commit_sha) or (False, error_text)."""
    normalized = normalize_github_url(url)
    if not normalized:
        return False, "Please send a GitHub repository URL like https://github.com/user/repository"
    if branch and not GIT_BRANCH_RE.match(branch):
        return False, "Invalid branch name."
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [_authed_clone_url(normalized, token), destination]
    try:
        with git_auth(token) as env:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return False, "GitHub clone timed out after 10 minutes."
    except OSError as exc:
        return False, f"Git is not available in the hosting runtime: {exc}"
    if result.returncode != 0:
        return False, f"GitHub clone failed:\n{(result.stderr or result.stdout)[-2000:]}"
    sha = ""
    try:
        sha = subprocess.run(["git", "-C", destination, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.rmtree(os.path.join(destination, ".git"), ignore_errors=True)
    return True, sha


def detect_project_bot_username(folder):
    token_pattern = re.compile(r"\b(\d{8,12}:[A-Za-z0-9_-]{35})\b")
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv"}]
        for filename in files:
            if not filename.endswith((".py", ".js", ".mjs", ".cjs", ".json", ".env", ".txt")):
                continue
            try:
                text = open(os.path.join(root, filename), "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            match = token_pattern.search(text)
            if not match:
                continue
            try:
                with urllib.request.urlopen(f"https://api.telegram.org/bot{match.group(1)}/getMe", timeout=8) as response:
                    payload = json.loads(response.read().decode())
                user = payload.get("result", {})
                if user.get("username"):
                    return "@" + user["username"]
            except Exception:
                return None
    return None


def manifest_commands(folder, filename):
    path = os.path.join(folder, filename)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    raw = manifest.get("processes", manifest.get("commands", []))
    if isinstance(raw, dict):
        raw = list(raw.values())
    commands = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            commands.append(item.strip())
        elif isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"].strip():
            commands.append(item["command"].strip())
    return commands


def detect_entry(folder):
    for manifest_name in ("run.json", "project.json"):
        if manifest_commands(folder, manifest_name):
            return manifest_name
    package_json = os.path.join(folder, "package.json")
    if os.path.isfile(package_json):
        try:
            with open(package_json, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            if manifest.get("scripts", {}).get("start") or manifest.get("main"):
                main = manifest.get("main")
                if main and os.path.isfile(os.path.join(folder, main)):
                    return main
                return "package.json"
        except (OSError, json.JSONDecodeError):
            pass
    for name in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(folder, name)):
            return name
    source_files = [
        f for f in os.listdir(folder)
        if f.endswith((".py", ".js", ".mjs", ".cjs")) and os.path.isfile(os.path.join(folder, f))
    ]
    if len(source_files) == 1:
        return source_files[0]
    return None


# ================= AUTO RUNTIME SETUP (Node.js, pip) =================
RUNTIME_DIR = os.path.join(BASE_DIR, ".runtime")
LOCAL_NODE_DIR = os.path.join(RUNTIME_DIR, "node")
NODE_FALLBACK_VERSION = "v22.12.0"  # used only if nodejs.org index can't be read
_node_lock = threading.Lock()


def _is_termux():
    return "com.termux" in os.environ.get("PREFIX", "") or os.path.isdir("/data/data/com.termux")


def _activate_local_node():
    """Put the bot-managed Node.js (if present) first on PATH so every child process sees it."""
    bindir = os.path.join(LOCAL_NODE_DIR, "bin")
    if os.path.isfile(os.path.join(bindir, "node")):
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if bindir not in parts:
            os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def _run_install(cmd, timeout=900):
    try:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-600:]
    except Exception as exc:
        return False, str(exc)


def _system_install(packages):
    """Best-effort install through the OS package manager (Termux pkg / apt / apk / dnf)."""
    if _is_termux() and shutil.which("pkg"):
        pk = [p for p in packages if p != "npm"]  # Termux's nodejs already ships npm
        return _run_install(["pkg", "install", "-y", *pk])
    prefix = []
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if shutil.which("sudo"):
            prefix = ["sudo", "-n"]
        else:
            return False, "no root/sudo available for system package install"
    if shutil.which("apt-get"):
        _run_install(prefix + ["apt-get", "update", "-y"], 300)
        return _run_install(prefix + ["apt-get", "install", "-y", *packages])
    if shutil.which("apk"):
        return _run_install(prefix + ["apk", "add", *packages])
    if shutil.which("dnf"):
        return _run_install(prefix + ["dnf", "install", "-y", *packages])
    return False, "no supported package manager found"


def _install_node_tarball():
    """Download the official Node.js LTS build into .runtime/node (no root needed)."""
    if sys.platform != "linux":
        return False, "portable Node.js download is only supported on Linux"
    if os.path.exists("/etc/alpine-release"):
        return False, "official Node.js builds need glibc (Alpine uses musl)"
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "armv7l"}.get(platform.machine().lower())
    if not arch:
        return False, f"unsupported CPU: {platform.machine()}"
    version = NODE_FALLBACK_VERSION
    try:
        with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as resp:
            for rel in json.load(resp):
                if rel.get("lts"):
                    version = rel["version"]
                    break
    except Exception:
        pass
    name = f"node-{version}-linux-{arch}"
    url = f"https://nodejs.org/dist/{version}/{name}.tar.xz"
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    archive = os.path.join(RUNTIME_DIR, name + ".tar.xz")
    extract_dir = os.path.join(RUNTIME_DIR, "_extract")
    try:
        urllib.request.urlretrieve(url, archive)
        shutil.rmtree(extract_dir, ignore_errors=True)
        with tarfile.open(archive, "r:xz") as t:
            try:
                t.extractall(extract_dir, filter="data")
            except TypeError:  # Python < 3.12 has no extraction filters
                t.extractall(extract_dir)
        shutil.rmtree(LOCAL_NODE_DIR, ignore_errors=True)
        shutil.move(os.path.join(extract_dir, name), LOCAL_NODE_DIR)
        return True, version
    except Exception as exc:
        return False, f"download failed: {exc}"
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        try:
            os.remove(archive)
        except OSError:
            pass


def ensure_node():
    """Make sure `node` and `npm` exist, installing them automatically if they don't."""
    with _node_lock:
        _activate_local_node()
        if shutil.which("node") and shutil.which("npm"):
            return True, "Node.js ready."
        errors = []
        if _is_termux():
            ok, msg = _system_install(["nodejs"])
            if not ok:
                errors.append(f"pkg: {msg}")
        else:
            ok, msg = _install_node_tarball()
            if not ok:
                errors.append(f"download: {msg}")
                ok, msg = _system_install(["nodejs", "npm"])
                if not ok:
                    errors.append(f"system: {msg}")
        _activate_local_node()
        if shutil.which("node") and shutil.which("npm"):
            return True, "Node.js was missing and has been installed automatically."
        return False, "Node.js auto-install failed. " + " | ".join(errors)[-700:]


def ensure_pip():
    def _has_pip():
        try:
            return subprocess.run([sys.executable, "-m", "pip", "--version"],
                                  capture_output=True, timeout=60).returncode == 0
        except Exception:
            return False
    if _has_pip():
        return True, "pip ready."
    try:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True, timeout=300)
    except Exception:
        pass
    if _has_pip():
        return True, "pip bootstrapped."
    _system_install(["python-pip"] if _is_termux() else ["python3-pip"])
    if _has_pip():
        return True, "pip installed."
    return False, "pip is missing and could not be installed automatically."


_activate_local_node()  # reuse a Node.js downloaded on a previous run


def runtime_command(folder, entry):
    if not entry:
        return None, "No entry file set."
    if entry in {"run.json", "project.json"}:
        commands = manifest_commands(folder, entry)
        if not commands:
            return None, f"{entry} has no valid commands/processes list."
        if any(re.search(r"\b(node|npm|npx)\b", c) for c in commands):
            node_ok, node_msg = ensure_node()
            if not node_ok:
                return None, node_msg
        script = "trap 'kill 0' TERM INT; " + " & ".join(f"({command})" for command in commands) + " & wait"
        return ["bash", "-lc", script], f"Mixed process manifest ({len(commands)} processes)"
    if not entry:
        return None, "No entry file set."
    if entry == "package.json":
        node_ok, node_msg = ensure_node()
        if not node_ok:
            return None, node_msg
        return ["npm", "start"], "Node.js package.json start script"
    extension = os.path.splitext(entry)[1].lower()
    if extension == ".py":
        return [PYTHON_BIN, entry], "Python"
    if extension in {".js", ".mjs", ".cjs"}:
        node_ok, node_msg = ensure_node()
        if not node_ok:
            return None, node_msg
        return ["node", entry], "Node.js"
    return None, f"Unsupported entry type: {entry}"


IMPORT_TO_PACKAGE = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "discord": "discord.py",
    "cv": "opencv-python",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "fitz": "PyMuPDF",
    "multipart": "python-multipart",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyOpenSSL",
    "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl",
    "pyrogram": "pyrogram",
    "tgcrypto": "TgCrypto",
    "serial": "pyserial",
    "attr": "attrs",
    "magic": "python-magic",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "skimage": "scikit-image",
    "git": "GitPython",
    "socks": "PySocks",
    "nacl": "PyNaCl",
    "Levenshtein": "python-Levenshtein",
    "telethon": "Telethon",
}

# Imports that need more than one pip package to work properly.
IMPORT_EXTRAS = {
    "yt_dlp": ["yt-dlp[default]", "yt-dlp-ejs"],
}
# Imports whose libraries want a Node.js runtime on the system (e.g. YouTube JS challenges).
NODE_HELPER_IMPORTS = {"yt_dlp", "youtube_dl"}

# Standard-library modules that must never be sent to pip.
STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ())) | {
    "__future__", "typing_extensions", "_thread", "winreg", "msvcrt"
}


def discover_imports(folder):
    """Find top-level imports in .py files without running project code."""
    imported = set()
    local_modules = set()
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "env"}]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, folder)
            module_name = os.path.splitext(rel.replace(os.sep, "."))[0]
            local_modules.add(module_name.split(".")[0])
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    tree = ast.parse(f.read(), filename=path)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
    imported -= local_modules
    imported -= STDLIB_MODULES
    return sorted(imported)


JS_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "crypto", "dgram", "dns",
    "events", "fs", "http", "https", "module", "net", "os", "path", "perf_hooks",
    "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls",
    "url", "util", "v8", "vm", "worker_threads", "zlib"
}


def discover_js_imports(folder):
    """Find common npm imports without executing JavaScript code."""
    imported = set()
    patterns = [
        re.compile(r"(?:require\s*\(\s*|from\s*['\"]|import\s*['\"])([^'\"/]+(?:/[^'\" ]+)*)"),
    ]
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "dist", "build"}]
        for filename in files:
            if not filename.endswith((".js", ".mjs", ".cjs")):
                continue
            path = os.path.join(root, filename)
            try:
                text = open(path, "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for pattern in patterns:
                for name in pattern.findall(text):
                    top = name.split("/")[0]
                    if not top.startswith((".", "node:")) and top not in JS_BUILTINS:
                        imported.add(name if name.startswith("@") else top)
    return sorted(imported)


def package_names_for_imports(imports):
    names = set()
    for name in imports:
        if name in IMPORT_EXTRAS:
            names.update(IMPORT_EXTRAS[name])
        else:
            names.add(IMPORT_TO_PACKAGE.get(name, name))
    return sorted(names)


def missing_imports(imports):
    missing = []
    for name in imports:
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(name)
    return sorted(missing)


def requirement_lines(folder):
    lines = []
    req = os.path.join(folder, "requirements.txt")
    if os.path.isfile(req):
        try:
            with open(req, "r", encoding="utf-8", errors="ignore") as f:
                lines.extend(line.strip() for line in f if line.strip() and not line.lstrip().startswith(("#", "-")))
        except OSError:
            pass

    # Optional JSON manifests for projects that do not use requirements.txt.
    for manifest_name in ("requirements.json", "dependencies.json"):
        manifest_path = os.path.join(folder, manifest_name)
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            values = manifest.get("python", manifest.get("pip", manifest.get("pythonPackages", [])))
            if isinstance(values, dict):
                values = [f"{name}{spec if isinstance(spec, str) else ''}" for name, spec in values.items()]
            if isinstance(values, list):
                lines.extend(str(value).strip() for value in values if str(value).strip())
        except (OSError, json.JSONDecodeError):
            pass
    return lines


def json_node_dependencies(folder):
    values = []
    for manifest_name in ("requirements.json", "dependencies.json"):
        manifest_path = os.path.join(folder, manifest_name)
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            node_values = manifest.get("node", manifest.get("npm", manifest.get("nodePackages", [])))
            if isinstance(node_values, dict):
                values.extend(f"{name}@{spec}" if isinstance(spec, str) and spec else name for name, spec in node_values.items())
            elif isinstance(node_values, list):
                values.extend(str(value).strip() for value in node_values if str(value).strip())
        except (OSError, json.JSONDecodeError):
            pass
    return values


def dependency_fingerprint(folder, imports, packages):
    digest = hashlib.sha256()
    req = os.path.join(folder, "requirements.txt")
    if os.path.isfile(req):
        with open(req, "rb") as f:
            digest.update(f.read())
    digest.update("\n".join(imports).encode())
    digest.update("\n".join(packages).encode())
    return digest.hexdigest()


def _pip_install(args, cwd, timeout=600):
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--break-system-packages", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0, (r.stderr or r.stdout or "unknown pip error").strip()


def install_dependencies(folder, imports, packages):
    """Install declared requirements plus detected packages.

    Tries one batch pip call first. If that fails, installs package by package so one
    bad/unknown name can't block everything else.
    """
    declared = requirement_lines(folder)
    combined = list(declared)
    installed = {line.split("[", 1)[0].split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].strip().lower() for line in declared}
    for package in packages:
        normalized = package.split("[", 1)[0].lower().replace("_", "-")
        if normalized not in installed:
            combined.append(package)
            installed.add(normalized)
    if not combined:
        return True, "No third-party modules detected."

    pip_ok, pip_msg = ensure_pip()
    if not pip_ok:
        return False, pip_msg

    req_file = os.path.join(folder, ".auto_requirements.txt")
    try:
        with open(req_file, "w", encoding="utf-8") as f:
            f.write("\n".join(combined) + "\n")
        ok, output = _pip_install(["-r", req_file], folder)
    except subprocess.TimeoutExpired:
        return False, "Automatic module installation timed out after 10 minutes."
    except Exception as exc:
        return False, f"Automatic module installation failed: {exc}"
    finally:
        try:
            os.remove(req_file)
        except OSError:
            pass
    if ok:
        return True, f"Installed or verified {len(combined)} package(s). Detected imports: {', '.join(imports) or 'none'}."

    # Batch failed -> retry one by one so we know exactly which package is the problem.
    declared_set = set(declared)
    hard_failures, soft_failures = [], []
    for spec in combined:
        try:
            one_ok, one_out = _pip_install([spec], folder, timeout=600)
        except subprocess.TimeoutExpired:
            one_ok, one_out = False, "timed out"
        except Exception as exc:
            one_ok, one_out = False, str(exc)
        if not one_ok:
            (hard_failures if spec in declared_set else soft_failures).append((spec, one_out[-300:]))
    if hard_failures:
        detail = "\n".join(f"- {spec}: {out}" for spec, out in hard_failures)
        return False, f"Automatic module installation failed:\n{detail}"
    note = ""
    if soft_failures:
        note = " Skipped (not on PyPI / local module?): " + ", ".join(spec for spec, _ in soft_failures) + "."
    return True, f"Installed {len(combined) - len(soft_failures)} package(s).{note}"


def install_node_dependencies(folder, js_imports):
    if not os.path.isfile(os.path.join(folder, "package.json")) and not js_imports and not json_node_dependencies(folder):
        return True, "No Node.js dependencies detected."
    node_ok, node_msg = ensure_node()
    if not node_ok:
        return False, node_msg

    try:
        manifest_result = None
        if os.path.isfile(os.path.join(folder, "package.json")):
            manifest_result = subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund"],
                cwd=folder, capture_output=True, text=True, timeout=900,
            )
        detected = [name for name in js_imports if name not in JS_BUILTINS]
        detected.extend(json_node_dependencies(folder))
        if detected:
            extra = subprocess.run(
                ["npm", "install", "--no-save", "--no-audit", "--no-fund", *sorted(set(detected))],
                cwd=folder, capture_output=True, text=True, timeout=900,
            )
            if extra.returncode != 0:
                output = (extra.stderr or extra.stdout or "unknown npm error").strip()
                return False, f"Node.js dependency installation failed:\n{output[-2500:]}"
        if manifest_result and manifest_result.returncode != 0:
            output = (manifest_result.stderr or manifest_result.stdout or "unknown npm error").strip()
            return False, f"Node.js package installation failed:\n{output[-2500:]}"
        return True, f"Node.js dependencies ready; detected imports: {', '.join(js_imports) or 'none'}."
    except subprocess.TimeoutExpired:
        return False, "Node.js dependency installation timed out after 15 minutes."
    except Exception as exc:
        return False, f"Node.js dependency installation failed: {exc}"


def ensure_dependencies(proj_id, data):
    proj = data["projects"][proj_id]
    folder = project_path(proj_id)
    imports = discover_imports(folder)
    missing = missing_imports(imports)
    packages = package_names_for_imports(missing)
    js_imports = discover_js_imports(folder)
    if NODE_HELPER_IMPORTS & set(imports):
        ensure_node()  # best effort; yt-dlp & friends use it to solve JS challenges
    fingerprint = dependency_fingerprint(folder, imports + js_imports, packages + json_node_dependencies(folder))
    proj["detected_imports"] = imports
    proj["missing_imports"] = missing
    proj["detected_packages"] = packages
    proj["detected_js_imports"] = js_imports
    if proj.get("dependency_fingerprint") == fingerprint and proj.get("requirements_status") == "installed":
        save_data(data)
        return True, "Dependencies already installed."

    proj["requirements_status"] = "installing"
    save_data(data)
    ok, message = install_dependencies(folder, imports, packages)
    if ok:
        ok, node_message = install_node_dependencies(folder, js_imports)
        message = f"{message} {node_message}"
    proj["requirements_status"] = "installed" if ok else "failed"
    proj["requirements_error"] = None if ok else message
    if ok:
        proj["dependency_fingerprint"] = fingerprint
    save_data(data)
    return ok, message


def railway_environment():
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"))


def railway_public_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not domain:
        return None
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain.rstrip("/")


def find_available_port():
    # Railway injects PORT and routes the service domain to that one port.
    # All child API processes in this service must use it.
    if railway_environment():
        try:
            return int(os.environ.get("PORT", DEFAULT_API_PORT))
        except ValueError:
            return DEFAULT_API_PORT
    for port in API_PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return DEFAULT_API_PORT


def detect_listening_port(preferred_port):
    candidates = [preferred_port] + [p for p in API_PORT_RANGE if p != preferred_port]
    for port in candidates:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return port
        except OSError:
            continue
    return None


def wait_for_api_port(pid, preferred_port):
    deadline = time.time() + PORT_DETECT_SECONDS
    while time.time() < deadline:
        if not is_alive(pid):
            return None
        port = detect_listening_port(preferred_port)
        if port:
            return port
        time.sleep(0.25)
    return None


def start_cloudflared_tunnel(port):
    if not ENABLE_CLOUDFLARED_TUNNEL or PUBLIC_URL_TEMPLATE or not shutil.which("cloudflared"):
        return None, None
    try:
        tunnel = subprocess.Popen(
            [shutil.which("cloudflared"), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        deadline = time.time() + 15
        pattern = re.compile(r"https://[a-z0-9-]+\\.trycloudflare\\.com")
        while time.time() < deadline and tunnel.poll() is None:
            line = tunnel.stdout.readline()
            match = pattern.search(line or "")
            if match:
                return match.group(0), tunnel.pid
        os.killpg(tunnel.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    return None, None


def stop_cloudflared_tunnel(proj):
    tunnel_pid = proj.get("tunnel_pid")
    if tunnel_pid and is_alive(tunnel_pid):
        try:
            os.killpg(tunnel_pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    proj["tunnel_pid"] = None


def project_url_text(proj):
    public_url = proj.get("public_url")
    local_url = proj.get("local_url")
    if public_url:
        return f"🌐 <b>Public URL:</b> <a href=\"{html.escape(public_url, quote=True)}\">{html.escape(public_url)}</a>"
    if local_url:
        return f"🌐 <b>Local URL:</b> <code>{html.escape(local_url)}</code>\n<i>Set PUBLIC_URL_TEMPLATE or a tunnel to make it public.</i>"
    return ""


def start_project(proj_id, data):
    proj = data["projects"][proj_id]
    folder = project_path(proj_id)
    entry = proj.get("entry")
    entry_path = os.path.join(folder, entry) if entry else ""
    if not entry:
        return False, "No entry file set. Use the ⚙️ Set Entry button first."
    if not os.path.isfile(entry_path):
        return False, f"Entry file not found: {entry}"
    if is_alive(proj.get("pid")):
        return False, "Already running."

    ok, install_error = ensure_dependencies(proj_id, data)
    if not ok:
        return False, install_error

    command, runtime = runtime_command(folder, entry)
    if not command:
        return False, runtime
    logf = open(log_path(proj_id), "w")
    env = child_environment(proj)
    try:
        proc = subprocess.Popen(
            command,
            cwd=folder,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            preexec_fn=os.setsid,  # new process group so we can kill children too
        )
    finally:
        logf.close()

    # Give imports and argument validation a moment to fail before recording
    # the PID as running. The complete traceback is available through Logs.
    time.sleep(0.75)
    if proc.poll() is not None or not is_alive(proc.pid):
        error = read_log_tail(proj_id)
        proj["pid"] = None
        proj["pgid"] = None
        save_data(data)
        return False, f"Project exited during startup.\n<pre>{html.escape(error)}</pre>"

    proj["pid"] = proc.pid
    proj["pgid"] = proc.pid  # since setsid, pgid == pid
    proj["desired_running"] = True
    proj["started_at"] = time.time()
    proj["runtime"] = runtime
    proj["command"] = command
    proj["port"] = None
    proj["local_url"] = None
    proj["public_url"] = None
    proj["tunnel_pid"] = None
    save_data(data)
    return True, f"Started (PID {proc.pid})."


def stop_project(proj_id, data):
    proj = data["projects"][proj_id]
    pid = proj.get("pid")
    proj["desired_running"] = False
    if not is_alive(pid):
        proj["pid"] = None
        save_data(data)
        return False, "Not running."
    try:
        os.killpg(proj.get("pgid", pid), signal.SIGTERM)
        time.sleep(1)
        if is_alive(pid):
            os.killpg(proj.get("pgid", pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    proj["pid"] = None
    save_data(data)
    return True, "Stopped."


# ---------------- project file backups (Turso) + GitHub updates ----------------
SECRET_ENV_KEYS = {"BOT_TOKEN"}
BACKUP_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv", ".cache"}
BACKUP_SKIP_FILES = {"run.log"}
_backup_lock = threading.Lock()
_backup_sigs = {}


def child_environment(proj):
    """Env for hosted projects: the bot's own secrets are removed, per-project vars added."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_KEYS and not k.startswith("TURSO_")}
    for key, value in (proj.get("env") or {}).items():
        env[str(key)] = str(value)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _backup_files(pid):
    base = project_path(pid)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in BACKUP_SKIP_DIRS]
        for filename in files:
            if filename in BACKUP_SKIP_FILES:
                continue
            full = os.path.join(root, filename)
            if os.path.islink(full):
                continue
            yield full, os.path.relpath(full, base)


def folder_signature(pid):
    h = hashlib.sha1()
    for full, rel in sorted(_backup_files(pid), key=lambda item: item[1]):
        try:
            st = os.stat(full)
        except OSError:
            continue
        h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def build_archive(pid):
    limit = MAX_BACKUP_MB * 1024 * 1024
    files = list(_backup_files(pid))
    total = 0
    for full, _ in files:
        try:
            total += os.path.getsize(full)
        except OSError:
            pass
    if total > limit * 4:
        raise ValueError(f"project is too large to back up (over {MAX_BACKUP_MB * 4} MB of files)")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for full, rel in files:
            try:
                z.write(full, rel)
            except OSError:
                continue
    blob = buf.getvalue()
    if len(blob) > limit:
        raise ValueError(f"project is larger than {MAX_BACKUP_MB} MB after compression")
    return blob


def backup_project(pid, force=False):
    if not store:
        return False, "Turso is not configured."
    if not os.path.isdir(project_path(pid)):
        return False, "Project folder is missing."
    with _backup_lock:
        try:
            sig = folder_signature(pid)
            if not force and _backup_sigs.get(pid) == sig:
                return True, "Already backed up."
            blob = build_archive(pid)
            store.save_archive(pid, blob)
        except Exception as exc:
            print(f"Backup of {pid} failed: {exc}")
            return False, str(exc)
        _backup_sigs[pid] = sig
        return True, f"Saved {len(blob) / 1024:.0f} KB to Turso."


def backup_async(pid, force=True):
    threading.Thread(target=backup_project, args=(pid, force), daemon=True).start()


def backup_all(force=False, deadline=None):
    for pid in list(load_data()["projects"]):
        if deadline and time.time() > deadline:
            break
        backup_project(pid, force)


def backup_loop():
    while True:
        time.sleep(BACKUP_MINUTES * 60)
        try:
            backup_all()
        except Exception as exc:
            print(f"Backup loop error: {exc}")


def restore_projects():
    """After a redeploy the disk is empty: bring every project's files back from Turso."""
    if not store:
        return
    data = load_data()
    try:
        remote = store.list_archives()
    except Exception as exc:
        print(f"Could not list Turso backups: {exc}")
        return
    changed = False
    for pid, proj in list(data["projects"].items()):
        folder = project_path(pid)
        if os.path.isdir(folder) and os.listdir(folder):
            continue
        if pid not in remote:
            print(f"No backup found for project {pid} ({proj.get('name')})")
            continue
        try:
            blob = store.load_archive(pid)
            os.makedirs(folder, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                z.extractall(folder)
        except Exception as exc:
            print(f"Restore of {pid} failed: {exc}")
            shutil.rmtree(folder, ignore_errors=True)
            continue
        # processes and installed packages did not survive the redeploy
        proj.update(pid=None, pgid=None, port=None, local_url=None, public_url=None, tunnel_pid=None,
                    requirements_status=None, dependency_fingerprint=None)
        _backup_sigs[pid] = folder_signature(pid)
        changed = True
        print(f"Restored project {pid} ({proj.get('name')})")
    if changed:
        save_data(data)


def normalize_pids():
    """Forget PIDs that no longer belong to the project's own process."""
    data = load_data()
    changed = False
    for pid, proj in data["projects"].items():
        old = proj.get("pid")
        if not old:
            continue
        keep = False
        if is_alive(old):
            try:
                keep = os.path.realpath(os.readlink(f"/proc/{int(old)}/cwd")) == os.path.realpath(project_path(pid))
            except OSError:
                keep = False
        if not keep:
            proj["pid"] = None
            proj["pgid"] = None
            changed = True
    if changed:
        save_data(data)


def autostart_projects():
    """Start every project that was running before the bot restarted / redeployed."""
    data = load_data()
    for pid, proj in list(data["projects"].items()):
        if not proj.get("desired_running") or is_alive(proj.get("pid")) or not proj.get("entry"):
            continue
        try:
            ok, msg = start_project(pid, data)
        except Exception as exc:
            ok, msg = False, str(exc)
        print(f"Auto-restart {pid} ({proj.get('name')}): {'ok' if ok else msg[:200]}")
        if not ok:
            proj["desired_running"] = False
            save_data(data)
            try:
                bot.send_message(project_owner(proj), f"⚠️ <b>{html.escape(proj.get('name', pid))}</b> could not auto-restart:\n<pre>{html.escape(plain_text(msg)[-1500:])}</pre>")
            except Exception:
                pass


def update_from_github(pid, force=False):
    """Pull the latest commit of a GitHub-imported project. Returns (ok, message, changed)."""
    data = load_data()
    proj = data["projects"].get(pid)
    gh = (proj or {}).get("github")
    if not gh:
        return False, "This project was not imported from GitHub.", False
    token = load_github_token(project_owner(proj))
    remote = git_remote_sha(gh["url"], gh.get("branch"), token)
    if remote and remote == gh.get("commit") and not force:
        return True, "Already up to date.", False
    tmp = project_path(pid) + ".update"
    shutil.rmtree(tmp, ignore_errors=True)
    ok, result = clone_github_repo(gh["url"], tmp, token, gh.get("branch"))
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, result, False
    if not force and result and result == gh.get("commit"):
        shutil.rmtree(tmp, ignore_errors=True)
        return True, "Already up to date.", False
    was_running = is_alive(proj.get("pid"))
    if was_running:
        stop_project(pid, data)
    try:
        shutil.copytree(tmp, project_path(pid), dirs_exist_ok=True)
    except OSError as exc:
        return False, f"Could not apply update: {exc}", False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    gh["commit"] = result or remote or gh.get("commit")
    gh["updated_at"] = time.time()
    entry = proj.get("entry")
    if not entry or not os.path.isfile(os.path.join(project_path(pid), entry)):
        proj["entry"] = detect_entry(project_path(pid))
    save_data(data)
    backup_project(pid, force=True)
    message = f"Updated to {(gh.get('commit') or '')[:7] or 'latest'}."
    if was_running:
        started, start_msg = start_project(pid, data)
        message += " Restarted." if started else f" Restart failed: {plain_text(start_msg)[-300:]}"
    return True, message, True


def github_auto_update_loop():
    while True:
        time.sleep(AUTO_UPDATE_MINUTES * 60)
        for pid, proj in list(load_data()["projects"].items()):
            gh = proj.get("github")
            if not (gh and gh.get("auto")):
                continue
            try:
                ok, msg, changed = update_from_github(pid)
            except Exception as exc:
                print(f"Auto-update {pid} failed: {exc}")
                continue
            if changed:
                try:
                    bot.send_message(project_owner(proj), f"🔁 <b>Auto-update</b> — {html.escape(proj.get('name', pid))}\n{html.escape(msg)}")
                except Exception:
                    pass


def _graceful_exit(signum, frame):
    print("Shutting down: saving state and project backups...")
    try:
        flush_data()
        backup_all(deadline=time.time() + 20)
    except Exception as exc:
        print(f"Shutdown save error: {exc}")
    os._exit(0)


# ---------------- keyboards ----------------

def styled_button(text, callback_data=None, url=None, style=None):
    """Use Bot API 10.2 button colors when the installed client supports them."""
    if style is None:
        if any(word in text for word in ("Delete", "Stop", "Remove", "Deny")):
            style = "danger"
        elif any(word in text for word in ("Run", "Restart", "GitHub", "Add", "Approve", "URL")):
            style = "success"
        else:
            style = "primary"
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    try:
        return types.InlineKeyboardButton(style=style, **kwargs)
    except TypeError:
        return types.InlineKeyboardButton(**kwargs)


def github_manage_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(styled_button("👤 Profile", callback_data="ghprofile"), styled_button("📚 Repositories", callback_data="ghlist:0"))
    kb.add(styled_button("🔌 Disconnect", callback_data="ghdisconnect"), styled_button("⬅️ Back", callback_data="home"))
    return kb


def main_menu_kb(uid=None):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        styled_button("📁 List Projects", callback_data="list"),
        styled_button("📊 Live Status", callback_data="status"),
    )
    if uid is not None and load_github_token(uid):
        kb.add(styled_button("🔗 Manage GitHub", callback_data="ghmanage"))
    else:
        kb.add(styled_button("🔗 Connect GitHub", callback_data="ghconnect"))
    kb.add(styled_button("👥 Admin Help", callback_data="adminhelp"))
    return kb


def project_list_kb(data, uid=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    projects = visible_projects(data, uid) if uid is not None else data["projects"]
    if not projects:
        kb.add(styled_button("No projects yet — send a file/zip", callback_data="noop"))
        return kb
    for pid, proj in projects.items():
        running = is_alive(proj.get("pid"))
        dot = "🟢" if running else "🔴"
        owner_tag = f" · 👤{project_owner(proj)}" if uid is not None and project_owner(proj) != uid else ""
        kb.add(styled_button(f"{dot} {proj['name']}{owner_tag}"[:60], callback_data=f"open:{pid}"))
    kb.add(styled_button("⬅️ Back", callback_data="home"))
    return kb


def project_ctrl_kb(pid, data):
    proj = data["projects"][pid]
    running = is_alive(proj.get("pid"))
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            styled_button("⏹ Stop", callback_data=f"stop:{pid}"),
            styled_button("🔄 Restart", callback_data=f"restart:{pid}"),
        )
    else:
        kb.add(styled_button("▶️ Run", callback_data=f"run:{pid}"))
    kb.add(
        styled_button("📜 Logs", callback_data=f"logs:{pid}"),
        styled_button("⚙️ Set Entry", callback_data=f"setentry:{pid}"),
    )
    if proj.get("github"):
        kb.add(
            styled_button("⬇️ Pull Update", callback_data=f"ghpull:{pid}"),
            styled_button("🔁 Auto-update: " + ("ON" if proj["github"].get("auto") else "OFF"), callback_data=f"ghauto:{pid}"),
        )
    kb.add(
        styled_button("🔐 Env Vars", callback_data=f"env:{pid}"),
        styled_button("💾 Backup", callback_data=f"backup:{pid}"),
    )
    kb.add(
        styled_button("📦 Download", callback_data=f"dl:{pid}"),
        styled_button("🗑 Delete", callback_data=f"del:{pid}"),
    )
    kb.add(styled_button("⬅️ Back", callback_data="list"))
    return kb


def register_project_folder(chat_id, folder, name, source=None, owner=None, github=None):
    proj_id = os.path.basename(folder)
    data = load_data()
    entry = detect_entry(folder)
    project = {
        "name": name,
        "entry": entry,
        "pid": None,
        "created": time.time(),
        "source": source,
        "telegram_bot": detect_project_bot_username(folder),
        "owner": owner or ADMIN_ID,
        "github": github,
    }
    data["projects"][proj_id] = project
    save_data(data)
    backup_async(proj_id)
    dep_ok, dep_message = ensure_dependencies(proj_id, data)
    run_ok, run_message = (start_project(proj_id, load_data()) if dep_ok and entry else (False, "Not started: entry file is missing."))
    data = load_data()
    entry_note = f"Entry: <code>{html.escape(entry)}</code>" if entry else "⚠️ Entry file not detected — use ⚙️ Set Entry."
    bot_name = f"\n🤖 Hosted bot: <b>{html.escape(project['telegram_bot'])}</b>" if project.get("telegram_bot") else ""
    dep_note = "✅ Dependencies ready." if dep_ok else f"⚠️ <b>Dependency error:</b>\n<pre>{html.escape(dep_message[-1800:])}</pre>"
    run_note = f"\n🚀 <b>Auto-start:</b> {html.escape(run_message)}" if run_ok else f"\n❌ <b>Auto-start failed:</b>\n<pre>{html.escape(run_message[-1800:])}</pre>"
    bot.send_message(chat_id, f"✅ <b>Project imported and processed</b>\n<blockquote>{html.escape(name)}</blockquote>\n{entry_note}{bot_name}\n\n{dep_note}{run_note}", reply_markup=project_ctrl_kb(proj_id, data))


def github_repo_text(repo):
    visibility = "private" if repo.get("private") else "public"
    return (
        f"📦 <b>{html.escape(repo.get('full_name', repo.get('name', 'repository')))}</b>\n"
        f"<blockquote>{html.escape(repo.get('description') or 'No description')}</blockquote>\n"
        f"⭐ <b>Stars:</b> {repo.get('stargazers_count', 0)}  |  🍴 <b>Forks:</b> {repo.get('forks_count', 0)}\n"
        f"🔒 <b>Visibility:</b> {visibility}\n"
        f"🗓 <b>Updated:</b> {html.escape(repo.get('updated_at', 'unknown'))}"
    )


def github_profile_text(profile):
    return (
        "🔗 <b>GitHub Connected</b>\n"
        f"<blockquote><b>{html.escape(profile.get('name') or profile.get('login', 'Unknown'))}</b> (@{html.escape(profile.get('login', 'unknown'))})</blockquote>\n"
        f"🆔 <b>ID:</b> {profile.get('id', 'unknown')}\n"
        f"📦 <b>Public repositories:</b> {profile.get('public_repos', 0)}\n"
        f"👥 <b>Followers:</b> {profile.get('followers', 0)}\n"
        f"📍 <b>Location:</b> {html.escape(profile.get('location') or 'Not set')}\n"
        f"🔗 <a href=\"{html.escape(profile.get('html_url', ''), quote=True)}\">Open GitHub profile</a>"
    )


def github_repo_list_kb(repos):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for index, repo in enumerate(repos[:100]):
        visibility = "🔒" if repo.get("private") else "🌐"
        label = f"{visibility} {repo.get('full_name', repo.get('name', 'repository'))}"
        kb.add(styled_button(label[:60], callback_data=f"ghrepo:{index}"))
    kb.add(styled_button("🔄 Refresh repositories", callback_data="ghlist:0"))
    kb.add(styled_button("⬅️ Manage GitHub", callback_data="ghmanage"))
    return kb


def github_repo_actions_kb(full_name):
    safe_name = html.escape(full_name, quote=True)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(styled_button("▶️ Run Repo", callback_data=f"ghrun:{full_name}"), styled_button("🗑 Delete Repo", callback_data=f"ghdelete:{full_name}"))
    kb.add(styled_button("📝 Commits", callback_data=f"ghcommits:{full_name}"), styled_button("🔗 Open on GitHub", url=f"https://github.com/{safe_name}"))
    kb.add(styled_button("⬅️ Repositories", callback_data="ghlist:0"))
    return kb


def process_github_repo(m, url, user_id=None, branch=None):
    uid = user_id or m.from_user.id
    allowed, limit_msg = check_project_limit(uid)
    if not allowed:
        bot.reply_to(m, limit_msg)
        return
    status = bot.reply_to(m, "🐙 Cloning GitHub repository...")
    proj_id = uuid.uuid4().hex[:8]
    folder = project_path(proj_id)
    ok, result = clone_github_repo(url, folder, load_github_token(uid), branch)
    if not ok:
        bot.edit_message_text(f"❌ <b>GitHub import failed</b>\n<pre>{html.escape(result[-2500:])}</pre>", m.chat.id, status.message_id)
        shutil.rmtree(folder, ignore_errors=True)
        return
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    bot.edit_message_text(f"✅ Cloned <b>{html.escape(name)}</b>. Installing dependencies and detecting entry...", m.chat.id, status.message_id)
    github = {"url": normalize_github_url(url), "branch": branch, "commit": result, "auto": False}
    register_project_folder(m.chat.id, folder, name, source=url, owner=uid, github=github)


def connect_github_prompt(chat_id, user_id):
    awaiting_github_token.add(user_id)
    bot.send_message(chat_id, "🔐 <b>Connect GitHub</b>\n<blockquote>Send a GitHub Personal Access Token. It is validated and saved to the bot's database for your account only. Your message is deleted right after. Use a token with only the access you need.</blockquote>")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_github_token, content_types=["text"])
def handle_github_token(m):
    awaiting_github_token.discard(m.from_user.id)
    token = m.text.strip()
    if len(token) < 20 or " " in token:
        bot.reply_to(m, "❌ Invalid-looking token. Please try again.")
        return
    profile, error = github_profile(token)
    if not profile:
        bot.reply_to(m, f"❌ <b>GitHub connection failed:</b>\n<pre>{html.escape(error)}</pre>")
        return
    save_github_token(m.from_user.id, token)
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass
    bot.send_message(m.chat.id, "✅ <b>GitHub connected successfully.</b>", reply_markup=github_manage_kb())


@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    uid = m.from_user.id
    if not can_host(uid):
        bot.reply_to(m, "🚫 <b>You are banned from this bot.</b>" if is_banned(uid) else "🔒 <b>This bot is invite-only right now.</b> Ask the admin to grant you access.")
        return
    track_user(m.from_user)
    admin_lines = (
        "\n<b>Admin</b>\n"
        "/public on|off — open or close the bot to everyone\n"
        "/users — list users\n"
        "/ban ID, /unban ID — block or unblock a user\n"
        "/setlimit ID N — project limit for one user\n"
        "/broadcast text — message every user\n"
        "/grant ID, /revoke ID — allow list (used when public is off)\n"
    ) if is_admin(uid) else ""
    bot.send_message(
        m.chat.id,
        "🤖 <b>Hosting Bot</b>\n\n"
        "<blockquote>Upload a .py/.js file, a .zip project, or use /github with a repository URL. "
        "Your projects are private — only you can see and control them.</blockquote>\n\n"
        "<b>Commands</b>\n"
        "/list — your projects\n"
        "/status — live status\n"
        "/github URL [branch] — import a GitHub repository\n"
        "/connect_github — connect your GitHub account (private repos, repo list)\n"
        + admin_lines,
        reply_markup=main_menu_kb(uid),
    )


@bot.message_handler(commands=["connect_github"])
def cmd_connect_github(m):
    if can_host(m.from_user.id):
        connect_github_prompt(m.chat.id, m.from_user.id)


@bot.message_handler(commands=["github"])
def cmd_github(m):
    if not can_host(m.from_user.id):
        bot.reply_to(m, "🚫 <b>Not authorized.</b>")
        return
    parts = m.text.split()
    if len(parts) >= 2:
        process_github_repo(m, parts[1].strip(), branch=parts[2] if len(parts) > 2 else None)
    else:
        awaiting_github.add(m.from_user.id)
        bot.reply_to(m, "🐙 <b>GitHub import</b>\n<blockquote>Send a repository URL, for example: https://github.com/user/repository\nFor a branch: /github URL branch-name</blockquote>")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_github, content_types=["text"])
def handle_github_reply(m):
    awaiting_github.discard(m.from_user.id)
    if can_host(m.from_user.id):
        process_github_repo(m, m.text.strip())


@bot.message_handler(func=lambda m: can_host(m.from_user.id) and bool(normalize_github_url(m.text.strip())), content_types=["text"])
def handle_direct_github_link(m):
    process_github_repo(m, m.text.strip())


@bot.message_handler(commands=["grant", "revoke"])
def cmd_permission(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "🚫 Admin only.")
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Usage: <code>/grant 123456789</code> or <code>/revoke 123456789</code>")
        return
    uid = int(parts[1])
    data = load_data()
    users = set(data.get("hosting_users", []))
    if m.text.split()[0].lower() == "/grant":
        users.add(uid)
        data["hosting_users"] = sorted(users)
        save_data(data)
        bot.reply_to(m, f"✅ Hosting permission granted to <code>{uid}</code>.")
    else:
        users.discard(uid)
        data["hosting_users"] = sorted(users)
        save_data(data)
        bot.reply_to(m, f"✅ Hosting permission revoked from <code>{uid}</code>.")


@bot.message_handler(commands=["public", "ban", "unban", "users", "broadcast", "setlimit"])
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "🚫 Admin only.")
        return
    parts = m.text.split(maxsplit=2)
    cmd = parts[0].split("@")[0].lstrip("/").lower()
    data = load_data()

    if cmd == "public":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            bot.reply_to(m, f"Public mode is <b>{'ON' if is_public() else 'OFF'}</b>.\nUsage: <code>/public on</code> or <code>/public off</code>")
            return
        data["settings"]["public"] = parts[1].lower() == "on"
        save_data(data)
        bot.reply_to(m, "🌍 Public mode <b>ON</b> — everyone can host." if data["settings"]["public"] else "🔒 Public mode <b>OFF</b> — only admins and /grant users can host.")

    elif cmd in ("ban", "unban"):
        if len(parts) < 2 or not parts[1].isdigit():
            bot.reply_to(m, f"Usage: <code>/{cmd} 123456789</code>")
            return
        uid = int(parts[1])
        banned = {int(x) for x in data["banned"] if str(x).isdigit()}
        if cmd == "ban":
            if uid in admin_ids(data):
                bot.reply_to(m, "❌ You cannot ban an admin.")
                return
            banned.add(uid)
            stopped = 0
            for pid, proj in data["projects"].items():
                if project_owner(proj) == uid and is_alive(proj.get("pid")):
                    stop_project(pid, data)
                    stopped += 1
            data["banned"] = sorted(banned)
            save_data(data)
            bot.reply_to(m, f"🚫 Banned <code>{uid}</code> (stopped {stopped} running project(s)).")
        else:
            banned.discard(uid)
            data["banned"] = sorted(banned)
            save_data(data)
            bot.reply_to(m, f"✅ Unbanned <code>{uid}</code>.")

    elif cmd == "setlimit":
        args = m.text.split()
        if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
            bot.reply_to(m, "Usage: <code>/setlimit 123456789 10</code>")
            return
        data["limits"][args[1]] = int(args[2])
        save_data(data)
        bot.reply_to(m, f"✅ Project limit for <code>{args[1]}</code> set to {int(args[2])}.")

    elif cmd == "users":
        counts = {}
        for proj in data["projects"].values():
            counts[project_owner(proj)] = counts.get(project_owner(proj), 0) + 1
        lines = []
        for key, info in list(data["users"].items())[-40:]:
            handle = f" @{info['username']}" if info.get("username") else ""
            mark = " 🚫" if is_banned(int(key)) else ""
            lines.append(f"<code>{key}</code> {html.escape(info.get('name', ''))}{html.escape(handle)} — {counts.get(int(key), 0)} project(s){mark}")
        bot.reply_to(m, f"👥 <b>Users</b> ({len(data['users'])} total, last 40 shown)\n" + ("\n".join(lines) or "none yet"))

    elif cmd == "broadcast":
        body = m.text.split(maxsplit=1)
        if len(body) < 2:
            bot.reply_to(m, "Usage: <code>/broadcast your message</code>")
            return
        sent = failed = 0
        for key in list(data["users"]):
            try:
                bot.send_message(int(key), f"📢 <b>Announcement</b>\n{html.escape(body[1])}")
                sent += 1
            except Exception:
                failed += 1
            time.sleep(0.06)
        bot.reply_to(m, f"📢 Sent to {sent} user(s), {failed} failed.")


@bot.message_handler(commands=["list"])
def cmd_list(m):
    if not can_host(m.from_user.id):
        return
    data = load_data()
    bot.send_message(m.chat.id, "📁 <b>Your Projects</b>", reply_markup=project_list_kb(data, m.from_user.id))


@bot.message_handler(commands=["status"])
def cmd_status(m):
    if not can_host(m.from_user.id):
        return
    bot.send_message(m.chat.id, status_text(m.from_user.id), reply_markup=main_menu_kb(m.from_user.id))


def status_text(uid=None):
    data = load_data()
    projects = visible_projects(data, uid) if uid is not None else data["projects"]
    total = len(projects)
    running = sum(1 for p in projects.values() if is_alive(p.get("pid")))
    size = 0
    for pid in projects:
        for root, _, files in os.walk(project_path(pid)):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    size_mb = size / (1024 * 1024)
    text = (
        f"📊 <b>LIVE HOSTING STATUS</b>\n"
        f"<blockquote>🟢 <b>Running:</b> {running}\n"
        f"🔴 <b>Stopped:</b> {total - running}\n"
        f"📦 <b>Total projects:</b> {total}\n"
        f"💾 <b>Storage:</b> {size_mb:.1f} MB</blockquote>"
    )
    if uid is not None and is_admin(uid):
        text += (
            f"\n👥 <b>Users:</b> {len(data['users'])}  |  "
            f"🌍 <b>Public:</b> {'ON' if is_public() else 'OFF'}  |  "
            f"☁️ <b>Turso:</b> {'connected' if store else 'off'}"
        )
    elif uid is not None:
        limit = project_limit(uid, data)
        if limit is not None:
            text += f"\n📦 Your limit: {total}/{limit} projects"
    return text


# ---------------- file upload ----------------

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not can_host(m.from_user.id):
        return
    track_user(m.from_user)
    allowed, limit_msg = check_project_limit(m.from_user.id)
    if not allowed:
        bot.reply_to(m, limit_msg)
        return
    doc = m.document
    if doc.file_size > MAX_FILE_MB * 1024 * 1024:
        bot.reply_to(m, f"❌ File too big (max {MAX_FILE_MB}MB).")
        return

    msg = bot.reply_to(m, "⬇️ Downloading...")

    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)

    proj_id = uuid.uuid4().hex[:8]
    folder = project_path(proj_id)
    os.makedirs(folder, exist_ok=True)

    orig_name = doc.file_name or f"file_{proj_id}"
    save_path = os.path.join(folder, orig_name)
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    is_zip = orig_name.lower().endswith(".zip")
    if is_zip:
        try:
            with zipfile.ZipFile(save_path, "r") as z:
                if sum(i.file_size for i in z.infolist()) > MAX_FILE_MB * 1024 * 1024 * 10:
                    raise zipfile.BadZipFile("zip expands to too much data")
                z.extractall(folder)
            os.remove(save_path)
            # flatten if zip had a single root folder
            entries = [e for e in os.listdir(folder) if not e.startswith(".")]
            if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])):
                inner = os.path.join(folder, entries[0])
                for item in os.listdir(inner):
                    shutil.move(os.path.join(inner, item), folder)
                os.rmdir(inner)
        except zipfile.BadZipFile:
            bot.edit_message_text("❌ Invalid zip file.", m.chat.id, msg.message_id)
            shutil.rmtree(folder, ignore_errors=True)
            return

    name = orig_name.rsplit(".", 1)[0] if not is_zip else orig_name[:-4]
    entry = detect_entry(folder)

    data = load_data()
    data["projects"][proj_id] = {
        "name": name,
        "entry": entry,
        "pid": None,
        "created": time.time(),
        "telegram_bot": detect_project_bot_username(folder),
        "owner": m.from_user.id,
    }
    save_data(data)
    backup_async(proj_id)
    bot.edit_message_text("⬇️ Downloaded. Detecting Python modules and installing dependencies...", m.chat.id, msg.message_id)
    dep_ok, dep_message = ensure_dependencies(proj_id, data)
    dep_note = "✅ Dependencies ready." if dep_ok else f"⚠️ <b>Dependency installation failed:</b>\n<pre>{html.escape(dep_message[-1800:])}</pre>"

    entry_note = f"Entry file: <code>{entry}</code>" if entry else "⚠️ Couldn't auto-detect entry file — set it with ⚙️ Set Entry."
    run_ok, run_message = (start_project(proj_id, load_data()) if dep_ok and entry else (False, "Not started: entry file is missing."))
    data = load_data()
    run_note = f"\n🚀 <b>Auto-start:</b> {html.escape(run_message)}" if run_ok else f"\n❌ <b>Auto-start failed:</b>\n<pre>{html.escape(run_message[-1800:])}</pre>"
    bot.edit_message_text(
        f"✅ Uploaded and processed: <b>{html.escape(name)}</b>\n{entry_note}\n\n{dep_note}{run_note}",
        m.chat.id,
        msg.message_id,
        reply_markup=project_ctrl_kb(proj_id, data),
    )


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_entry, content_types=["text"])
def handle_entry_reply(m):
    pid = awaiting_entry.pop(m.from_user.id)
    data = load_data()
    if pid not in data["projects"]:
        return
    filename = m.text.strip()
    folder = project_path(pid)
    if not os.path.exists(os.path.join(folder, filename)):
        bot.reply_to(m, f"❌ <code>{filename}</code> not found in project folder.")
        return
    data["projects"][pid]["entry"] = filename
    save_data(data)
    backup_async(pid)
    bot.reply_to(m, f"✅ Entry file set to <code>{filename}</code>", reply_markup=project_ctrl_kb(pid, data))


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_env and not (m.text or "").startswith("/"), content_types=["text"])
def handle_env_reply(m):
    pid = awaiting_env.pop(m.from_user.id)
    data = load_data()
    proj = data["projects"].get(pid)
    if not proj or not can_access(m.from_user.id, proj):
        return
    env = proj.setdefault("env", {})
    results = []
    lowered = m.text.strip().lower()
    if lowered == "clear":
        env.clear()
        results.append("Cleared all variables.")
    elif lowered == "show":
        results.append("Variables: " + (", ".join(env) or "none"))
    else:
        for line in m.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("unset "):
                key = line[6:].strip()
                env.pop(key, None)
                results.append(f"Removed {key}")
                continue
            if "=" not in line:
                results.append("Skipped a line without '='")
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if not ENV_KEY_RE.match(key):
                results.append(f"Skipped invalid name: {key[:30]}")
            elif key not in env and len(env) >= 50:
                results.append(f"Skipped {key}: limit of 50 variables")
            else:
                env[key] = value
                results.append(f"Set {key}")
    save_data(data)
    backup_async(pid)
    try:
        bot.delete_message(m.chat.id, m.message_id)  # may contain secrets
    except Exception:
        pass
    bot.send_message(m.chat.id, "🔐 <b>Environment variables</b>\n" + html.escape("\n".join(results) or "Nothing changed.") + "\n<i>Restart the project to apply.</i>", reply_markup=project_ctrl_kb(pid, data))


# ---------------- callbacks ----------------

@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    if not can_host(c.from_user.id):
        bot.answer_callback_query(c.id, "Not authorized.")
        return

    data = load_data()
    action = c.data

    if action == "home":
        bot.edit_message_text("🤖 <b>Hosting Bot</b>", c.message.chat.id, c.message.message_id, reply_markup=main_menu_kb(c.from_user.id))

    elif action == "list":
        bot.edit_message_text("📁 <b>Your Projects</b>", c.message.chat.id, c.message.message_id, reply_markup=project_list_kb(data, c.from_user.id))

    elif action == "status":
        bot.edit_message_text(status_text(c.from_user.id), c.message.chat.id, c.message.message_id, reply_markup=main_menu_kb(c.from_user.id))

    elif action == "noop":
        bot.answer_callback_query(c.id)

    elif action == "github":
        awaiting_github.add(c.from_user.id)
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "🐙 <b>GitHub import</b>\n<blockquote>Send a public GitHub repository URL.</blockquote>")

    elif action == "adminhelp":
        bot.answer_callback_query(c.id)
        if is_admin(c.from_user.id):
            bot.send_message(c.message.chat.id, "👥 <b>Admin controls</b>\n<blockquote>/public on|off — open/close the bot\n/users — list users\n/ban ID · /unban ID\n/setlimit ID N — project limit\n/broadcast text\n/grant ID · /revoke ID — allow list (when public is off)</blockquote>")
        else:
            bot.send_message(c.message.chat.id, "ℹ️ Your hosting permission is managed by the administrator.")

    elif action == "ghconnect":
        bot.answer_callback_query(c.id)
        connect_github_prompt(c.message.chat.id, c.from_user.id)

    elif action == "ghmanage":
        token = load_github_token(c.from_user.id)
        profile, error = github_profile(token) if token else (None, "Not connected")
        bot.answer_callback_query(c.id)
        if profile:
            bot.send_message(c.message.chat.id, github_profile_text(profile), reply_markup=github_manage_kb())
        else:
            bot.send_message(c.message.chat.id, f"❌ <b>GitHub connection unavailable:</b> {html.escape(error)}", reply_markup=main_menu_kb(c.from_user.id))

    elif action == "ghprofile":
        token = load_github_token(c.from_user.id)
        profile, error = github_profile(token) if token else (None, "Not connected")
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, github_profile_text(profile) if profile else f"❌ {html.escape(error)}", reply_markup=github_manage_kb() if profile else main_menu_kb(c.from_user.id))

    elif action.startswith("ghlist:"):
        token = load_github_token(c.from_user.id)
        repos, error = github_repositories(token) if token else (None, "Connect GitHub first")
        bot.answer_callback_query(c.id)
        if repos is None:
            bot.send_message(c.message.chat.id, f"❌ <b>Repository list failed:</b> {html.escape(error)}", reply_markup=github_manage_kb())
        else:
            _repo_cache[c.from_user.id] = repos
            bot.send_message(c.message.chat.id, f"📚 <b>Repositories</b> — {len(repos)} found", reply_markup=github_repo_list_kb(repos))

    elif action.startswith("ghrepo:"):
        bot.answer_callback_query(c.id)
        try:
            index = int(action.split(":", 1)[1])
            repo = _repo_cache.get(c.from_user.id, [])[index]
        except (ValueError, IndexError, TypeError):
            bot.send_message(c.message.chat.id, "❌ Repository list expired. Press Refresh repositories.")
        else:
            bot.send_message(c.message.chat.id, github_repo_text(repo), reply_markup=github_repo_actions_kb(repo.get("full_name", "")))

    elif action.startswith("ghcommits:"):
        full_name = action.split(":", 1)[1]
        parts = full_name.split("/", 1)
        bot.answer_callback_query(c.id)
        if len(parts) != 2:
            bot.send_message(c.message.chat.id, "❌ Invalid repository name.")
        else:
            status, payload = github_api(f"/repos/{urllib.parse.quote(parts[0])}/{urllib.parse.quote(parts[1])}/commits?per_page=5", load_github_token(c.from_user.id))
            if status == 200 and isinstance(payload, list) and payload:
                lines = []
                for item in payload:
                    commit = item.get("commit", {})
                    title = (commit.get("message") or "").split("\n", 1)[0][:80]
                    author = (commit.get("author") or {}).get("name", "unknown")
                    lines.append(f"• <code>{html.escape(str(item.get('sha', ''))[:7])}</code> {html.escape(title)} — <i>{html.escape(author)}</i>")
                bot.send_message(c.message.chat.id, f"📝 <b>Latest commits — {html.escape(full_name)}</b>\n" + "\n".join(lines))
            else:
                message = payload.get("message", "no commits found") if isinstance(payload, dict) else "no commits found"
                bot.send_message(c.message.chat.id, f"❌ Could not load commits: {html.escape(str(message))}")

    elif action.startswith("ghdelete:"):
        full_name = action.split(":", 1)[1]
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(styled_button("✅ Confirm delete", callback_data=f"ghdelete_confirm:{full_name}", style="danger"), styled_button("Cancel", callback_data="ghlist:0"))
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, f"⚠️ <b>Delete repository?</b>\n<blockquote>This permanently deletes <code>{html.escape(full_name)}</code> from GitHub.</blockquote>", reply_markup=kb)

    elif action.startswith("ghdelete_confirm:"):
        full_name = action.split(":", 1)[1]
        parts = full_name.split("/", 1)
        bot.answer_callback_query(c.id, "Deleting...")
        if len(parts) != 2:
            bot.send_message(c.message.chat.id, "❌ Invalid repository name.")
        else:
            status, payload = github_api(f"/repos/{urllib.parse.quote(parts[0])}/{urllib.parse.quote(parts[1])}", load_github_token(c.from_user.id), method="DELETE")
            if status == 204:
                bot.send_message(c.message.chat.id, f"✅ Deleted <code>{html.escape(full_name)}</code> from GitHub.", reply_markup=github_manage_kb())
            else:
                bot.send_message(c.message.chat.id, f"❌ Delete failed: {html.escape(str(payload.get('message', payload)))}", reply_markup=github_manage_kb())

    elif action.startswith("ghrun:"):
        full_name = action.split(":", 1)[1]
        bot.answer_callback_query(c.id, "Importing repository...")
        process_github_repo(c.message, f"https://github.com/{full_name}", user_id=c.from_user.id)

    elif action == "ghdisconnect":
        delete_github_token(c.from_user.id)
        bot.answer_callback_query(c.id, "Disconnected.")
        bot.send_message(c.message.chat.id, "✅ GitHub disconnected.", reply_markup=main_menu_kb(c.from_user.id))

    elif ":" in action:
        cmd, pid = action.split(":", 1)
        if pid not in data["projects"]:
            bot.answer_callback_query(c.id, "Project not found (deleted?).")
            return
        proj = data["projects"][pid]
        if not can_access(c.from_user.id, proj):
            bot.answer_callback_query(c.id, "This project belongs to another user.")
            return

        if cmd == "open":
            running = is_alive(proj.get("pid"))
            txt = f"📦 <b>{html.escape(proj['name'])}</b>\nStatus: {'🟢 running' if running else '🔴 stopped'}\nEntry: <code>{html.escape(proj.get('entry') or 'not set')}</code>"
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "run":
            ok, msg = start_project(pid, data)
            bot.answer_callback_query(c.id, msg[:190])
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))
            if ok and project_url_text(data["projects"][pid]):
                bot.send_message(c.message.chat.id, project_url_text(data["projects"][pid]))

        elif cmd == "stop":
            ok, msg = stop_project(pid, data)
            backup_async(pid)
            bot.answer_callback_query(c.id, msg[:190])
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "restart":
            stop_project(pid, data)
            data = load_data()
            ok, msg = start_project(pid, data)
            bot.answer_callback_query(c.id, "Restarted." if ok else msg)
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "logs":
            lp = log_path(pid)
            if not os.path.exists(lp):
                bot.answer_callback_query(c.id, "No logs yet.")
                return
            with open(lp, "r", errors="ignore") as f:
                lines = f.readlines()[-40:]
            text = "".join(lines).strip() or "(empty)"
            if len(text) > 3500:
                text = text[-3500:]
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"📜 <b>Last logs — {html.escape(proj['name'])}</b>\n<pre>{html.escape(text)}</pre>")

        elif cmd == "setentry":
            awaiting_entry[c.from_user.id] = pid
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Send the entry filename for <b>{proj['name']}</b> (e.g. main.py):")

        elif cmd == "ghpull":
            bot.answer_callback_query(c.id, "Checking GitHub...")
            ok, msg, changed = update_from_github(pid)
            data = load_data()
            bot.send_message(c.message.chat.id, ("✅ " if ok else "❌ ") + html.escape(plain_text(msg)[-1500:]), reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "ghauto":
            gh = proj.get("github")
            if not gh:
                bot.answer_callback_query(c.id, "Not a GitHub project.")
                return
            gh["auto"] = not gh.get("auto")
            save_data(data)
            bot.answer_callback_query(c.id, f"Auto-update {'ON' if gh['auto'] else 'OFF'} (checks every {AUTO_UPDATE_MINUTES} min)")
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "env":
            awaiting_env[c.from_user.id] = pid
            bot.answer_callback_query(c.id)
            keys = ", ".join(proj.get("env") or {}) or "none"
            bot.send_message(c.message.chat.id, (
                f"🔐 <b>Environment variables — {html.escape(proj['name'])}</b>\n"
                f"<blockquote>Current: {html.escape(keys)}</blockquote>\n"
                "Send <code>KEY=VALUE</code> (one per line) to set.\n"
                "<code>unset KEY</code> removes one, <code>clear</code> removes all, <code>show</code> lists names.\n"
                "Your message is deleted after saving. Restart the project to apply."
            ))

        elif cmd == "backup":
            bot.answer_callback_query(c.id, "Backing up...")
            ok, msg = backup_project(pid, force=True)
            bot.send_message(c.message.chat.id, ("💾 " if ok else "❌ ") + html.escape(msg))

        elif cmd == "dl":
            bot.answer_callback_query(c.id, "Preparing zip...")
            try:
                blob = build_archive(pid)
                if len(blob) > 49 * 1024 * 1024:
                    raise ValueError("zip is larger than Telegram's 50 MB bot upload limit")
                bio = io.BytesIO(blob)
                bio.name = re.sub(r"[^A-Za-z0-9._-]+", "_", proj["name"]) + ".zip"
                bot.send_document(c.message.chat.id, bio)
            except Exception as exc:
                bot.send_message(c.message.chat.id, f"❌ Download failed: {html.escape(str(exc))}")

        elif cmd == "del":
            stop_project(pid, data)
            shutil.rmtree(project_path(pid), ignore_errors=True)
            del data["projects"][pid]
            save_data(data)
            if store:
                threading.Thread(target=store.delete_archive, args=(pid,), daemon=True).start()
            bot.answer_callback_query(c.id, "Deleted.")
            bot.edit_message_text("📁 <b>Your Projects</b>", c.message.chat.id, c.message.message_id, reply_markup=project_list_kb(data, c.from_user.id))


def acquire_single_instance_lock():
    """Stop a second copy of this bot from polling with the same token (causes 409)."""
    try:
        import fcntl
    except ImportError:
        return None
    lock_file = open(os.path.join(BASE_DIR, ".hosting_bot.lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another instance of this bot is already running. Exiting.")
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file  # keep reference so the lock stays held


def clear_webhook():
    try:
        bot.remove_webhook()
        # also drop stale pending updates so old backlog does not replay
        bot.delete_webhook(drop_pending_updates=True)
        print("Webhook cleared.")
    except Exception as e:
        print(f"delete_webhook failed: {e}")


if __name__ == "__main__":
    _lock = acquire_single_instance_lock()
    init_storage()
    atexit.register(flush_data)
    restore_projects()
    normalize_pids()
    for _target in (autostart_projects, backup_loop, github_auto_update_loop):
        threading.Thread(target=_target, daemon=True, name=_target.__name__).start()
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT, _graceful_exit)
    print("Hosting bot running...")
    while True:
        clear_webhook()
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            msg = str(e)
            print(f"Polling crashed: {msg[:300]}")
            # 409 = webhook active or another getUpdates consumer; wait and retry
            time.sleep(15 if "409" in msg else 5)
        else:
            time.sleep(5)
