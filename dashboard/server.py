"""The web dashboard, and the status screen on the laptop's own display.

Runs as the agent account on 127.0.0.1:7380 (see modules/dashboard.nix).
`tailscale serve` puts it on the tailnet and adds the Tailscale-User-Login
header to every request; the dashboard only answers logins that own this
machine or are listed in `dashboardUsers` in settings.nix.

/screen, its files and /api/screen need no login. They are what the laptop's
own display shows (modules/screen.nix) and hold nothing secret. When
`agency screen show URL` has picked another page, /screen shows that page
fullscreen instead, until `agency screen reset`.

/settings moves a machine's setup to another one: `agency export` makes an
encrypted file to download, and an uploaded one goes to `agency import`.

/terminal is the web terminal (ttyd on 127.0.0.1:7381). It goes through this
server too, so it has the same login check.

Changes go through the `agency` command, as jobs that run outside this
service (systemd-run --user), so a rebuild that restarts the dashboard does
not stop the rebuild. One job runs at a time.

Python standard library only.
"""

import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = int(os.environ.get("AGENCY_DASHBOARD_PORT", "7380"))
UI = Path(os.environ.get("AGENCY_UI", Path(__file__).parent / "ui"))
FONTS = Path(os.environ.get("AGENCY_FONTS", Path(__file__).parent.parent / "installer/ui/fonts"))
USER = os.environ.get("AGENCY_USER", "agent")
EXTRA_LOGINS = {u.strip().lower() for u in os.environ.get("AGENCY_DASHBOARD_USERS", "").split() if u.strip()}
TERMINAL = ("127.0.0.1", int(os.environ.get("AGENCY_TERMINAL_PORT", "7381")))
# Overridable so the server can be tried off the real machine.
REPO = Path(os.environ.get("AGENCY_REPO", "/etc/nixos"))
# Parts of the repo the running system never uses: the download page, docs,
# READMEs and CI. A commit that touches only these is not an update.
# Keep in step with NOT_SYSTEM in cli/agency.py.
NOT_SYSTEM = ("site", "docs", ".github", "**/README.md")
URL_FILE = Path(os.environ.get("AGENCY_URL_FILE", "/run/agency/dashboard-url"))
PROFILE = Path(os.environ.get("AGENCY_PROFILE", "/nix/var/nix/profiles/system"))
RECOVERY_KEY = "/root/luks-recovery-key.txt"
SCREEN_URL = Path(os.environ.get("AGENCY_SCREEN_URL", "/var/lib/agency/screen-url"))
HOME = Path.home()

LOGIN_HEADER = "Tailscale-User-Login"
SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def sh(*cmd, timeout=30, env=None, stdin=None):
    """Run a command; return (ok, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, input=stdin, errors="replace")
        return p.returncode == 0, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)


def cached(ttl):
    """Remember a function's result for ttl seconds."""
    def wrap(fn):
        memo = {}
        lock = threading.Lock()

        def inner(*args):
            with lock:
                hit = memo.get(args)
                if hit and time.time() - hit[0] < ttl:
                    return hit[1]
            value = fn(*args)
            with lock:
                memo[args] = (time.time(), value)
            return value

        inner.clear = memo.clear
        return inner
    return wrap


def which(name):
    """Find a program, including the agent's Bun, npm and pip installs."""
    extra = [str(HOME / ".bun/bin"), str(HOME / ".npm-global/bin"), str(HOME / ".local/bin")]
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", "")] + extra))


# ------------------------------------------------------------------ facts ---


@cached(20)
def tailscale_status():
    ok, out, _ = sh("tailscale", "status", "--json", timeout=10)
    try:
        return json.loads(out) if ok else {}
    except ValueError:
        return {}


def owner_login():
    """The login of the person whose tailnet this machine is on."""
    st = tailscale_status()
    user = (st.get("User") or {}).get(str((st.get("Self") or {}).get("UserID", "")))
    return (user or {}).get("LoginName", "").lower()


def tailnet_name():
    st = tailscale_status()
    if st.get("BackendState") != "Running":
        return ""
    return ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")


def read_setting(key):
    try:
        match = re.search(rf"^  {key} = (.*);$", (REPO / "settings.nix").read_text(), re.M)
    except OSError:
        return None
    return match.group(1) if match else None


def desktop_count():
    value = read_setting("desktops")
    return int(value) if value and value.isdigit() else 1


def unit_state(unit):
    _, out, _ = sh("systemctl", "is-active", unit, timeout=5)
    return out.strip() or "unknown"


def machine_state():
    _, out, _ = sh("systemctl", "is-system-running", timeout=5)
    return out.strip() or "unknown"


def windows(n):
    ok, out, _ = sh("wmctrl", "-l", timeout=5, env=dict(os.environ, DISPLAY=f":{n}"))
    return len(out.splitlines()) if ok else None


def desktops():
    found = []
    for n in range(1, desktop_count() + 1):
        state = unit_state(f"agency-display@{n}.service")
        found.append({"n": n, "port": 6080 + n, "running": state == "active",
                      "windows": windows(n) if state == "active" else None})
    return found


class Shots:
    """Screenshots of the desktops, at most one every two seconds each."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agency-dashboard-shots-"))
        self.locks = {}
        self.taken = {}

    def get(self, n):
        lock = self.locks.setdefault(n, threading.Lock())
        path = self.dir / f"{n}.jpg"
        with lock:
            if time.time() - self.taken.get(n, 0) > 2:
                ok, _, _ = sh("scrot", "--overwrite", "--silent", "--quality", "60", str(path),
                              timeout=10, env=dict(os.environ, DISPLAY=f":{n}"))
                self.taken[n] = time.time()
                if not ok:
                    path.unlink(missing_ok=True)
            return path.read_bytes() if path.exists() else None


shots = Shots()

# Where each agent CLI keeps its sign-in.
TOOLS = [
    ("github", "GitHub", "gh", None),
    ("claude", "Claude Code", "claude", ".claude/.credentials.json"),
    ("codex", "Codex", "codex", ".codex/auth.json"),
    ("opencode", "OpenCode", "opencode", ".local/share/opencode/auth.json"),
    # T3 keeps no plain sign-in file; the dashboard notes a finished sign-in.
    ("t3", "T3 Code", "t3", ".local/state/agency/t3-connected"),
]


@cached(30)
def accounts():
    found = []
    for tool_id, label, program, auth_file in TOOLS:
        entry = {"id": tool_id, "label": label, "installed": bool(which(program)), "signedIn": False, "detail": ""}
        if tool_id == "github" and entry["installed"]:
            ok, out, err = sh("gh", "auth", "status", "--hostname", "github.com", timeout=15)
            entry["signedIn"] = ok
            name = re.search(r"(?:account|as) (\S+)", out + err) if ok else None
            entry["detail"] = f"Signed in as {name.group(1)}" if name else ("Signed in" if ok else "Not signed in")
        elif entry["installed"]:
            entry["signedIn"] = (HOME / auth_file).exists()
            entry["detail"] = "Signed in" if entry["signedIn"] else "Not signed in"
        else:
            continue  # only tools that are installed; add others under Apps
        found.append(entry)
    return found


@cached(600)
def apps():
    ok, out, err = sh("agency", "apps", "list", "--json", timeout=180)
    try:
        listed = json.loads(out) if ok else None
    except ValueError:
        listed = None
    if not isinstance(listed, list):
        last = (err or out).strip().splitlines()
        return {"apps": [], "error": last[-1] if last else "agency apps failed"}
    found = []
    for app in listed:
        if not APP_ID.match(str(app.get("id", ""))):
            continue
        found.append({
            "id": app["id"],
            "label": app.get("label") or app["id"],
            "group": app.get("group") or "Other",
            "description": app.get("description") or "",
            "kind": app.get("kind"),
            "signin": app.get("signin") or "",
            "on": bool(app.get("on")),
            "logo": (UI / "logos" / f"{app['id']}.svg").is_file(),
        })
    return {"apps": found, "error": ""}


def secrets():
    """Each secret's name, and when it was added and last set (None if the
    history does not say, as for secrets that came with an import)."""
    ok, out, _ = sh("agency", "secret", "list", timeout=30)
    names = [line.strip() for line in out.splitlines() if SECRET_NAME.match(line.strip())] if ok else []
    # `agency secret` commits "Set secret NAME" and "Remove secret NAME".
    _, log, _ = sh("git", "-C", str(REPO), "log", "--format=%ct %s", "--", "secrets")
    added, changed = {}, {}
    for line in reversed(log.splitlines()):  # oldest first
        stamp, _, subject = line.partition(" ")
        verb, _, name = subject.partition(" secret ")
        if verb == "Set" and name:
            added.setdefault(name, int(stamp))
            changed[name] = int(stamp)
        elif verb == "Remove":
            added.pop(name, None)
            changed.pop(name, None)
    return [{"name": n, "added": added.get(n), "changed": changed.get(n)} for n in names]


# ------------------------------------------------------------- transfer ---
# Exports are made here, one at a time, and an uploaded export waits here
# for its import. ~/.cache is left out of exports, so they never nest.

TRANSFER = HOME / ".cache" / "agency"
EXPORTS = TRANSFER / "export"
UPLOAD = TRANSFER / "import" / "upload.agency"
MIN_PASSPHRASE = 10


def newest_export():
    try:
        files = [f for f in EXPORTS.glob("*.agency") if f.is_file()]
    except OSError:
        return None
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def transfer():
    found = newest_export()
    upload = UPLOAD.stat() if UPLOAD.is_file() else None
    return {
        "export": {"name": found.name, "size": found.stat().st_size, "made": found.stat().st_mtime} if found else None,
        "upload": {"size": upload.st_size, "made": upload.st_mtime} if upload else None,
        "free": shutil.disk_usage(HOME).free,
        "minPassphrase": MIN_PASSPHRASE,
    }


def generation():
    """The running system's generation number and when it was made."""
    try:
        link = os.readlink(PROFILE)
    except OSError:
        return None, None
    m = re.search(r"system-(\d+)-link", link)
    target = PROFILE.parent / link if not os.path.isabs(link) else Path(link)
    try:
        made = os.lstat(target).st_mtime
    except OSError:
        made = None
    return (int(m.group(1)) if m else None), made


def memory():
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total = int(info["MemTotal"].split()[0]) * 1024
        available = int(info["MemAvailable"].split()[0]) * 1024
        return {"used": total - available, "total": total}
    except (OSError, KeyError, ValueError):
        return None


# ----------------------------------------------------------------- vitals ---
# CPU and memory, sampled every few seconds into a short history, so the
# home page draws a full chart the moment it opens. Disks come from lsblk.

class Vitals:
    EVERY = 3        # seconds between samples
    KEEP = 200       # samples kept: ten minutes

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []   # [time, cpu %, memory used]
        self.last = None    # (busy, total) jiffies at the last sample

    @staticmethod
    def jiffies():
        fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        idle = fields[3] + fields[4]  # idle + iowait
        total = sum(fields[:8])       # guest time is already in user
        return total - idle, total

    def sample(self):
        busy, total = self.jiffies()
        mem = memory()
        if self.last and total > self.last[1]:
            cpu = 100 * (busy - self.last[0]) / (total - self.last[1])
            with self.lock:
                self.samples.append([round(time.time()), round(cpu, 1), mem["used"] if mem else None])
                del self.samples[:-self.KEEP]
        self.last = (busy, total)

    def loop(self):
        while True:
            try:
                self.sample()
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(self.EVERY)

    def get(self):
        with self.lock:
            history = list(self.samples)
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            load = None
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError):
            uptime = None
        return {
            "every": self.EVERY,
            "history": history,
            "cores": os.cpu_count(),
            "load": load,
            "temp": cpu_temp(),
            "uptime": uptime,
            "memory": memory(),
            "swap": swap(),
            "disks": disks(),
        }


vitals = Vitals()


def swap():
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total = int(info["SwapTotal"].split()[0]) * 1024
        free = int(info["SwapFree"].split()[0]) * 1024
        return {"used": total - free, "total": total} if total else None
    except (OSError, KeyError, ValueError):
        return None


def cpu_temp():
    """The CPU package's temperature in °C, or None."""
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        if name not in ("coretemp", "k10temp", "zenpower"):
            continue
        try:
            return round(int((hwmon / "temp1_input").read_text()) / 1000)
        except (OSError, ValueError):
            continue
    return None


@cached(15)
def disks():
    """Whole disks (not partitions): size, model and the space used on
    the filesystems mounted from them."""
    ok, out, _ = sh("lsblk", "--json", "--bytes", "--output",
                    "NAME,SIZE,TYPE,MODEL,TRAN,RM,FSUSED,FSSIZE,MOUNTPOINTS", timeout=10)
    if not ok:
        return []
    try:
        tree = json.loads(out)["blockdevices"]
    except (ValueError, KeyError):
        return []
    found = []
    for d in tree:
        if d.get("type") != "disk" or re.match(r"(zram|loop|ram)\d", d["name"]):
            continue
        used = size = 0
        mounts = []
        stack = list(d.get("children") or [])
        while stack:
            node = stack.pop()
            stack.extend(node.get("children") or [])
            points = [m for m in (node.get("mountpoints") or []) if m and m.startswith("/")]
            if points and node.get("fssize"):
                used += int(node.get("fsused") or 0)
                size += int(node["fssize"])
                mounts.extend(points)
        found.append({
            "name": d["name"],
            "model": (d.get("model") or "").strip(),
            "kind": "USB" if d.get("tran") == "usb" else "NVMe" if d.get("tran") == "nvme"
                    else "SATA" if d.get("tran") == "sata" else "",
            "removable": bool(d.get("rm")),
            "size": int(d.get("size") or 0),
            "used": used,
            "fsSize": size,
            "system": "/" in mounts,
            "mounts": sorted(mounts, key=len),
        })
    found.sort(key=lambda x: (not x["system"], x["name"]))
    return found


@cached(30)
def recovery_key_present():
    ok, _, _ = sh("sudo", "-n", "test", "-e", RECOVERY_KEY, timeout=10)
    return ok


def system():
    gen, made = generation()
    return {
        "generation": gen,
        "generationTime": made,
        "recoveryKey": recovery_key_present(),
    }


# ---------------------------------------------------------------- updates ---


class Updates:
    """Whether the repo has new commits. Fetched every six hours, or on request."""

    def __init__(self):
        self.lock = threading.Lock()
        linked = (REPO / ".git").is_dir()
        self.state = {"status": "unlinked" if not linked else "unchecked", "linked": linked,
                      "behind": 0, "subjects": [], "repo": "", "checked": None, "error": ""}

    @staticmethod
    def status_of(state):
        """unlinked, behind, error, current or unchecked, in that order."""
        if not state["linked"]:
            return "unlinked"
        if state["behind"]:
            return "behind"
        if state["error"]:
            return "error"
        return "current" if state["checked"] else "unchecked"

    def check(self, fetch=True):
        linked = (REPO / ".git").is_dir()
        state = {"linked": linked, "behind": 0, "subjects": [], "repo": "", "checked": time.time(), "error": ""}
        if linked:
            _, url, _ = sh("git", "-C", str(REPO), "remote", "get-url", "origin", timeout=10)
            state["repo"] = re.sub(r"^https://github\.com/|\.git$", "", url.strip())
            if fetch:
                ok, _, err = sh("git", "-C", str(REPO), "fetch", "-q", "origin", timeout=90)
                if not ok:
                    state["error"] = "Could not reach the repo to check for updates."
            ok, out, _ = sh("git", "-C", str(REPO), "log", "--format=%s", "HEAD..@{u}", "--", ".",
                            *(f":(exclude,glob){p}" for p in NOT_SYSTEM), timeout=10)
            subjects = out.splitlines() if ok else []
            state.update(behind=len(subjects), subjects=subjects[:5])
        state["status"] = self.status_of(state)
        with self.lock:
            self.state = state
        return state

    def loop(self):
        time.sleep(60)
        while True:
            self.check()
            time.sleep(6 * 3600)


updates = Updates()


# ------------------------------------------------------------------- jobs ---
# A job is one `agency` command. It runs in its own transient user unit, so
# it keeps going if this server restarts. Its log, exit status and a note of
# what it is live in STATE, which survives the restart too.
#
# A job's state comes from those files, so a restarted server agrees with
# the one that started it:
#
#   idle     no job.json                         start() -> running
#   running  job.json, no .status, runner alive  wrapper exits -> done/failed
#   done     .status is 0                        dismiss() -> idle, start() -> running
#   failed   .status is not 0, or the runner     dismiss() -> idle, start() -> running
#            died without writing one (code null)
#
# job.json names the runner ("systemd" and a unit, or "direct" and a pid),
# so liveness is checked the way the job was really started. Leaving
# running is handled once per job: job.json gets "seen" when it has been.

# systemd-run --user and systemctl --user find the agent's user manager here.
if not os.environ.get("XDG_RUNTIME_DIR") and Path(f"/run/user/{os.getuid()}").is_dir():
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

STATE = Path(os.environ.get("AGENCY_DASHBOARD_STATE")
             or Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "agency-dashboard")

# Runs the command with stdin from a file (deleted once open), then writes
# the exit status where the server looks for it.
WRAPPER = ('st="$1"; in="$2"; shift 2; '
           'if [ -n "$in" ]; then exec 3<"$in"; rm -f "$in"; else exec 3</dev/null; fi; '
           '"$@" <&3 3<&-; echo $? > "$st"')


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.use_systemd = bool(shutil.which("systemd-run")) and not os.environ.get("AGENCY_JOBS_DIRECT")

    def current(self):
        try:
            return json.loads((STATE / "job.json").read_text())
        except (OSError, ValueError):
            return None

    def status(self):
        job = self.current()
        if not job:
            return None
        log_file = STATE / f"{job['id']}.log"
        status_file = STATE / f"{job['id']}.status"
        try:
            log = log_file.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            log = []
        state, code = "running", None
        if status_file.exists():
            text = status_file.read_text().strip()
            code = int(text) if text.lstrip("-").isdigit() else 1
            state = "done" if code == 0 else "failed"
        elif time.time() - job["started"] > 5 and not self.alive(job):
            state, code = "failed", None
        return {**job, "state": state, "code": code, "log": log}

    def alive(self, job):
        if job.get("runner", "systemd") == "systemd":
            _, out, _ = sh("systemctl", "--user", "is-active", job["unit"], timeout=5)
            return out.strip() in ("active", "activating", "reloading")
        if self.proc is not None and self.proc.pid == job.get("pid"):
            return self.proc.poll() is None
        # Started by an earlier server: the pid is ours only if it is still
        # the wrapper for this job (its argv names the job's status file).
        try:
            argv = Path(f"/proc/{job.get('pid')}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        return str(STATE / f"{job['id']}.status").encode() in argv

    def running(self):
        st = self.status()
        return bool(st and st["state"] == "running")

    def start(self, kind, title, cmd, stdin=None, extra=None, done=None):
        """title says what runs ("Adding Blender"); done, what it did once
        it worked ("Added Blender")."""
        with self.lock:
            if self.running():
                return {"ok": False, "busy": True, "error": "Another change is still running. Wait for it to finish."}
            STATE.mkdir(parents=True, exist_ok=True)
            os.chmod(STATE, 0o700)
            job_id = time.strftime("%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}"
            log, status = STATE / f"{job_id}.log", STATE / f"{job_id}.status"
            infile = ""
            unit = f"agency-dashboard-job-{job_id}"
            argv = [shutil.which("sh") or "/bin/sh", "-c", WRAPPER, "sh", str(status), "", *cmd]
            try:
                if stdin is not None:
                    infile = argv[5] = str(STATE / f"{job_id}.in")
                    fd = os.open(infile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(stdin)
                log.write_text("")
                runner = {"runner": "systemd", "unit": unit}
                if self.use_systemd:
                    path = os.pathsep.join(["/run/wrappers/bin", os.environ.get("PATH", "")])
                    ok, _, err = sh("systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                                    f"--description=agency dashboard: {title}",
                                    f"--setenv=PATH={path}",
                                    f"--property=StandardOutput=append:{log}",
                                    f"--property=StandardError=append:{log}",
                                    "--", *argv, timeout=20)
                    if not ok:
                        # No user manager to run it in: run it here instead.
                        print(f"systemd-run failed, running the job directly: {err.strip()}", file=sys.stderr)
                        self.use_systemd = False
                if not self.use_systemd:
                    with open(log, "ab") as out:
                        self.proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                                                     stdin=subprocess.DEVNULL, start_new_session=True)
                    runner = {"runner": "direct", "pid": self.proc.pid}
            except OSError as e:
                if infile:
                    Path(infile).unlink(missing_ok=True)
                return {"ok": False, "error": f"Could not start it: {e}"}
            for old in STATE.glob("*"):
                if old.name.split(".")[0] not in (job_id, "job", "queue"):
                    old.unlink(missing_ok=True)
            (STATE / "job.json").write_text(json.dumps(
                {"id": job_id, "kind": kind, "title": title, "done": done or title, "started": time.time(),
                 **runner, **(extra or {})}))
            return {"ok": True}

    def dismiss(self):
        with self.lock:
            if not self.running():
                (STATE / "job.json").unlink(missing_ok=True)

    def after_finish(self, st):
        """Refresh what a finished job may have changed, once per job."""
        if not st or st["state"] == "running" or st.get("seen"):
            return
        with self.lock:
            job = self.current()
            if not job or job["id"] != st["id"] or job.get("seen"):
                return
            (STATE / "job.json").write_text(json.dumps({**job, "seen": True}))
        apps.clear()
        accounts.clear()
        if st.get("kind") == "import" and st["state"] == "done":
            UPLOAD.unlink(missing_ok=True)
        threading.Thread(target=updates.check, args=(False,), daemon=True).start()


jobs = Jobs()


# ------------------------------------------------------------- app queue ---
# Apps clicked while a job runs wait here, in STATE/queue.json, and go as one
# job (one rebuild at most) when nothing runs. The server sends them, so they
# go whether or not a page is still open, and survive a restart.

def start_apps_change(adds, removes):
    known = {a["id"]: a for a in apps()["apps"]}
    names = lambda xs: joined([known.get(x, {}).get("label", x) for x in xs])
    def said(add, remove):  # ("Adding", "removing") -> "Adding X; removing Y"
        parts = ([f"{add} {names(adds)}"] if adds else []) + ([f"{remove} {names(removes)}"] if removes else [])
        return "; ".join(parts)[:1].upper() + "; ".join(parts)[1:]
    title, done = said("Adding", "removing"), said("Added", "removed")
    cmd = ["agency", "apps", "change"]
    for x in adds:
        cmd += ["--add", x]
    for x in removes:
        cmd += ["--remove", x]
    return jobs.start("apps", title, cmd, extra={"apps": {"add": adds, "remove": removes}}, done=done)


class AppQueue:
    def __init__(self):
        self.lock = threading.Lock()
        self.file = STATE / "queue.json"

    def get(self):
        """{app id: "add" | "remove"}"""
        try:
            q = json.loads(self.file.read_text())
            return {k: v for k, v in q.items() if v in ("add", "remove")} if isinstance(q, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, q):
        STATE.mkdir(parents=True, exist_ok=True)
        if q:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(q))
            tmp.replace(self.file)
        else:
            self.file.unlink(missing_ok=True)

    def set(self, app_id, want):
        app_id = str(app_id or "")
        if not APP_ID.match(app_id) or app_id not in {a["id"] for a in apps()["apps"]}:
            return {"ok": False, "error": "There is no app with that name."}
        if want not in ("add", "remove", None):
            return {"ok": False, "error": "Say add or remove."}
        with self.lock:
            q = self.get()
            if want:
                q[app_id] = want
            else:
                q.pop(app_id, None)
            self._write(q)
        return self.flush()

    def flush(self):
        """Send the queue as one job, if there is one and nothing runs."""
        with self.lock:
            q = self.get()
            if not q or jobs.running():
                return {"ok": True}
            adds = [k for k, v in q.items() if v == "add"]
            removes = [k for k, v in q.items() if v == "remove"]
            result = start_apps_change(adds, removes)
            if not result.get("busy"):
                # Started, or failed for good: either way it has left the queue.
                self._write({})
            if not result["ok"] and not result.get("busy"):
                print(f"queued app change did not start: {result.get('error')}", file=sys.stderr)
            return result if not result.get("busy") else {"ok": True}

    def loop(self):
        while True:
            time.sleep(2)
            if self.file.exists():
                try:
                    jobs.after_finish(jobs.status())
                    self.flush()
                except Exception as e:  # keep the loop alive
                    print(f"app queue: {e}", file=sys.stderr)


app_queue = AppQueue()


# ----------------------------------------------------------------- screen ---


def qr_svg(text):
    ok, out, _ = sh("qrencode", "-t", "SVG", "-m", "0", "--foreground=101813", "--background=eef5e6",
                    "-o", "-", text, timeout=10)
    return out[out.find("<svg"):] if ok and "<svg" in out else ""


qr_cached = cached(3600)(qr_svg)


def when(stamp):
    if not stamp:
        return ""
    then, now = datetime.fromtimestamp(stamp), datetime.now()
    days = (now.date() - then.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return then.strftime("on %-d %b" if then.year == now.year else "on %-d %b %Y")


def joined(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@cached(60)
def console_password():
    """Whether the agent account has a password, so the console can be used."""
    ok, out, _ = sh("sudo", "-n", "passwd", "-S", USER, timeout=10)
    return ok and len(out.split()) > 1 and out.split()[1] == "P"


def screen_page():
    """The page `agency screen show` picked for the display, or ""."""
    try:
        url = SCREEN_URL.read_text().strip()
    except OSError:
        return ""
    return url if re.match(r"^https?://\S+$", url) else ""


def screen():
    name = tailnet_name()
    try:
        url = URL_FILE.read_text().strip()
    except OSError:
        url = ""
    _, made = generation()
    up = updates.state
    updated = f"updated {when(made)}" if made else ""
    if up["status"] == "current":
        text = "Up to date" + (f", {updated}" if updated else "")
    elif up["status"] == "behind":
        text = f"{up['behind']} update{'s' if up['behind'] != 1 else ''} ready" + (f", {updated}" if updated else "")
    else:
        text = updated.capitalize() or "Unknown"
    state = machine_state()
    return {
        "hostname": socket.gethostname().split(".")[0],
        "state": "Starting" if state in ("starting", "initializing") else "Running",
        "tailscale": name,
        "desktops": joined([f":{n}" for n in range(1, desktop_count() + 1)]),
        "system": text,
        "url": url if name else "",
        "qr": qr_cached(url) if name and url else "",
        "console": console_password(),
        "show": screen_page(),
    }



# ------------------------------------------------------------ GitHub ---
# Sign in with GitHub's device flow, the same one `gh auth login` uses: the
# page shows a short code, you enter it at github.com/login/device on any
# device, and the token goes to `gh auth login --with-token`. No terminal.

GH_CLIENT_ID = "178c6fc778ccc68e1d6a"  # the GitHub CLI's own OAuth app
GH_SCOPES = "repo read:org gist workflow"


def github_post(url, fields):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    req = Request(url, data=urlencode(fields).encode(), headers={"Accept": "application/json"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


class CodeLogin:
    """A sign-in that shows a URL (and usually a short code) on the page.

        idle --start--> starting --URL seen--> waiting --signed in--> done
                           |                      |
                           +------- error --------+---------------> failed
        any --cancel--> idle;  done/failed --start--> starting

    start() does nothing while starting or waiting. Each start or cancel
    begins a new run; a run that is no longer current changes nothing, so
    a cancelled sign-in cannot come back.
    """

    BUSY = ("starting", "waiting")

    def __init__(self):
        self.lock = threading.Lock()
        self.run_id = 0
        self.state = self.blank("idle")

    @staticmethod
    def blank(state):
        return {"state": state, "code": "", "url": "", "error": ""}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        with self.lock:
            if self.state["state"] in self.BUSY:
                return dict(self.state)
            self.run_id += 1
            self.state = self.blank("starting")
            run_id = self.run_id
        threading.Thread(target=self._run, args=(run_id,), daemon=True).start()
        return self.snapshot()

    def cancel(self):
        with self.lock:
            self.run_id += 1
            self.state = self.blank("idle")
            self._stop()
        return self.snapshot()

    def _stop(self):
        """Stop the current run's work. Called with the lock held."""

    def _set(self, run_id, **kw):
        """Update the state if run_id is still current; say whether it is."""
        with self.lock:
            if run_id == self.run_id:
                self.state.update(kw)
            return run_id == self.run_id

    def _run(self, run_id):
        raise NotImplementedError


class GitHubLogin(CodeLogin):
    def _run(self, run_id):
        try:
            dev = github_post("https://github.com/login/device/code",
                              {"client_id": GH_CLIENT_ID, "scope": GH_SCOPES})
        except Exception:
            self._set(run_id, state="failed", error="Could not reach GitHub.")
            return
        if not self._set(run_id, state="waiting", code=dev["user_code"], url=dev["verification_uri"]):
            return  # cancelled
        interval = int(dev.get("interval", 5))
        deadline = time.time() + int(dev.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            if not self._set(run_id):
                return  # cancelled
            try:
                res = github_post("https://github.com/login/oauth/access_token", {
                    "client_id": GH_CLIENT_ID, "device_code": dev["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            except Exception:
                continue
            err = res.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err:
                self._set(run_id, state="failed", code="", url="",
                          error="The code ran out. Try again." if err == "expired_token" else "GitHub said no.")
                return
            if not self._set(run_id):
                return  # cancelled while GitHub answered
            ok, _, _ = sh("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https",
                          "--with-token", stdin=res["access_token"] + "\n")
            if ok:
                sh("gh", "auth", "setup-git")
                sh("agency-git-identity")
            accounts.clear()
            self._set(run_id, state="done" if ok else "failed", code="", url="",
                      error="" if ok else "gh could not save the sign-in.")
            return
        self._set(run_id, state="failed", code="", url="", error="The code ran out. Try again.")


class CliCodeLogin(CodeLogin):
    """A CLI that signs in with a URL and maybe a short code, without a terminal.

    The program runs with no stdin; the page shows the URL (and code) it
    prints, and the sign-in is done when the program exits 0.
    """

    URL = re.compile(r"https://\S+")
    CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5})\b")

    def __init__(self, cmd, done_file):
        super().__init__()
        self.cmd = cmd
        self.done_file = done_file
        self.proc = None

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()

    def _run(self, run_id):
        prog = which(self.cmd[0])
        if not prog:
            self._set(run_id, state="failed", error="It is not installed.")
            return
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(HOME / ".bun/bin"), str(HOME / ".npm-global/bin"), env.get("PATH", "")])
        try:
            proc = subprocess.Popen([prog, *self.cmd[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace", env=env, cwd=HOME)
        except OSError as e:
            self._set(run_id, state="failed", error=str(e))
            return
        with self.lock:
            current = run_id == self.run_id
            if current:
                self.proc = proc
        if not current:
            proc.terminate()  # cancelled while it started
            proc.wait()
            return
        tail = []
        for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw).strip()
            tail = (tail + [line])[-3:]
            with self.lock:
                if run_id != self.run_id:
                    break
                url, code = self.URL.search(line), self.CODE.search(line)
                if url and not self.state["url"]:
                    self.state.update(url=url.group(0))
                if code and not self.state["code"]:
                    self.state.update(code=code.group(1))
                # The URL is enough to act on; a code may follow it.
                if self.state["url"]:
                    self.state["state"] = "waiting"
        ok = proc.wait() == 0
        with self.lock:
            if run_id != self.run_id:
                return  # cancelled
            self.proc = None
            if ok:
                self.done_file.parent.mkdir(parents=True, exist_ok=True)
                self.done_file.touch()
                self.state = self.blank("done")
            else:
                self.state.update(state="failed", code="", url="",
                                  error=tail[-1] if tail else "The sign-in stopped.")
        accounts.clear()


github_login = GitHubLogin()

# T3 Connect, plus T3's background service, so `t3 serve` keeps running.
code_logins = {
    "t3": CliCodeLogin(["t3", "up", "--connect", "--yes", "--headless"],
                       HOME / ".local/state/agency/t3-connected"),
}

# ------------------------------------------------------------------- HTTP ---

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
         ".svg": "image/svg+xml"}
OPEN_FILES = {"/favicon.svg": "favicon.svg", "/screen": "screen.html", "/screen.css": "screen.css", "/screen.js": "screen.js"}
FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/apps": "apps.html",
    "/app.js": "app.js",
    "/apps.js": "apps.js",
    "/settings": "settings.html",
    "/settings.js": "settings.js",
    "/common.js": "common.js",
    "/style.css": "style.css",
}
SHOT = re.compile(r"^/api/desktops/(\d+)/shot$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # --- replies

    def send(self, status, body, ctype, extra=None, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send(status, json.dumps(data).encode(), "application/json")

    def send_file(self, path, cache="no-store"):
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        self.send(200, data, TYPES.get(path.suffix, "application/octet-stream"), cache=cache)

    def refuse(self, why):
        page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Not allowed</title><body style="font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;color:#15201a;background:#eef1ec">
<h1 style="font-size:1.5rem">Not allowed</h1><p>{html.escape(why)}</p></body>"""
        if self.path.startswith("/api/"):
            return self.send_json({"error": why}, 403)
        self.send(403, page.encode(), TYPES[".html"])

    # --- checks

    def login(self):
        """The Tailscale login allowed to use the dashboard, or None (after refusing)."""
        login = (self.headers.get(LOGIN_HEADER) or "").strip().lower()
        if not login:
            self.refuse("The dashboard only opens through Tailscale. Open this machine's "
                        "Tailscale address from a device on your tailnet.")
            return None
        if login != owner_login() and login not in EXTRA_LOGINS:
            self.refuse(f"You are signed in to Tailscale as {login}, which does not own this machine. "
                        "To let this login in, add it to dashboardUsers in settings.nix and rebuild.")
            return None
        return login

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        hosts = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", "")}
        return urlsplit(origin).netloc in hosts - {""}

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # --- routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        # The laptop's own screen: no login.
        if path in OPEN_FILES:
            return self.send_file(UI / OPEN_FILES[path])
        if path.startswith("/fonts/"):
            name = path[len("/fonts/"):]
            if re.match(r"^[a-z0-9-]+\.woff2$", name):
                # The faces never change, and a page that waits for them
                # draws its text twice: keep them for a week.
                return self.send_file(FONTS / name, cache="max-age=604800")
            return self.send_json({"error": "not found"}, 404)
        if path == "/api/screen":
            return self.send_json(screen())

        login = self.login()
        if not login:
            return
        if path == "/terminal" or path.startswith("/terminal/"):
            return self.proxy_terminal()
        if path in FILES:
            return self.send_file(UI / FILES[path])
        if path.startswith("/logos/"):
            name = path[len("/logos/"):]
            if re.match(r"^[a-z0-9][a-z0-9-]*\.svg$", name):
                return self.send_file(UI / "logos" / name)
            return self.send_json({"error": "not found"}, 404)
        if path == "/api/state":
            job = jobs.status()
            jobs.after_finish(job)
            return self.send_json({
                "hostname": socket.gethostname().split(".")[0],
                "login": login,
                "state": machine_state(),
                "tailscale": tailnet_name(),
                "desktops": desktops(),
                "accounts": accounts(),
                "secrets": secrets(),
                "system": system(),
                "updates": updates.state,
                "vitals": vitals.get(),
                "job": job,
                "queue": app_queue.get(),
            })
        if path == "/api/apps":
            return self.send_json(apps())
        if path == "/api/transfer":
            return self.send_json(transfer())
        if path == "/api/export/download":
            return self.send_download()
        if path == "/api/github":
            return self.send_json(github_login.snapshot())
        found = re.fullmatch(r"/api/login/([a-z0-9]+)", path)
        if found and found.group(1) in code_logins:
            return self.send_json(code_logins[found.group(1)].snapshot())
        if path == "/api/job":
            job = jobs.status()
            jobs.after_finish(job)
            return self.send_json({"job": job})
        shot = SHOT.match(path)
        if shot and 1 <= int(shot.group(1)) <= desktop_count():
            data = shots.get(int(shot.group(1)))
            if data is None:
                return self.send_json({"error": "no picture"}, 404)
            return self.send(200, data, "image/jpeg")
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self.login():
            return
        if path.startswith("/terminal"):
            return self.proxy_terminal()
        if path == "/api/import/upload":
            return self.receive_upload()
        # A page on another site cannot send JSON here, or claim our origin.
        if not self.same_origin() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send_json({"error": "bad request"}, 400)
        data = self.body()

        if path == "/api/update/check":
            return self.send_json(updates.check())
        if path == "/api/github/start":
            return self.send_json(github_login.start())
        if path == "/api/github/cancel":
            return self.send_json(github_login.cancel())
        found = re.fullmatch(r"/api/login/([a-z0-9]+)/(start|cancel)", path)
        if found and found.group(1) in code_logins:
            login = code_logins[found.group(1)]
            return self.send_json(login.start() if found.group(2) == "start" else login.cancel())
        if path == "/api/job/dismiss":
            jobs.dismiss()
            return self.send_json({"ok": True})

        count = desktop_count()
        job = None
        if path == "/api/update":
            job = ("update", "Updating", ["agency", "update"], "Updated")
        elif path == "/api/rollback":
            job = ("rollback", "Going back to the previous system", ["agency", "rollback"], "Went back to the previous system")
        elif path == "/api/desktops/add":
            job = ("desktop", f"Adding desktop :{count + 1}", ["agency", "desktop", "add"], f"Added desktop :{count + 1}")
        elif path == "/api/desktops/remove":
            if count == 1:
                return self.send_json({"ok": False, "error": "Desktop :1 always stays."})
            job = ("desktop", f"Removing desktop :{count}", ["agency", "desktop", "remove"], f"Removed desktop :{count}")
        elif path == "/api/apps/queue":
            # {"id": id, "want": "add" | "remove" | null}. null takes the app
            # out of the queue again.
            return self.send_json(app_queue.set(data.get("id"), data.get("want")))
        elif path == "/api/secrets/set":
            name, value = str(data.get("name", "")), data.get("value")
            if not SECRET_NAME.match(name):
                return self.send_json({"ok": False, "error": "Use a name like OPENAI_API_KEY: capitals, digits and _."})
            if not isinstance(value, str) or not value.strip():
                return self.send_json({"ok": False, "error": "The value is empty."})
            # The value goes to `agency secret set` on stdin, never in argv.
            return self.send_json(jobs.start("secrets", f"Saving {name}", ["agency", "secret", "set", name],
                                             stdin=value.rstrip("\n") + "\n", done=f"Saved {name}"))
        elif path == "/api/export":
            passphrase = data.get("passphrase")
            if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
                return self.send_json({"ok": False, "error": f"Use a passphrase of at least {MIN_PASSPHRASE} characters."})
            if "\n" in passphrase:
                return self.send_json({"ok": False, "error": "The passphrase cannot have a line break."})
            if jobs.running():
                return self.send_json({"ok": False, "busy": True, "error": "Another change is still running. Wait for it to finish."})
            EXPORTS.mkdir(parents=True, exist_ok=True)
            os.chmod(EXPORTS, 0o700)
            for old in EXPORTS.iterdir():
                old.unlink(missing_ok=True)
            out = EXPORTS / f"agency-{socket.gethostname().split('.')[0]}-{time.strftime('%Y%m%d-%H%M')}.agency"
            home = bool(data.get("home"))
            cmd = ["agency", "export", "-o", str(out)] + (["--home"] if home else [])
            what = "this machine" + (" and its home folder" if home else "")
            return self.send_json(jobs.start("export", f"Exporting {what}", cmd, stdin=passphrase + "\n",
                                             done=f"Exported {what}"))
        elif path == "/api/export/delete":
            if jobs.running() and (jobs.current() or {}).get("kind") == "export":
                return self.send_json({"ok": False, "error": "The export is still being made."})
            if EXPORTS.is_dir():
                for old in EXPORTS.iterdir():
                    old.unlink(missing_ok=True)
            return self.send_json({"ok": True})
        elif path == "/api/import":
            passphrase = data.get("passphrase")
            if not UPLOAD.is_file():
                return self.send_json({"ok": False, "error": "Upload an export first."})
            if not isinstance(passphrase, str) or not passphrase or "\n" in passphrase:
                return self.send_json({"ok": False, "error": "Enter the passphrase the export was made with."})
            cmd = ["agency", "import", str(UPLOAD)] + ([] if data.get("home", True) else ["--no-home"])
            return self.send_json(jobs.start("import", "Importing a machine's setup", cmd, stdin=passphrase + "\n",
                                             done="Imported a machine's setup"))
        elif path == "/api/import/discard":
            if jobs.running() and (jobs.current() or {}).get("kind") == "import":
                return self.send_json({"ok": False, "error": "The import is still running."})
            UPLOAD.unlink(missing_ok=True)
            return self.send_json({"ok": True})
        elif path == "/api/secrets/remove":
            name = str(data.get("name", ""))
            if not SECRET_NAME.match(name):
                return self.send_json({"ok": False, "error": "There is no secret with that name."})
            job = ("secrets", f"Removing {name}", ["agency", "secret", "remove", name], f"Removed {name}")
        if job is None:
            return self.send_json({"error": "not found"}, 404)
        kind, title, cmd, done = job
        return self.send_json(jobs.start(kind, title, cmd, done=done))

    # --- exports, in and out

    def send_download(self):
        found = newest_export()
        if not found or (jobs.running() and (jobs.current() or {}).get("kind") == "export"):
            return self.send_json({"error": "There is no export to download."}, 404)
        size = found.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{found.name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        with found.open("rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    def receive_upload(self):
        """An export, as the raw request body. Only this page can send it:
        another site would need CORS for this content type, and no Origin
        header from a browser means no upload."""
        if not self.headers.get("Origin") or not self.same_origin() or \
                self.headers.get("Content-Type") != "application/octet-stream":
            return self.send_json({"error": "bad request"}, 400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self.send_json({"ok": False, "error": "The file is empty."}, 400)
        if length > shutil.disk_usage(HOME).free - (2 << 30):
            self.close_connection = True
            return self.send_json({"ok": False, "error": "There is not enough free disk space for that file."}, 413)
        if jobs.running() and (jobs.current() or {}).get("kind") == "import":
            self.close_connection = True
            return self.send_json({"ok": False, "error": "An import is running."}, 409)
        UPLOAD.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(UPLOAD.parent, 0o700)
        part = UPLOAD.with_name(f"upload-{threading.get_ident()}.part")
        left = length
        try:
            with open(part, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise OSError("the upload stopped")
                    f.write(chunk)
                    left -= len(chunk)
            os.chmod(part, 0o600)
            part.replace(UPLOAD)
        except OSError:
            part.unlink(missing_ok=True)
            self.close_connection = True
            return self.send_json({"ok": False, "error": "The upload stopped before it finished."}, 400)
        return self.send_json({"ok": True, "size": length})

    # --- the web terminal

    def proxy_terminal(self):
        """Pass the request (and a WebSocket, if it is one) to ttyd as it is."""
        if not self.same_origin():
            return self.send_json({"error": "bad request"}, 400)
        try:
            upstream = socket.create_connection(TERMINAL, timeout=5)
        except OSError:
            return self.send(502, b"The terminal is not running. Over SSH: systemctl status agency-terminal",
                             "text/plain; charset=utf-8")
        upstream.settimeout(None)
        upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
        lines = [f"{self.command} {self.path} HTTP/1.1"]
        for key, value in self.headers.items():
            if key.lower() == "connection" and not upgrade:
                continue
            lines.append(f"{key}: {value}")
        if not upgrade:
            lines.append("Connection: close")
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            upstream.sendall(self.rfile.read(length))
        self.close_connection = True

        if not upgrade:
            # Add a frame rule to ttyd's reply, so no other site can frame it.
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                head += chunk
            end = head.find(b"\r\n\r\n")
            if end >= 0:
                head = head[:end] + b"\r\nX-Frame-Options: SAMEORIGIN" + head[end:]
            self.connection.sendall(head)
            while chunk := upstream.recv(65536):
                self.connection.sendall(chunk)
            upstream.close()
            return

        def downstream():
            try:
                while chunk := upstream.recv(65536):
                    self.connection.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (self.connection, upstream):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=downstream, daemon=True).start()
        try:
            while chunk := self.rfile.read1(65536):
                upstream.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream.close()


if __name__ == "__main__":
    threading.Thread(target=updates.loop, daemon=True).start()
    threading.Thread(target=vitals.loop, daemon=True).start()
    threading.Thread(target=app_queue.loop, daemon=True).start()
    host = os.environ.get("AGENCY_DASHBOARD_HOST", "127.0.0.1")
    print(f"agency dashboard on http://{host}:{PORT}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()
