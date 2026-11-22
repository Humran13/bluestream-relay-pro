#!/usr/bin/env python3
"""Container-restart recovery check. Phases are run with `docker exec` around a `docker restart` of the container
(the whole process tree is killed mid-stream = a hard crash). State lives in /state (a bind mount that survives).

    restart_phase.py prepare   create nginx conf, media, licence, a published project (revision 2)
    restart_phase.py start     start nginx + the real supervise loop + sidecar; wait for a valid 720p30 stream
    restart_phase.py verify    check the persisted state and that stale files from the crash were handled

NOT a VPS reboot test: there is no systemd here, so nothing starts by itself after the restart (that is Gate B)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE = Path("/state")
REPO = Path("/work")
sys.path.insert(0, str(REPO))
SK = bytes(range(32))
ENV_KEYS = dict(BLUESTREAM_STUDIO_DIR=str(STATE / "studio"), BLUESTREAM_RUN_DIR=str(STATE / "run"),
                BLUESTREAM_MEDIA_DIR=str(STATE / "media"), BLUESTREAM_ETC_DIR=str(STATE / "etc"),
                BLUESTREAM_HLS_ROOT=str(STATE / "hls"))
os.environ.update(ENV_KEYS)


def env():
    from webapp.studio import ed25519
    e = dict(os.environ, PYTHONPATH=str(STATE / "pylib"), BS_REPO=str(REPO), BS_RELAY="rst", BS_PROJECT="rst",
             BLUESTREAM_STUDIO_PUBKEY_HEX=ed25519.public_key(SK).hex(), BLUESTREAM_STUDIO_MAX_LIVE="2")
    e.update(ENV_KEYS)
    return e


def probe(url):
    r = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "10000000", "-show_streams", "-of", "json", url],
                       capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout or "{}") if r.returncode == 0 else {}


def prepare():
    from webapp.studio import ed25519, entitlement, store
    from webapp.studio.model import Overlay, Project
    for d in ("studio", "run", "media", "etc", "hls", "logs", "pylib", "nginx"):
        (STATE / d).mkdir(parents=True, exist_ok=True)
    link = STATE / "pylib" / "studio"
    if not link.exists():
        link.symlink_to(REPO / "webapp" / "studio")
    tpl = (REPO / "config/nginx/rtmp.conf.template").read_text()
    rtmp = tpl.replace("__RTMP_BIND__", "127.0.0.1").replace("__RTMP_PORT__", "1935").replace("__HLS_ROOT__", str(STATE / "hls"))
    rtmp = rtmp.replace("rtmp {", "rtmp {\n    access_log off;", 1)
    n = str(STATE / "nginx")
    (STATE / "nginx/nginx.conf").write_text(
        "load_module /usr/lib/nginx/modules/ngx_rtmp_module.so;\nworker_processes 1;\npid %s/nginx.pid;\nerror_log %s/../logs/nginx-error.log info;\n"
        "events { worker_connections 128; }\nhttp { access_log off; client_body_temp_path %s/b; proxy_temp_path %s/p; fastcgi_temp_path %s/f; "
        "uwsgi_temp_path %s/u; scgi_temp_path %s/s; }\n%s\n" % (n, n, n, n, n, n, n, rtmp))
    media = STATE / "media" / "loop.mp4"
    if not media.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20", "-f", "lavfi", "-i",
                        "sine=duration=20", "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "aac", str(media)], check=True)
    os.environ["BLUESTREAM_STUDIO_PUBKEY_HEX"] = ed25519.public_key(SK).hex()
    entitlement.install_token(entitlement.make_token(SK, {"v": 1, "lic": "rst", "plan": "studio", "exp": time.time() + 86400}))
    p = Project("rst", "media", "loop.mp4", [Overlay("clock", x=0.05, y=0.05, w=0.3, h=0.12, size=64),
                                             Overlay("text", x=0.05, y=0.85, w=0.5, h=0.1, text="revision one")]).validate()
    store.save_draft(p)
    store.publish("rst")
    p.overlays[1] = Overlay("text", x=0.05, y=0.85, w=0.5, h=0.1, text="revision two")
    store.save_draft(p)
    assert store.publish("rst") == 2
    print("prepared: published revision 2")


def start():
    nginx = STATE / "nginx/nginx.conf"
    subprocess.Popen(["nginx", "-c", str(nginx), "-g", "daemon off;"], stdout=subprocess.DEVNULL,
                     stderr=open(STATE / "logs/nginx.out", "a"), start_new_session=True)
    time.sleep(2)
    # remove the status file of the previous (crashed) life, exactly like relay_start does, but KEEP stale leases/ports to prove handling
    (STATE / "run/relay-rst.status").unlink(missing_ok=True)
    subprocess.Popen(["bash", str(REPO / "tools/docker/supervise_harness.sh")], env=env(), stdout=open(STATE / "logs/sup.out", "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        info = probe("rtmp://127.0.0.1:1935/bluestream-relay/rst")
        if info.get("streams"):
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            assert (v["codec_name"], v["width"], v["height"]) == ("h264", 1280, 720), v
            print("stream live: %s %sx%s" % (v["codec_name"], v["width"], v["height"]))
            return
        time.sleep(2)
    print("FAIL: stream did not come back", open(STATE / "logs/sup.out").read()[-500:])
    sys.exit(1)


def verify():
    from webapp.studio import runtime, store
    p = store.load_published("rst")
    assert p.revision == 2 and p.overlays[1].text == "revision two", "published project did not survive the restart"
    assert store.revisions("rst") == [1, 2]
    status = (STATE / "run/relay-rst.status").read_text()
    assert "STATE=LIVE" in status, status
    leases = list((STATE / "run").glob("studio-*.live"))
    live = runtime.running_live_encodes()
    assert live == 1, "expected exactly one live lease (the current one), got %d: %s" % (live, leases)
    print("verified: revision 2 + history intact, supervisor LIVE, stale leases ignored/cleaned (live encodes=%d)" % live)


if __name__ == "__main__":
    {"prepare": prepare, "start": start, "verify": verify}[sys.argv[1]]()
