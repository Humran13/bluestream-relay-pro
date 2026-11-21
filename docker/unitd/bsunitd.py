#!/usr/bin/env python3
"""bsunitd - a minimal systemd-compatible unit manager for the BlueStream container.

Why it exists: the BlueStream engine (lib/*.sh, web-ctl, run-*.sh) is built on systemd units (start/stop/enable,
Restart=, BindsTo=, drop-ins, journal). Containers have no systemd, and running real systemd would need a privileged
container. This daemon runs the SAME unit files, with the semantics BlueStream actually relies on, as ordinary
child processes of the container's init. It is NOT a general systemd replacement and refuses what it does not model.

Modelled: [Service] ExecStart/ExecReload (single line), Restart=no|on-failure|always, RestartSec, User/Group,
Environment, TimeoutStopSec, [Unit] BindsTo/StartLimitIntervalSec/StartLimitBurst, instance templates (foo@.service, %i/%I/%n),
drop-ins (<unit>.d/*.conf), boot persistence ("enabled" markers), log capture for journalctl.
NOT modelled (ignored): sandboxing directives (the container is the sandbox), timers, sockets, cgroup accounting.
"""
import collections
import fnmatch
import glob
import json
import os
import re
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

UNIT_DIRS = [os.environ.get("BS_UNITD_UNITS", "/etc/systemd/system"), "/usr/local/lib/bluestream/docker/units"]
STATE_DIR = os.environ.get("BS_UNITD_STATE", "/var/lib/bluestream/units")      # enabled markers (persistent volume)
RUN_DIR = os.environ.get("BS_UNITD_RUN", "/run/bsunitd")                        # socket (tmpfs, root only)
LOG_DIR = os.environ.get("BS_UNITD_LOGS", "/var/log/bluestream-units")
SOCK = os.path.join(RUN_DIR, "sock")
LOG_MAX = int(os.environ.get("BS_UNITD_LOG_MAX", str(8 * 1024 * 1024)))
PASS_ENV_PREFIXES = ("BLUESTREAM_", "BS_", "TZ", "LANG", "LC_")
NAME_RE = re.compile(r"^[A-Za-z0-9:_.@\\-]{1,200}\.service$")
BOOT_PRIORITY = ("nginx.service", "bluestream-nginx.service", "bluestream-web.service")


# --------------------------------------------------------------------------- unit files
def unit_name(name):
    name = name.strip()
    if not name.endswith(".service"):
        name += ".service"
    if not NAME_RE.match(name):
        raise ValueError("invalid unit name")
    return name


def template_of(name):
    """foo@bar.service -> (foo@.service, bar); plain unit -> (name, '')."""
    m = re.match(r"^(.*)@(.+)\.service$", name)
    return (m.group(1) + "@.service", m.group(2)) if m else (name, "")


def find_unit_file(name):
    tmpl, inst = template_of(name)
    for d in UNIT_DIRS:
        for cand in (name, tmpl):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p, inst
    return None, inst


def _parse_ini(text):
    section, out = None, collections.defaultdict(lambda: collections.defaultdict(list))
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line and section:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                out[section][k] = []          # empty assignment resets (systemd semantics)
            else:
                out[section][k].append(v)
    return out


def load_unit(name):
    path, inst = find_unit_file(name)
    if not path:
        return None
    ini = _parse_ini(open(path, encoding="utf-8").read())
    for d in UNIT_DIRS:                         # drop-ins: <name>.d and <template>.d (later wins / accumulates)
        for base in {name, template_of(name)[0]}:
            for conf in sorted(glob.glob(os.path.join(d, base + ".d", "*.conf"))):
                extra = _parse_ini(open(conf, encoding="utf-8").read())
                for sec, kv in extra.items():
                    for k, vals in kv.items():
                        if vals == []:
                            ini[sec][k] = []
                        else:
                            ini[sec][k] = ini[sec][k] + vals
    subs = {"%i": inst, "%I": inst, "%n": name, "%N": name[:-8], "%%": "%"}

    def sub(s):
        for k, v in subs.items():
            s = s.replace(k, v)
        return s

    svc, unit = ini["Service"], ini["Unit"]

    def one(sec, key, default=None):
        v = sec.get(key)
        return sub(v[-1]) if v else default

    execs = svc.get("ExecStart") or []
    if not execs:
        return {"name": name, "error": "no ExecStart"}
    def num(s, default):
        try:
            return float(re.sub(r"[a-z]+$", "", s)) if s else default
        except ValueError:
            return default
    return {
        "name": name, "path": path, "instance": inst,
        "exec": sub(execs[-1]).lstrip("-@+!"), "reload": (sub(svc["ExecReload"][-1]).lstrip("-@+!") if svc.get("ExecReload") else None),
        "restart": one(svc, "Restart", "no"), "restart_sec": num(one(svc, "RestartSec", "0.1"), 0.1),
        "user": one(svc, "User"), "group": one(svc, "Group"),
        "cwd": (one(svc, "WorkingDirectory", "/") or "/").lstrip("-"),
        "env": [sub(e) for e in svc.get("Environment", [])],
        "stop_timeout": num(one(svc, "TimeoutStopSec", "15"), 15.0),
        "binds_to": [sub(x) for v in unit.get("BindsTo", []) for x in v.split()],
        "limit_interval": num(one(unit, "StartLimitIntervalSec") or one(svc, "StartLimitIntervalSec"), 10.0),
        "limit_burst": int(num(one(unit, "StartLimitBurst") or one(svc, "StartLimitBurst"), 5)),
        "description": one(unit, "Description", name),
    }


def parse_env_assignments(items):
    env = {}
    for item in items:
        try:
            for tok in shlex.split(item):
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    env[k] = v
        except ValueError:
            continue
    return env


# --------------------------------------------------------------------------- logging (journal substitute)
class UnitLog:
    def __init__(self, name):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, re.sub(r"[^A-Za-z0-9_.@:-]", "_", name) + ".log")
        self.lock = threading.Lock()

    def write(self, pid, ident, text):
        line = "%.6f %s[%s]: %s\n" % (time.time(), ident, pid, text.rstrip("\n")[:2000])
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(line)
            except OSError:
                pass


# --------------------------------------------------------------------------- manager
class Unit:
    def __init__(self, name):
        self.name = name
        self.state = "inactive"          # inactive | activating | active | deactivating | failed
        self.result = "success"
        self.proc = None
        self.pid = 0
        self.exit_status = 0
        self.n_restarts = 0
        self.active_enter = 0.0
        self.starts = collections.deque()
        self.want_stop = False
        self.gen = 0                      # bumped on every explicit start/stop (invalidates stale restart timers)
        self.log = UnitLog(name)


class Manager:
    def __init__(self):
        self.units = {}
        self.lock = threading.RLock()
        os.makedirs(os.path.join(STATE_DIR, "enabled"), exist_ok=True)
        self.shutting_down = False

    # -- helpers
    def get(self, name):
        with self.lock:
            if name not in self.units:
                self.units[name] = Unit(name)
            return self.units[name]

    def _spawn(self, u, spec):
        env = {k: v for k, v in os.environ.items() if k.startswith(PASS_ENV_PREFIXES) or k in ("PATH", "HOME")}
        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env.update(parse_env_assignments(spec["env"]))
        env["INVOCATION_ID"] = "%032x" % int.from_bytes(os.urandom(16), "big")
        argv = shlex.split(spec["exec"])
        preexec = None
        if spec.get("user") and spec["user"] != "root":
            import grp
            import pwd
            pw = pwd.getpwnam(spec["user"])
            gid = grp.getgrnam(spec["group"]).gr_gid if spec.get("group") else pw.pw_gid
            env["HOME"] = pw.pw_dir if os.path.isdir(pw.pw_dir) else "/"

            def preexec():
                os.setgroups([])
                os.initgroups(pw.pw_name, gid)
                os.setgid(gid)
                os.setuid(pw.pw_uid)
        proc = subprocess.Popen(argv, env=env, cwd=spec.get("cwd") or "/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, preexec_fn=preexec, close_fds=True)
        return proc

    def _pump(self, u, proc, ident):
        for raw in iter(proc.stdout.readline, b""):
            u.log.write(proc.pid, ident, raw.decode("utf-8", "replace"))
        proc.stdout.close()

    # -- state machine
    def start(self, name):
        name = unit_name(name)
        spec = load_unit(name)
        if spec is None:
            return False, "Unit %s not found." % name
        if spec.get("error"):
            return False, "%s: %s" % (name, spec["error"])
        with self.lock:
            u = self.get(name)
            if u.state in ("active", "activating"):
                return True, ""
            for dep in spec["binds_to"]:
                if self.get(unit_name(dep)).state != "active":
                    return False, "Dependency %s is not active; refusing to start %s." % (dep, name)
            u.gen += 1
            u.want_stop = False
            u.n_restarts = 0
            u.starts.clear()
            u.result = "success"
            return self._launch(u, spec)

    def _launch(self, u, spec):
        now = time.monotonic()
        u.starts.append(now)
        while u.starts and now - u.starts[0] > spec["limit_interval"]:
            u.starts.popleft()
        if len(u.starts) > spec["limit_burst"]:
            u.state, u.result = "failed", "start-limit-hit"
            u.log.write(0, spec["name"], "Start request repeated too quickly: start-limit-hit")
            return False, "start-limit-hit"
        try:
            proc = self._spawn(u, spec)
        except (OSError, ValueError, KeyError) as exc:
            u.state, u.result = "failed", "exec-failed"
            u.log.write(0, spec["name"], "failed to start: %s" % exc)
            return False, "failed to start: %s" % exc
        u.proc, u.pid, u.state, u.active_enter = proc, proc.pid, "active", time.time()
        ident = (spec["exec"].split()[0].rsplit("/", 1)[-1] or u.name)[:40]
        threading.Thread(target=self._pump, args=(u, proc, ident), daemon=True).start()
        threading.Thread(target=self._wait, args=(u, spec, proc, u.gen), daemon=True).start()
        return True, ""

    def _wait(self, u, spec, proc, gen):
        rc = proc.wait()
        with self.lock:
            if u.proc is not proc:
                return
            u.exit_status = rc
            u.pid = 0
            if u.want_stop or gen != u.gen:
                u.state, u.result = "inactive", "success"
                self._release_dependents(u.name)
                return
            ok = rc == 0
            policy = spec["restart"]
            restart = policy == "always" or (policy == "on-failure" and not ok)
            if not restart:
                if ok:
                    u.state, u.result = "inactive", "success"
                else:
                    u.state, u.result = "failed", ("signal" if rc < 0 else "exit-code")
                self._release_dependents(u.name)
                return
            u.state = "activating"                  # auto-restart pending
            u.result = "exit-code" if rc > 0 else ("signal" if rc < 0 else "success")
            self._release_dependents(u.name)
        delay = spec["restart_sec"]
        threading.Timer(delay, self._auto_restart, args=(u, spec, gen)).start()

    def _auto_restart(self, u, spec, gen):
        with self.lock:
            if self.shutting_down or gen != u.gen or u.want_stop or u.state != "activating":
                return
            u.n_restarts += 1
            self._launch(u, spec)

    def _release_dependents(self, bound):
        """systemd BindsTo: when the bound unit leaves 'active', units bound to it are stopped."""
        for other in list(self.units.values()):
            if other.name == bound or other.state not in ("active", "activating"):
                continue
            spec = load_unit(other.name)
            if spec and any(unit_name(d) == bound for d in spec["binds_to"]):
                threading.Thread(target=self.stop, args=(other.name,), daemon=True).start()

    def stop(self, name):
        name = unit_name(name)
        spec = load_unit(name) or {}
        timeout = spec.get("stop_timeout", 15.0)
        with self.lock:
            u = self.get(name)
            u.gen += 1                     # cancels any pending auto-restart
            u.want_stop = True
            proc = u.proc
            running = proc is not None and proc.poll() is None
            if running:
                u.state = "deactivating"
            else:
                u.state, u.result, u.pid = "inactive", "success", 0
        if running:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=10)
            with self.lock:
                u.state, u.result, u.pid = "inactive", "success", 0
        self._release_dependents(name)
        return True, ""

    def restart(self, name):
        self.stop(name)
        return self.start(name)

    def reload(self, name):
        name = unit_name(name)
        spec = load_unit(name)
        if not spec or not spec.get("reload"):
            return False, "Job type reload is not applicable for %s." % name
        if self.get(name).state != "active":
            return False, "Unit %s is not active." % name
        r = subprocess.run(shlex.split(spec["reload"]), capture_output=True, text=True, timeout=30)
        return r.returncode == 0, r.stderr.strip()

    def reset_failed(self, name):
        u = self.get(unit_name(name))
        with self.lock:
            if u.state == "failed":
                u.state, u.result = "inactive", "success"
            u.n_restarts = 0
            u.starts.clear()
        return True, ""

    # -- queries
    def snapshot(self, name):
        name = unit_name(name)
        with self.lock:
            u = self.units.get(name)
            if u is None:
                return {"name": name, "ActiveState": "inactive", "SubState": "dead", "Result": "success", "NRestarts": 0,
                        "MainPID": 0, "ExecMainStatus": 0, "ActiveEnterTimestamp": "", "loaded": load_unit(name) is not None}
            sub = {"active": "running", "activating": "auto-restart", "deactivating": "stop-sigterm", "failed": "failed", "inactive": "dead"}[u.state]
            ts = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(u.active_enter)) if u.active_enter else ""
            return {"name": name, "ActiveState": u.state, "SubState": sub, "Result": u.result, "NRestarts": u.n_restarts, "MainPID": u.pid,
                    "ExecMainStatus": u.exit_status, "ActiveEnterTimestamp": ts, "loaded": True}

    def list_units(self, patterns):
        with self.lock:
            names = sorted(self.units)
        out = []
        for n in names:
            if patterns and not any(fnmatch.fnmatchcase(n, p) for p in patterns):
                continue
            s = self.snapshot(n)
            out.append({"unit": n, "active": s["ActiveState"], "sub": s["SubState"], "description": (load_unit(n) or {}).get("description", n)})
        return out

    # -- boot / shutdown
    def enabled_units(self):
        d = os.path.join(STATE_DIR, "enabled")
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        return sorted(names, key=lambda n: (BOOT_PRIORITY.index(n) if n in BOOT_PRIORITY else len(BOOT_PRIORITY), n))

    def boot(self):
        for n in self.enabled_units():
            ok, msg = self.start(n)
            sys.stderr.write("bsunitd: boot start %s: %s\n" % (n, "ok" if ok else msg))
            if n in BOOT_PRIORITY[:3]:
                time.sleep(0.3)

    def shutdown(self):
        self.shutting_down = True
        with self.lock:
            names = [n for n, u in self.units.items() if u.state in ("active", "activating")]
        for n in names:
            try:
                self.stop(n)
            except Exception:
                pass


# --------------------------------------------------------------------------- socket server
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline(65536).decode("utf-8"))
            op, args = req.get("op"), req.get("args", [])
            m = self.server.manager
            if op == "start":
                ok, msg = m.start(args[0])
            elif op == "stop":
                ok, msg = m.stop(args[0])
            elif op == "restart":
                ok, msg = m.restart(args[0])
            elif op == "reload":
                ok, msg = m.reload(args[0])
            elif op == "reset-failed":
                ok, msg = m.reset_failed(args[0])
            elif op == "show":
                self.wfile.write((json.dumps({"ok": True, "data": m.snapshot(args[0])}) + "\n").encode())
                return
            elif op == "list":
                self.wfile.write((json.dumps({"ok": True, "data": m.list_units(args)}) + "\n").encode())
                return
            elif op == "ping":
                ok, msg = True, "pong"
            else:
                ok, msg = False, "unknown operation"
            self.wfile.write((json.dumps({"ok": ok, "msg": msg}) + "\n").encode())
        except (ValueError, KeyError, IndexError, OSError) as exc:
            try:
                self.wfile.write((json.dumps({"ok": False, "msg": "bad request: %s" % type(exc).__name__}) + "\n").encode())
            except OSError:
                pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    mgr = Manager()
    srv = Server(SOCK, Handler)
    os.chmod(SOCK, 0o600)
    srv.manager = mgr
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    mgr.boot()
    sys.stderr.write("bsunitd: ready (%d enabled unit(s) started)\n" % len(mgr.enabled_units()))
    stop.wait()
    sys.stderr.write("bsunitd: shutting down\n")
    mgr.shutdown()
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
