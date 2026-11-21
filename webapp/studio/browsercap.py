"""Gated, lightweight browser capture for Studio ("browser source").

WHY THIS IS A LAST RESORT. A screenshot of a cropped element does NOT make the
page cheap: Chromium still loads, lays out and runs the whole page. Prefer a Data
overlay (JSON -> text) whenever the number/text you want is available from an API.
This module therefore does ONE bounded capture every `interval` seconds (default
30+), never keeps a browser running between captures, and caps everything.

Security model (defence in depth - each layer is independent):
  1. Spec validation: https only, no credentials, public hostnames only, strict
     selector grammar, bounded viewport/interval.
  2. DNS pinning: every allowed host is resolved HERE, every answer must be public,
     and Chromium is told to use exactly those addresses
     (--host-resolver-rules="MAP host ip, MAP * ~NOTFOUND"). Any other host, any
     redirect to an unlisted host and any DNS-rebinding trick resolves to NOTFOUND.
  3. Chromium keeps its sandbox. `--no-sandbox` is never passed in production; if the
     sandbox cannot start, the capture is refused (see sandbox_works()).
  4. A dedicated systemd unit (config/systemd/bluestream-browser@.service) adds
     cgroup memory/CPU/task caps, kills the whole process group on stop (no orphans),
     denies all private/loopback/link-local/metadata IP ranges at the SOCKET layer
     (eBPF), and gives the worker only its own scratch directory.
  5. Per-capture timeout + process-group kill; temp profile deleted every time.
The feature stays OFF unless BLUESTREAM_STUDIO_BROWSER=1 AND Chromium is present AND
the sandbox self-test passes. OFF is the shipped default.
"""
import base64
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time

from . import netguard
from .model import ValidationError

SELECTOR_RE = re.compile(r"^[A-Za-z0-9 _#.>+~*:()\[\]=\"'^$|-]{1,200}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MIN_INTERVAL, MAX_INTERVAL = 15, 3600
MAX_VIEWPORT = (1920, 1080)
MIN_VIEWPORT = (320, 240)
CAPTURE_TIMEOUT = 40
BINARIES = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")


def enabled():
    return os.environ.get("BLUESTREAM_STUDIO_BROWSER") == "1"


def find_browser():
    explicit = os.environ.get("BLUESTREAM_BROWSER_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for name in BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# spec validation (used by Overlay.validate and again at capture time)
# ---------------------------------------------------------------------------
def validate_spec(url, selector, width, height, interval, extra_hosts=""):
    try:
        parts, port = netguard.validate_url(url)
    except netguard.BlockedError as exc:
        raise ValidationError("browser URL rejected: %s" % exc)
    if parts.scheme != "https":
        raise ValidationError("browser sources must use https")
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa")) \
            or "." not in host:
        raise ValidationError("browser URL must use a public hostname")
    import ipaddress
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise ValidationError("browser URL must use a hostname, not an IP address")
    if selector and not SELECTOR_RE.match(selector):
        raise ValidationError("selector contains unsupported characters")
    if not (MIN_VIEWPORT[0] <= width <= MAX_VIEWPORT[0] and MIN_VIEWPORT[1] <= height <= MAX_VIEWPORT[1]):
        raise ValidationError("viewport out of range")
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValidationError("capture interval must be %d-%d seconds" % (MIN_INTERVAL, MAX_INTERVAL))
    return allowed_hosts(parts.hostname, extra_hosts)


def allowed_hosts(primary, extra_hosts=""):
    hosts = [primary.lower()]
    for h in (x.strip() for x in (extra_hosts or "").split(",")):
        if not h:
            continue
        if not HOST_RE.match(h) or h.lower() in hosts:
            raise ValidationError("invalid or duplicate allowed host: %r" % h[:40])
        hosts.append(h.lower())
    if len(hosts) > 8:
        raise ValidationError("at most 8 hosts may be allowed")
    return hosts


def resolver_rules(hosts, resolve=netguard.resolve_public, port=443):
    """Chromium --host-resolver-rules: ONLY the listed hosts resolve, each pinned to a
    validated public IP; everything else is NOTFOUND."""
    rules = []
    for h in hosts:
        ips = resolve(h, port)
        rules.append("MAP %s %s" % (h, ips[0]))
    rules.append("MAP * ~NOTFOUND")
    return ", ".join(rules)


def chromium_args(binary, profile_dir, hosts, width, height, resolve=netguard.resolve_public,
                  allow_no_sandbox=False):
    args = [binary, "--headless=new", "--remote-debugging-pipe", "--user-data-dir=" + profile_dir,
            "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--disable-dev-shm-usage", "--mute-audio", "--hide-scrollbars", "--deny-permission-prompts",
            "--disable-features=Translate,MediaRouter,OptimizationHints", "--js-flags=--max-old-space-size=256",
            "--renderer-process-limit=1", "--window-size=%d,%d" % (width, height),
            "--host-resolver-rules=" + resolver_rules(hosts, resolve),
            "--proxy-server=direct://", "--proxy-bypass-list=*", "about:blank"]
    if allow_no_sandbox:  # TESTS ONLY - never reachable from a project/config
        args.insert(1, "--no-sandbox")
    return args


# ---------------------------------------------------------------------------
# minimal Chrome DevTools Protocol client over --remote-debugging-pipe (stdlib only)
# ---------------------------------------------------------------------------
class CaptureError(Exception):
    pass


def request_allowed(url, hosts, schemes=("https",)):
    """Browser-level egress policy for EVERY request the page makes (documents, scripts,
    images, XHR, redirects). Needed because Chromium's resolver rules do not apply to
    IP-literal URLs such as http://169.254.169.254/ ."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme in ("data", "about", "blob"):
        return True
    if parts.scheme not in schemes or parts.username or parts.password:
        return False
    return (parts.hostname or "").lower() in hosts


class _Cdp:
    def __init__(self, proc, rfd, wfd, deadline, hosts=(), schemes=("https",)):
        self.proc, self.rfd, self.wfd, self.deadline = proc, rfd, wfd, deadline
        self.buf, self.next_id, self.events = b"", 0, []
        self.hosts, self.schemes, self.blocked = set(hosts), schemes, 0

    def send(self, method, params=None, session=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.wfd, json.dumps(msg).encode() + b"\0")
        return self.next_id

    def _read_msg(self):
        while True:
            msg = self._read_raw()
            if msg.get("method") == "Fetch.requestPaused":
                self._on_paused(msg)
                continue
            return msg

    def _on_paused(self, msg):
        params, sess = msg.get("params", {}), msg.get("sessionId")
        rid = params.get("requestId")
        if request_allowed(params.get("request", {}).get("url", ""), self.hosts, self.schemes):
            self.send("Fetch.continueRequest", {"requestId": rid}, sess)
        else:
            self.blocked += 1
            self.send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"}, sess)

    def _read_raw(self):
        while b"\0" not in self.buf:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise CaptureError("capture timed out")
            r, _, _ = select.select([self.rfd], [], [], min(left, 1.0))
            if r:
                chunk = os.read(self.rfd, 1 << 20)
                if not chunk:
                    raise CaptureError("browser exited unexpectedly")
                self.buf += chunk
                if len(self.buf) > 64 * 1024 * 1024:
                    raise CaptureError("browser output too large")
            elif self.proc.poll() is not None:
                raise CaptureError("browser exited unexpectedly")
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, params=None, session=None):
        want = self.send(method, params, session)
        while True:
            msg = self._read_msg()
            if msg.get("id") == want:
                if "error" in msg:
                    raise CaptureError("%s failed: %s" % (method, str(msg["error"].get("message"))[:80]))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg["method"])

    def wait_event(self, name, session=None):
        while name not in self.events:
            msg = self._read_msg()
            if "method" in msg:
                self.events.append(msg["method"])


def capture(url, selector, width, height, out_png, extra_hosts="", resolve=netguard.resolve_public,
            browser=None, allow_no_sandbox=False, timeout=CAPTURE_TIMEOUT, settle=1.0,
            interval=30, test_mode=False):
    """One bounded capture -> PNG at out_png (atomic). Raises CaptureError / ValidationError.

    `test_mode` exists ONLY for the automated tests (plain-http loopback fixture page); it is
    never reachable from a project, config file or the console."""
    if test_mode:
        from urllib.parse import urlsplit
        hosts = allowed_hosts((urlsplit(url).hostname or "").lower(), extra_hosts)
    else:
        hosts = validate_spec(url, selector, width, height, interval, extra_hosts)
    binary = browser or find_browser()
    if not binary:
        raise CaptureError("no Chromium installed")
    profile = tempfile.mkdtemp(prefix="bsbrowser-")
    r_cmd, w_cmd = os.pipe()     # we -> chrome (fd 3 in the child)
    r_evt, w_evt = os.pipe()     # chrome -> we (fd 4 in the child)

    def child_setup():
        os.dup2(r_cmd, 3)
        os.dup2(w_evt, 4)
        os.set_inheritable(3, True)
        os.set_inheritable(4, True)

    proc = None
    try:
        args = chromium_args(binary, profile, hosts, width, height, resolve, allow_no_sandbox)
        proc = subprocess.Popen(args, preexec_fn=child_setup, close_fds=True, pass_fds=(3, 4),
                                start_new_session=True,   # own process group => whole tree killable
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r_cmd)
        os.close(w_evt)
        r_cmd = w_evt = -1
        cdp = _Cdp(proc, r_evt, w_cmd, time.monotonic() + timeout, hosts,
                   ("https", "http") if test_mode else ("https",))
        target = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.call("Page.enable", session=session)
        cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session)   # egress policy per request
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                 "deviceScaleFactor": 1, "mobile": False}, session)
        cdp.call("Page.navigate", {"url": url}, session)
        cdp.wait_event("Page.loadEventFired", session)
        time.sleep(settle)
        clip = None
        if selector:
            expr = ("(function(){var e=document.querySelector(%s);if(!e)return null;"
                    "e.scrollIntoView({block:'start'});var r=e.getBoundingClientRect();"
                    "return {x:r.left,y:r.top,width:r.width,height:r.height};})()" % json.dumps(selector))
            res = cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session)
            rect = res.get("result", {}).get("value")
            if not rect or rect["width"] < 2 or rect["height"] < 2:
                raise CaptureError("selector matched nothing visible")
            clip = {"x": max(0, rect["x"]), "y": max(0, rect["y"]),
                    "width": min(rect["width"], width), "height": min(rect["height"], height), "scale": 1}
        params = {"format": "png"}
        if clip:
            params["clip"] = clip
        data = base64.b64decode(cdp.call("Page.captureScreenshot", params, session)["data"])
        if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 100:
            raise CaptureError("browser returned an invalid image")
        return _store_if_changed(out_png, data)
    finally:
        for fd in (r_cmd, w_evt, w_cmd, r_evt):
            if isinstance(fd, int) and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if proc is not None:
            _kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)


def _kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # whole group: no orphaned renderer/GPU/zygote
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _store_if_changed(path, data):
    """Atomic write; skip entirely when identical (cache unchanged captures).
    Returns True when the file changed."""
    new = hashlib.sha256(data).hexdigest()
    try:
        with open(path, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() == new:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def sandbox_works(browser=None, timeout=20):
    """Self-test: can Chromium start WITH its sandbox? (about:blank, no network)."""
    binary = browser or find_browser()
    if not binary:
        return False
    with tempfile.TemporaryDirectory() as d:
        try:
            out = os.path.join(d, "x.png")
            # about:blank needs no hosts; use a throwaway public https URL spec only for validation
            hosts = ["example.com"]
            profile = os.path.join(d, "p")
            os.mkdir(profile)
            r = subprocess.run([binary, "--headless=new", "--user-data-dir=" + profile, "--screenshot=" + out,
                                "--window-size=320,240", "about:blank"], capture_output=True, timeout=timeout)
            del hosts
            return r.returncode == 0 and os.path.exists(out)
        except (OSError, subprocess.TimeoutExpired):
            return False


_usable_cache = {"at": 0.0, "val": None}


def usable(ttl=60):
    """(ok, reason) - the single gate every layer consults. The (slow) sandbox
    self-test result is cached for `ttl` seconds."""
    if not enabled():
        return False, "browser source is disabled (BLUESTREAM_STUDIO_BROWSER is not 1)"
    if not find_browser():
        return False, "Chromium is not installed"
    now = time.monotonic()
    if _usable_cache["val"] is None or now - _usable_cache["at"] > ttl:
        _usable_cache["val"] = sandbox_works()
        _usable_cache["at"] = now
    if not _usable_cache["val"]:
        return False, "Chromium cannot start with its sandbox on this host (refusing to run without it)"
    return True, "ready"


# ---------------------------------------------------------------------------
# worker loop (runs inside the isolated systemd unit)
# ---------------------------------------------------------------------------
def png_path(run_dir, relay, index):
    return os.path.join(run_dir, "studio-%s-br%d.png" % (relay, index))


def worker(relay, overlays, run_dir, stop, sleep=time.sleep, now=time.monotonic, do_capture=capture):
    """Capture each browser overlay on its own cadence. On failure the LAST VALID image
    stays in place; repeated failures back off (x2, max 10 min) so a broken page cannot
    burn CPU. Returns when stop() is truthy."""
    state = {i: {"next": 0.0, "fails": 0} for i, _ in overlays}
    while not stop():
        t = now()
        for i, o in overlays:
            st = state[i]
            if t < st["next"]:
                continue
            try:
                do_capture(o.url, o.selector, o.view_w, o.view_h, png_path(run_dir, relay, i),
                           o.allowed_hosts, interval=o.refresh_s)
                st["fails"], st["next"] = 0, t + o.refresh_s
            except (CaptureError, ValidationError, netguard.BlockedError, OSError) as exc:
                st["fails"] += 1
                st["next"] = t + min(600, o.refresh_s * (2 ** min(st["fails"], 5)))
                try:
                    import sys
                    sys.stderr.write("studio-browser: overlay %d capture failed (%s); keeping last image\n"
                                     % (i, type(exc).__name__))
                except OSError:
                    pass
        sleep(1.0)
