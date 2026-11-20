"""Destination ownership: exactly one publisher per destination (real flock)."""
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
HAVE_FLOCK = bool(shutil.which("flock"))

PRELUDE = textwrap.dedent("""
    set -u
    repo="%s"
    tmp="%s"
    . "$repo/lib/common.sh"; . "$repo/lib/destinations.sh"; . "$repo/lib/relay.sh"
    . "$repo/lib/playlist.sh"; . "$repo/lib/publisher.sh"
    BLUESTREAM_RUN_DIR="$tmp/run"; mkdir -p "$BLUESTREAM_RUN_DIR"
""")


def posix(p):
    s = str(p).replace("\\", "/")
    return s


@unittest.skipUnless(BASH and HAVE_FLOCK, "needs bash + flock (Linux CI)")
class FlockOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, body, timeout=30):
        text = PRELUDE % (posix(REPO), posix(self.tmp.name)) + textwrap.dedent(body)
        r = subprocess.run([BASH, "-c", text], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.split(), r.stderr

    def test_second_publisher_is_blocked_then_acquires_after_holder_dies(self):
        rc, out, err = self.script("""
            # holder: acquire in a background shell and keep the descriptor open
            ( publisher_acquire_dest_lock yt1 "relay:a:yt1"; echo HOLDER_RC=$?; sleep 30 ) &
            holder=$!
            sleep 1
            publisher_acquire_dest_lock yt1 "relay:b:yt1"; echo SECOND_RC=$?
            publisher_dest_held yt1 && echo HELD=yes || echo HELD=no
            echo OWNER=$(publisher_dest_owner yt1)
            kill -9 $holder; wait $holder 2>/dev/null; sleep 1
            publisher_dest_held yt1 && echo HELD_AFTER=yes || echo HELD_AFTER=no
            publisher_acquire_dest_lock yt1 "relay:b:yt1"; echo THIRD_RC=$?
        """)
        joined = " ".join(out)
        self.assertIn("HOLDER_RC=0", joined, err)
        self.assertIn("SECOND_RC=1", joined)
        self.assertIn("HELD=yes", joined)
        self.assertIn("OWNER=relay:a:yt1", joined)
        self.assertIn("HELD_AFTER=no", joined)   # kernel released the lock on kill -9
        self.assertIn("THIRD_RC=0", joined)

    def test_different_destinations_do_not_block_each_other(self):
        rc, out, err = self.script("""
            ( publisher_acquire_dest_lock yt1 "relay:a:yt1"; sleep 10 ) &
            h=$!; sleep 1
            publisher_acquire_dest_lock yt2 "relay:b:yt2"; echo RC=$?
            kill $h
        """)
        self.assertIn("RC=0", out, err)

    def test_invalid_destination_name_rejected(self):
        rc, out, err = self.script("""
            publisher_acquire_dest_lock '../evil' "relay:a:x"; echo RC=$?
            ls "$BLUESTREAM_RUN_DIR" | wc -l
        """)
        self.assertEqual(out, ["RC=2", "0"], err)


@unittest.skipUnless(BASH, "needs bash")
class TakeoverLogicTests(unittest.TestCase):
    """publisher_takeover ordering/rollback with stubbed systemd and lock state."""

    def run_script(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = PRELUDE % (posix(REPO), posix(tmp.name)) + textwrap.dedent("""
            LOG="$tmp/log"; : > "$LOG"
            STATE="$tmp/held_by"    # empty = free
            relay_load_config() { return 0; }
            relay_get_destinations() { echo yt1; }
            systemctl() {
                case "$1" in
                    is-active) echo active ;;
                    *) echo "systemctl $*" >> "$LOG" ;;
                esac
            }
            publisher_dest_held() { [ -s "$STATE" ]; }
            publisher_dest_owner() { [ -s "$STATE" ] && cat "$STATE"; }
            publisher_stop()  { echo "stop $1:$2:$3" >> "$LOG"; : > "$STATE"; }
            sleep() { :; }
        """) + textwrap.dedent(body)
        r = subprocess.run([BASH, "-c", text], capture_output=True, text=True, timeout=30)
        log = (Path(tmp.name) / "log").read_text() if (Path(tmp.name) / "log").exists() else ""
        return r.returncode, r.stdout.split(), log, r.stderr

    def test_switch_stops_old_owner_then_starts_new_and_records_previous(self):
        rc, out, log, err = self.run_script("""
            echo "relay:old:yt1" > "$STATE"
            publisher_start() { echo "start $1:$2:$3" >> "$LOG"; echo "$1:$2:$3" > "$STATE"; return 0; }
            publisher_takeover relay newone yt1; echo RC=$?
            cat "$BLUESTREAM_RUN_DIR/dest-yt1.previous"
        """)
        self.assertEqual(out, ["RC=0", "relay:old:yt1"], err)
        lines = log.splitlines()
        self.assertLess(lines.index("stop relay:old:yt1"), lines.index("start relay:newone:yt1"))

    def test_failed_takeover_reinstates_previous_owner(self):
        rc, out, log, err = self.run_script("""
            echo "relay:old:yt1" > "$STATE"
            publisher_start() {
                echo "start $1:$2:$3" >> "$LOG"
                [ "$2" = "newone" ] && return 1
                echo "$1:$2:$3" > "$STATE"; return 0
            }
            publisher_takeover relay newone yt1; echo RC=$?
            cat "$STATE"
        """)
        self.assertEqual(out, ["RC=4", "relay:old:yt1"], err)  # recoverable

    def test_nothing_is_stopped_when_destination_is_free_or_ours(self):
        rc, out, log, err = self.run_script("""
            publisher_start() { echo "start $1:$2:$3" >> "$LOG"; echo "$1:$2:$3" > "$STATE"; return 0; }
            publisher_takeover relay newone yt1; echo RC=$?
        """)
        self.assertEqual(out, ["RC=0"], err)
        self.assertNotIn("stop ", log)

    def test_invalid_names_rejected(self):
        rc, out, log, err = self.run_script("""
            publisher_takeover relay '../x' yt1; echo A=$?
            publisher_takeover relay ok '$(id)'; echo B=$?
            publisher_takeover bogus ok yt1; echo C=$?
        """)
        self.assertEqual(out, ["A=1", "B=1", "C=1"], err)


if __name__ == "__main__":
    unittest.main()
