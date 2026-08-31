"""Focused tests for the playlist normalization/cache engine (live-bug fix).

Regression coverage for the confirmed playlist failure where heterogeneous
media (different resolutions, H.264 profiles, frame rates, MP4 time bases,
AAC flavors, video-only files) was fed directly to FFmpeg's concat demuxer
with stream copy, producing non-monotonic DTS / decoder errors and HLS loss.

The fix routes every playlist entry through a managed content-addressed
normalization cache so the concat demuxer only ever concatenates uniform
baseline artifacts. These tests are source-level (static argv) plus
behavioral bash-harness tests with a stubbed ffmpeg: the orchestration logic
and the exact FFmpeg argv we emit are exercised; FFmpeg itself is a trusted
third-party decoder/encoder and is not present in this dev environment.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


class PlaylistNormalizationStaticTests(unittest.TestCase):
    """Source-level assertions for the normalization/cache/concat wiring."""

    def test_01_baseline_constants_declared(self):
        p = repo_text("lib/playlist.sh")
        for needle in (
            "BLUESTREAM_PLAYLIST_W=1280",
            "BLUESTREAM_PLAYLIST_H=720",
            "BLUESTREAM_PLAYLIST_FPS=25",
            "BLUESTREAM_PLAYLIST_VIDEO_PROFILE=main",
            "BLUESTREAM_PLAYLIST_AUDIO_RATE=44100",
            "BLUESTREAM_PLAYLIST_AUDIO_CHANNELS=2",
            "BLUESTREAM_PLAYLIST_VIDEO_TIMESCALE=90000",
        ):
            self.assertIn(needle, p)

    def test_02_normalize_argv_is_fixed_baseline(self):
        # The normalization command encodes to ONE conservative baseline:
        # H.264/yuv420p/1280x720/25fps + AAC-LC/44100/stereo, aspect preserved.
        p = repo_text("lib/playlist.sh")
        self.assertIn("-c:v libx264", p)
        self.assertIn("-preset \"$BLUESTREAM_PLAYLIST_VIDEO_PRESET\"", p)
        self.assertIn("-profile:v \"$BLUESTREAM_PLAYLIST_VIDEO_PROFILE\"", p)
        self.assertIn("-pix_fmt yuv420p", p)
        self.assertIn("force_original_aspect_ratio=decrease", p)
        self.assertIn("pad=${BLUESTREAM_PLAYLIST_W}:${BLUESTREAM_PLAYLIST_H}:(ow-iw)/2:(oh-ih)/2", p)
        self.assertIn("-r \"$BLUESTREAM_PLAYLIST_FPS\"", p)
        self.assertIn("-video_track_timescale \"$BLUESTREAM_PLAYLIST_VIDEO_TIMESCALE\"", p)
        self.assertIn("-c:a \"$BLUESTREAM_PLAYLIST_AUDIO_CODEC\"", p)
        self.assertIn("-ar \"$BLUESTREAM_PLAYLIST_AUDIO_RATE\"", p)
        self.assertIn("-ac \"$BLUESTREAM_PLAYLIST_AUDIO_CHANNELS\"", p)
        self.assertIn("-avoid_negative_ts make_zero", p)

    def test_03_playlist_stream_copy_unchanged(self):
        # The concat streaming argv remains stream copy to the private RTMP
        # ingest; only the INPUT is now a uniform-artifact concat file.
        p = repo_text("lib/playlist.sh")
        self.assertIn("-re -stream_loop -1 -f concat -safe 0 -i \"$concat_file\"", p)
        self.assertIn("-c:v copy -c:a copy", p)
        self.assertIn("-f flv", p)
        self.assertIn('"${BLUESTREAM_RTMP_BASE}/${BLUESTREAM_RTMP_APP_PLAYLIST}/${PLAYLIST_NAME}"', p)

    def test_04_concat_lists_only_prepared_artifacts(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn('printf "file \'%s\'\\n" "$art"', p)
        # the concat writer must never print a managed-media path directly
        self.assertNotIn('"$BLUESTREAM_MEDIA_DIR/$f" >> "$tmp"', p)
        self.assertNotIn('printf "file \'%s\'\\n" "$BLUESTREAM_MEDIA_DIR', p)

    def test_05_cache_key_is_content_hash_not_basename(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn("sha256sum \"$src\"", p)
        self.assertIn('"$BLUESTREAM_PLAYLIST_CACHE_DIR/$key.mp4"', p)
        # basenames never appear in cache paths; key is validated hex only
        self.assertIn('case "$key" in *[!0-9a-f]*|\'\') return 1 ;; esac', p)

    def test_06_atomic_publication_no_partial_artifact(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn('tmp="$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.$key.$$.mp4"', p)
        self.assertIn('mv -f "$tmp" "$art" || { rm -f "$tmp"; return 1; }', p)
        self.assertIn('[ -s "$tmp" ] || { rm -f "$tmp"; return 1; }', p)

    def test_07_video_only_items_get_silent_audio(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn("anullsrc=channel_layout=${BLUESTREAM_PLAYLIST_AUDIO_CHANNEL_LAYOUT}:sample_rate=${BLUESTREAM_PLAYLIST_AUDIO_RATE}", p)
        self.assertIn("-shortest", p)
        self.assertIn("-map 1:a:0", p)

    def test_08_cache_dir_and_prepare_timeout_in_common(self):
        c = repo_text("lib/common.sh")
        self.assertIn('BLUESTREAM_PLAYLIST_CACHE_DIR="${BLUESTREAM_VAR_DIR}/playlist-cache"', c)
        self.assertIn("BLUESTREAM_PLAYLIST_PREPARE_TIMEOUT=1800", c)

    def test_09_systemd_unit_allows_narrow_cache_write(self):
        s = repo_text("config/systemd/bluestream-playlist@.service")
        self.assertIn("ReadWritePaths=/var/lib/bluestream/run /var/lib/bluestream/playlist-cache", s)

    def test_10_installer_creates_narrow_cache_dir(self):
        inst = repo_text("install.sh")
        self.assertIn('"$BLUESTREAM_PLAYLIST_CACHE_DIR"', inst)
        self.assertIn('chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_PLAYLIST_CACHE_DIR"', inst)
        self.assertIn('chmod 0750 "$BLUESTREAM_PLAYLIST_CACHE_DIR"', inst)

    def test_11_wrapper_prepares_then_execs(self):
        w = repo_text("config/systemd/run-playlist.sh")
        self.assertIn("playlist_prepare_all", w)
        self.assertIn("playlist_write_concat_file", w)
        self.assertIn("bs_verify_ffmpeg_args", w)
        self.assertLess(w.index("playlist_prepare_all"), w.index("exec setpriv"))

    def test_12_prepare_marker_written_and_cleared(self):
        w = repo_text("config/systemd/run-playlist.sh")
        self.assertIn('"$RUN_DIR/$NAME.prepare"', w)
        self.assertIn('rm -f "$RUN_DIR/$NAME.prepare"', w)
        h = repo_text("lib/health.sh")
        self.assertIn('"$BLUESTREAM_RUN_DIR/$name.prepare"', h)
        self.assertIn("BLUESTREAM_PLAYLIST_PREPARE_TIMEOUT", h)

    def test_13_cache_prune_is_bounded_and_temp_aware(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn("playlist_cache_prune", p)
        self.assertIn("find \"$cache\" -maxdepth 1 -name '.tmp.*.mp4' -type f -mmin +60 -delete", p)
        self.assertIn('[ "${n_art:-0}" -le "${n_media:-0}" ] && return 0', p)

    def test_14_probe_captures_time_base(self):
        pr = repo_text("lib/probe.sh")
        self.assertIn("time_base", pr)
        self.assertIn("PV_TIME_BASE=", pr)
        self.assertIn("PA_TIME_BASE=", pr)

class PlaylistNormalizationBehaviorTests(unittest.TestCase):
    """Behavioral tests for the playlist preparation engine.

    Runs the REAL bash engine functions against a sandboxed dir layout with a
    stubbed ffmpeg/ffprobe. The orchestration we own (cache identity, atomic
    publication, fail-closed preparation, ordered concat, exact FFmpeg argv)
    is exercised genuinely; the third-party ffmpeg binary itself is not
    installed in this dev environment.
    """

    _HARNESS = """#!/usr/bin/env bash
set -u
repo="$(cygpath -u "$1" 2>/dev/null || printf '%s' "$1")"
scenario="$(cygpath -u "$2" 2>/dev/null || printf '%s' "$2")"
tmp="$(cygpath -u "$3" 2>/dev/null || printf '%s' "$3")"
. "$repo/lib/common.sh"
. "$repo/lib/probe.sh"
. "$repo/lib/playlist.sh"
BLUESTREAM_MEDIA_DIR="$tmp/media"
BLUESTREAM_RUN_DIR="$tmp/run"
BLUESTREAM_PLAYLIST_CACHE_DIR="$tmp/cache"
BLUESTREAM_PLAYLIST_CONF_DIR="$tmp/conf"
mkdir -p "$BLUESTREAM_MEDIA_DIR" "$BLUESTREAM_RUN_DIR" "$BLUESTREAM_PLAYLIST_CACHE_DIR" "$BLUESTREAM_PLAYLIST_CONF_DIR"
# Unit-test isolation: engine ops require root; stub it (chown is a no-op on
# non-root dev hosts).
bs_require_root() { return 0; }
chown() { return 0; }

# Controlled ffprobe values (scenarios vary these via env).
probe_media_path() {
    PROBE_OK=1
    PV_CODEC="${PROBE_VCODEC:-h264}"
    PV_WIDTH="${PROBE_WIDTH:-1280}"
    PV_HEIGHT="${PROBE_HEIGHT:-720}"
    PV_PIXFMT="${PROBE_PIXFMT:-yuv420p}"
    PV_FPS_RAW="${PROBE_FPS_RAW:-25/1}"
    PV_FPS="${PROBE_FPS:-25.00}"
    PV_PROFILE="${PROBE_PROFILE:-High}"
    PV_TIME_BASE="${PROBE_TIME_BASE:-1/90000}"
    if [ "${PROBE_AUDIO:-yes}" = "yes" ]; then
        PA_CODEC="${PROBE_ACODEC:-aac}"
        PA_SAMPLE_RATE="${PROBE_ARATE:-44100}"
        PA_CHANNELS="${PROBE_ACHANNELS:-2}"
        PA_CHANNEL_LAYOUT="${PROBE_ALAYOUT:-stereo}"
        PA_TIME_BASE="${PROBE_ATIME_BASE:-1/44100}"
    else
        PA_CODEC=""; PA_SAMPLE_RATE=""; PA_CHANNELS=""; PA_CHANNEL_LAYOUT=""; PA_TIME_BASE=""
    fi
    PF_FORMAT=mov; PF_DURATION=300; PF_BITRATE=1200000; PF_SIZE=1; PF_NB_STREAMS=2
    if [ "${PROBE_FAIL:-0}" = "1" ]; then PROBE_OK=0; return 1; fi
    return 0
}
FFMPEG_LOG="$tmp/ffmpeg.log"
mkdir -p "$tmp/bin"
cat > "$tmp/bin/ffmpeg" <<'STUB'
#!/usr/bin/env bash
{
  printf 'INVOKED\n'
  for a in "$@"; do printf 'ARG=<%s>\n' "$a"; done
} >> "$FFMPEG_LOG"
out="${@: -1}"
if [ "${FFMPEG_FAIL:-0}" = "1" ]; then
  printf 'partial' > "$out"
  exit 1
fi
prev=""; in=""
for a in "$@"; do
  if [ "$prev" = "-i" ] && [ -z "$in" ]; then in="$a"; fi
  prev="$a"
done
if [ -n "$in" ] && [ -f "$in" ]; then cp -f "$in" "$out"; else printf 'artifact' > "$out"; fi
exit 0
STUB
chmod +x "$tmp/bin/ffmpeg"
export PATH="$tmp/bin:$PATH"
export FFMPEG_LOG FFMPEG_FAIL
. "$scenario"
"""

    def _run(self, scenario_text):
        import subprocess
        import tempfile

        import webapp.engine as engine_module

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            scenario = tmpdir / "scenario.sh"
            scenario.write_text(scenario_text, encoding="utf-8")
            harness = tmpdir / "harness.sh"
            harness.write_text(self._HARNESS, encoding="utf-8")
            bash = engine_module.default_bash_path()
            proc = subprocess.run(
                [bash, str(harness), str(REPO_ROOT), str(scenario), tmp],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            return proc.stdout.decode("utf-8").strip().splitlines()

    # --- items 1, 2-6, 8: uniform preparation regardless of source shape ---

    def test_01_two_entries_produce_ordered_artifacts_and_concat(self):
        out = self._run(
            "printf 'AAA' > \"$BLUESTREAM_MEDIA_DIR/first.mp4\"\n"
            "printf 'BBB' > \"$BLUESTREAM_MEDIA_DIR/second.mp4\"\n"
            "PLAYLIST_NAME='loop'\n"
            "PLAYLIST_FILES=( 'first.mp4' 'second.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "echo ART0=$(basename \"${PLAYLIST_ARTIFACTS[0]}\")\n"
            "echo ART1=$(basename \"${PLAYLIST_ARTIFACTS[1]}\")\n"
            "playlist_write_concat_file\n"
            "echo CONCAT_RC=$?\n"
            "echo L1=$(basename \"$(sed -n \"1s/^file '\\(.*\\)'$/\\1/p\" \"$BLUESTREAM_RUN_DIR/loop.concat.txt\")\")\n"
            "echo L2=$(basename \"$(sed -n \"2s/^file '\\(.*\\)'$/\\1/p\" \"$BLUESTREAM_RUN_DIR/loop.concat.txt\")\")\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=0")
        art0 = out[1].split("=", 1)[1]
        art1 = out[2].split("=", 1)[1]
        self.assertRegex(art0, r"^[0-9a-f]{64}\.mp4$")
        self.assertRegex(art1, r"^[0-9a-f]{64}\.mp4$")
        self.assertNotEqual(art0, art1)
        self.assertEqual(out[3], "CONCAT_RC=0")
        # 1 -> 2 order preserved in the concat file
        self.assertEqual(out[4], "L1=%s" % art0)
        self.assertEqual(out[5], "L2=%s" % art1)

    def test_02_normalize_argv_uses_fixed_baseline(self):
        out = self._run(
            "printf 'X' > \"$BLUESTREAM_MEDIA_DIR/one.mp4\"\n"
            "PLAYLIST_NAME='base'\n"
            "PLAYLIST_FILES=( 'one.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "grep -q 'ARG=<-c:v>' \"$FFMPEG_LOG\" && grep -q 'ARG=<libx264>' \"$FFMPEG_LOG\" && echo X264=yes || echo X264=no\n"
            "grep -q 'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1' \"$FFMPEG_LOG\" && echo VF=yes || echo VF=no\n"
            "grep -q 'ARG=<-r>' \"$FFMPEG_LOG\" && grep -q 'ARG=<25>' \"$FFMPEG_LOG\" && echo FPS=yes || echo FPS=no\n"
            "grep -q 'ARG=<-video_track_timescale>' \"$FFMPEG_LOG\" && grep -q 'ARG=<90000>' \"$FFMPEG_LOG\" && echo TB=yes || echo TB=no\n"
            "grep -q 'ARG=<-c:a>' \"$FFMPEG_LOG\" && grep -q 'ARG=<aac>' \"$FFMPEG_LOG\" && grep -q 'ARG=<-ar>' \"$FFMPEG_LOG\" && grep -q 'ARG=<44100>' \"$FFMPEG_LOG\" && echo AAC=yes || echo AAC=no\n"
            "grep -q 'ARG=<-ac>' \"$FFMPEG_LOG\" && grep -q 'ARG=<2>' \"$FFMPEG_LOG\" && echo STEREO=yes || echo STEREO=no\n"
            "grep -q 'ARG=<-avoid_negative_ts>' \"$FFMPEG_LOG\" && grep -q 'ARG=<make_zero>' \"$FFMPEG_LOG\" && echo TSZERO=yes || echo TSZERO=no\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=0")
        for line in out[1:]:
            self.assertTrue(line.endswith("=yes"), line)

    def test_03_heterogeneous_probe_values_still_normalize_to_baseline(self):
        # Different resolution / profile / fps / video time base / audio
        # characteristics must NOT flow into the encoder: every artifact uses
        # the fixed baseline argv.
        out = self._run(
            "printf 'H1' > \"$BLUESTREAM_MEDIA_DIR/hi.mp4\"\n"
            "printf 'L1' > \"$BLUESTREAM_MEDIA_DIR/lo.mp4\"\n"
            "PROBE_WIDTH=1920; PROBE_HEIGHT=1080; PROBE_PROFILE=High; PROBE_FPS_RAW=6/1; PROBE_FPS=6.00; PROBE_TIME_BASE=1/12288\n"
            "PROBE_ACODEC=aac; PROBE_ARATE=22050; PROBE_ACHANNELS=2; PROBE_ATIME_BASE=1/22050\n"
            "PLAYLIST_NAME='het'\n"
            "PLAYLIST_FILES=( 'hi.mp4' )\n"
            "playlist_prepare_all\n"
            "echo FIRST_RC=$?\n"
            "PROBE_WIDTH=256; PROBE_HEIGHT=144; PROBE_PROFILE=Main; PROBE_FPS_RAW=6/1; PROBE_FPS=6.00; PROBE_TIME_BASE=1/90000\n"
            "PROBE_ACODEC=aac; PROBE_ARATE=44100; PROBE_ACHANNELS=2; PROBE_ATIME_BASE=1/44100\n"
            "PLAYLIST_FILES=( 'lo.mp4' )\n"
            "playlist_prepare_all\n"
            "echo SECOND_RC=$?\n"
            "echo SCALE_COUNT=$(grep -c 'scale=1280:720:force_original_aspect_ratio=decrease' \"$FFMPEG_LOG\")\n"
            "echo TB_COUNT=$(grep -c 'ARG=<90000>' \"$FFMPEG_LOG\")\n"
            "echo ARATE_COUNT=$(grep -c 'ARG=<44100>' \"$FFMPEG_LOG\")\n"
            "echo NO_SOURCE_GEOM=$(grep -c '1920x1080\\|256x144\\|12288\\|22050' \"$FFMPEG_LOG\" || true)\n"
        )
        self.assertEqual(out[0], "FIRST_RC=0")
        self.assertEqual(out[1], "SECOND_RC=0")
        self.assertEqual(out[2], "SCALE_COUNT=2")
        self.assertEqual(out[3], "TB_COUNT=2")
        self.assertEqual(out[4], "ARATE_COUNT=2")
        self.assertEqual(out[5], "NO_SOURCE_GEOM=0")

    # --- item 7: video-only media ---

    def test_04_video_only_item_gets_silent_audio(self):
        out = self._run(
            "PROBE_AUDIO=no\n"
            "printf 'V' > \"$BLUESTREAM_MEDIA_DIR/videoonly.mp4\"\n"
            "PLAYLIST_NAME='vo'\n"
            "PLAYLIST_FILES=( 'videoonly.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "grep -q 'anullsrc=channel_layout=stereo:sample_rate=44100' \"$FFMPEG_LOG\" && echo ANULLSRC=yes || echo ANULLSRC=no\n"
            "grep -q 'ARG=<-shortest>' \"$FFMPEG_LOG\" && echo SHORTEST=yes || echo SHORTEST=no\n"
            "grep -q 'ARG=<-map>' \"$FFMPEG_LOG\" && grep -q 'ARG=<1:a:0>' \"$FFMPEG_LOG\" && echo MAPSILENT=yes || echo MAPSILENT=no\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=0")
        self.assertIn("ANULLSRC=yes", out)
        self.assertIn("SHORTEST=yes", out)
        self.assertIn("MAPSILENT=yes", out)

    # --- items 11, 12: fail closed / no partial artifact ---

    def test_05_prepare_failure_fails_closed(self):
        out = self._run(
            "printf 'X' > \"$BLUESTREAM_MEDIA_DIR/bad.mp4\"\n"
            "PLAYLIST_NAME='bad'\n"
            "PLAYLIST_FILES=( 'bad.mp4' )\n"
            "FFMPEG_FAIL=1\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "echo ART_COUNT=${#PLAYLIST_ARTIFACTS[@]}\n"
            "playlist_write_concat_file\n"
            "echo CONCAT_RC=$?\n"
            "echo CONCAT_FILE=$([ -f \"$BLUESTREAM_RUN_DIR/bad.concat.txt\" ] && echo yes || echo no)\n"
            "echo TMP_LEFT=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c '\\.tmp\\.' || true)\n"
            "echo ARTIFACTS=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c '\\.mp4$' || true)\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=1")
        self.assertEqual(out[1], "ART_COUNT=0")
        self.assertEqual(out[2], "CONCAT_RC=1")
        self.assertEqual(out[3], "CONCAT_FILE=no")
        self.assertEqual(out[4], "TMP_LEFT=0")
        self.assertEqual(out[5], "ARTIFACTS=0")

    def test_06_probe_failure_fails_closed_before_ffmpeg(self):
        out = self._run(
            "printf 'X' > \"$BLUESTREAM_MEDIA_DIR/undecodable.mp4\"\n"
            "PROBE_FAIL=1\n"
            "PLAYLIST_NAME='undec'\n"
            "PLAYLIST_FILES=( 'undecodable.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "echo INVOKED=$(grep -c '^INVOKED$' \"$FFMPEG_LOG\" 2>/dev/null || echo 0)\n"
            "echo ARTIFACTS=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c '\\.mp4$' || true)\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=1")
        self.assertEqual(out[1], "INVOKED=0")
        self.assertEqual(out[2], "ARTIFACTS=0")

    # --- items 13, 14: cache reuse and invalidation ---

    def test_07_unchanged_cache_is_reused_without_reencode(self):
        out = self._run(
            "printf 'SAME-CONTENT' > \"$BLUESTREAM_MEDIA_DIR/one.mp4\"\n"
            "PLAYLIST_NAME='reuse'\n"
            "PLAYLIST_FILES=( 'one.mp4' )\n"
            ": > \"$FFMPEG_LOG\"\n"
            "playlist_prepare_all\n"
            "echo FIRST_RC=$?\n"
            "echo INVOCATIONS_1=$(grep -c '^INVOKED$' \"$FFMPEG_LOG\")\n"
            ": > \"$FFMPEG_LOG\"\n"
            "playlist_prepare_all\n"
            "echo SECOND_RC=$?\n"
            "echo INVOCATIONS_2=$(grep -c '^INVOKED$' \"$FFMPEG_LOG\")\n"
        )
        self.assertEqual(out[0], "FIRST_RC=0")
        self.assertEqual(out[1], "INVOCATIONS_1=1")
        self.assertEqual(out[2], "SECOND_RC=0")
        self.assertEqual(out[3], "INVOCATIONS_2=0")

    def test_08_changed_source_invalidates_stale_cache(self):
        out = self._run(
            "printf 'CONTENT-OLD' > \"$BLUESTREAM_MEDIA_DIR/change.mp4\"\n"
            "PLAYLIST_NAME='chg'\n"
            "PLAYLIST_FILES=( 'change.mp4' )\n"
            "playlist_prepare_all\n"
            "echo FIRST_RC=$?\n"
            "echo KEY_OLD=$(basename \"${PLAYLIST_ARTIFACTS[0]}\" .mp4)\n"
            ": > \"$FFMPEG_LOG\"\n"
            "printf 'CONTENT-NEW' > \"$BLUESTREAM_MEDIA_DIR/change.mp4\"\n"
            "playlist_prepare_all\n"
            "echo SECOND_RC=$?\n"
            "echo KEY_NEW=$(basename \"${PLAYLIST_ARTIFACTS[0]}\" .mp4)\n"
            "echo REENCODED=$(grep -c '^INVOKED$' \"$FFMPEG_LOG\")\n"
            "echo NEW_ART=$([ -f \"${PLAYLIST_ARTIFACTS[0]}\" ] && echo yes || echo no)\n"
        )
        self.assertEqual(out[0], "FIRST_RC=0")
        self.assertEqual(out[1].split("=", 1)[0], "KEY_OLD")
        key_old = out[1].split("=", 1)[1]
        self.assertEqual(out[2], "SECOND_RC=0")
        self.assertEqual(out[3].split("=", 1)[0], "KEY_NEW")
        key_new = out[3].split("=", 1)[1]
        self.assertNotEqual(key_old, key_new)
        self.assertEqual(out[4], "REENCODED=1")
        self.assertEqual(out[5], "NEW_ART=yes")

    # --- item 15: basename cannot influence the cache path ---

    def test_09_basename_does_not_influence_cache_path(self):
        out = self._run(
            "printf 'IDENTICAL-CONTENT' > \"$BLUESTREAM_MEDIA_DIR/name-a.mp4\"\n"
            "printf 'IDENTICAL-CONTENT' > \"$BLUESTREAM_MEDIA_DIR/name_b.mp4\"\n"
            "PLAYLIST_NAME='two'\n"
            "PLAYLIST_FILES=( 'name-a.mp4' 'name_b.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "echo K1=$(basename \"${PLAYLIST_ARTIFACTS[0]}\" .mp4)\n"
            "echo K2=$(basename \"${PLAYLIST_ARTIFACTS[1]}\" .mp4)\n"
            "echo SAME=$([ \"${PLAYLIST_ARTIFACTS[0]}\" = \"${PLAYLIST_ARTIFACTS[1]}\" ] && echo yes || echo no)\n"
            "echo CACHE_ENTRIES=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c '\\.mp4$' || true)\n"
            "echo NO_BASENAME=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c 'name' || true)\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=0")
        k1 = out[1].split("=", 1)[1]
        k2 = out[2].split("=", 1)[1]
        self.assertRegex(k1, r"^[0-9a-f]{64}$")
        self.assertEqual(k1, k2)
        self.assertEqual(out[3], "SAME=yes")
        self.assertEqual(out[4], "CACHE_ENTRIES=1")
        self.assertEqual(out[5], "NO_BASENAME=0")

    # --- item 16: traversal still rejected ---

    def test_10_traversal_rejected_before_any_cache_write(self):
        out = self._run(
            "PLAYLIST_NAME='trav'\n"
            "PLAYLIST_FILES=( '../evil.mp4' 'b.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREPARE_RC=$?\n"
            "echo ART_COUNT=${#PLAYLIST_ARTIFACTS[@]}\n"
            "echo CACHE_ENTRIES=$(ls -1 \"$BLUESTREAM_PLAYLIST_CACHE_DIR\" 2>/dev/null | grep -c '\\.mp4$' || true)\n"
            "echo NO_EVIL=$([ -e \"$BLUESTREAM_MEDIA_DIR/../evil.mp4\" ] && echo yes || echo no)\n"
        )
        self.assertEqual(out[0], "PREPARE_RC=1")
        self.assertEqual(out[1], "ART_COUNT=0")
        self.assertEqual(out[2], "CACHE_ENTRIES=0")
        self.assertEqual(out[3], "NO_EVIL=no")

    # --- cache boundedness / temp cleanup ---

    def test_11_prune_removes_orphans_and_stale_temps(self):
        out = self._run(
            "printf 'KEEP' > \"$BLUESTREAM_MEDIA_DIR/keep.mp4\"\n"
            "PLAYLIST_NAME='pr'\n"
            "PLAYLIST_FILES=( 'keep.mp4' )\n"
            "playlist_prepare_all\n"
            "echo PREP_RC=$?\n"
            "printf 'junk' > \"$BLUESTREAM_PLAYLIST_CACHE_DIR/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.mp4\"\n"
            "printf 'stale' > \"$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.old.999.mp4\"\n"
            "touch -d '2 hours ago' \"$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.old.999.mp4\"\n"
            "printf 'fresh' > \"$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.fresh.1.mp4\"\n"
            "playlist_cache_prune\n"
            "echo PRUNE_RC=$?\n"
            "echo KEEP_ART=$([ -f \"${PLAYLIST_ARTIFACTS[0]}\" ] && echo yes || echo no)\n"
            "echo ORPHAN=$([ -f \"$BLUESTREAM_PLAYLIST_CACHE_DIR/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.mp4\" ] && echo yes || echo no)\n"
            "echo OLD_TMP=$([ -f \"$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.old.999.mp4\" ] && echo yes || echo no)\n"
            "echo FRESH_TMP=$([ -f \"$BLUESTREAM_PLAYLIST_CACHE_DIR/.tmp.fresh.1.mp4\" ] && echo yes || echo no)\n"
        )
        self.assertEqual(out[0], "PREP_RC=0")
        self.assertEqual(out[1], "PRUNE_RC=0")
        self.assertEqual(out[2], "KEEP_ART=yes")
        self.assertEqual(out[3], "ORPHAN=no")
        self.assertEqual(out[4], "OLD_TMP=no")
        self.assertEqual(out[5], "FRESH_TMP=yes")

    # --- items 3, 9, 10: looping + final->first wrap via ffmpeg argv ---

    def test_12_whole_playlist_loops_and_stream_copy_args(self):
        out = self._run(
            "printf 'A' > \"$BLUESTREAM_MEDIA_DIR/a.mp4\"\n"
            "printf 'B' > \"$BLUESTREAM_MEDIA_DIR/b.mp4\"\n"
            "PLAYLIST_NAME='loopme'\n"
            "PLAYLIST_FILES=( 'a.mp4' 'b.mp4' )\n"
            "playlist_prepare_all\n"
            "playlist_write_concat_file\n"
            "playlist_build_ffmpeg_args\n"
            "bs_verify_ffmpeg_args\n"
            "echo VERIFY_RC=$?\n"
            "echo CONCAT_LINES=$(wc -l < \"$BLUESTREAM_RUN_DIR/loopme.concat.txt\")\n"
            "echo HAS_RE=$([ \"${FFMPEG_ARGS[4]}\" = '-re' ] && echo yes || echo no)\n"
            "echo HAS_LOOP=$([ \"${FFMPEG_ARGS[5]}\" = '-stream_loop' ] && [ \"${FFMPEG_ARGS[6]}\" = '-1' ] && echo yes || echo no)\n"
            "echo CONCAT_FMT=$([ \"${FFMPEG_ARGS[7]}\" = '-f' ] && [ \"${FFMPEG_ARGS[8]}\" = 'concat' ] && echo yes || echo no)\n"
            "echo STREAM_COPY=$([ \"${FFMPEG_ARGS[17]}\" = '-c:v' ] && [ \"${FFMPEG_ARGS[18]}\" = 'copy' ] && [ \"${FFMPEG_ARGS[19]}\" = '-c:a' ] && [ \"${FFMPEG_ARGS[20]}\" = 'copy' ] && echo yes || echo no)\n"
            "echo RTMP=${FFMPEG_ARGS[23]}\n"
        )
        self.assertEqual(out[0], "VERIFY_RC=0")
        self.assertEqual(out[1], "CONCAT_LINES=2")
        self.assertEqual(out[2], "HAS_RE=yes")
        self.assertEqual(out[3], "HAS_LOOP=yes")
        self.assertEqual(out[4], "CONCAT_FMT=yes")
        self.assertEqual(out[5], "STREAM_COPY=yes")
        self.assertEqual(out[6], "RTMP=rtmp://127.0.0.1:1935/bluestream-playlist/loopme")

    def test_13_first_and_last_entries_both_in_concat_for_wrap(self):
        # The final->first transition is the concat loop closing: the concat
        # file lists every entry once in order and -stream_loop -1 loops the
        # whole playlist, so the last entry wraps to the first.
        out = self._run(
            "printf '1' > \"$BLUESTREAM_MEDIA_DIR/item1.mp4\"\n"
            "printf '2' > \"$BLUESTREAM_MEDIA_DIR/item2.mp4\"\n"
            "printf '3' > \"$BLUESTREAM_MEDIA_DIR/item3.mp4\"\n"
            "PLAYLIST_NAME='wrap'\n"
            "PLAYLIST_FILES=( 'item1.mp4' 'item2.mp4' 'item3.mp4' )\n"
            "playlist_prepare_all\n"
            "playlist_write_concat_file\n"
            "echo FIRST_ART=$(basename \"${PLAYLIST_ARTIFACTS[0]}\")\n"
            "echo LAST_ART=$(basename \"${PLAYLIST_ARTIFACTS[2]}\")\n"
            "echo FIRST_LINE=$(basename \"$(sed -n \"1s/^file '\\(.*\\)'$/\\1/p\" \"$BLUESTREAM_RUN_DIR/wrap.concat.txt\")\")\n"
            "echo LAST_LINE=$(basename \"$(sed -n \"3s/^file '\\(.*\\)'$/\\1/p\" \"$BLUESTREAM_RUN_DIR/wrap.concat.txt\")\")\n"
        )
        first = out[0].split("=", 1)[1]
        last = out[1].split("=", 1)[1]
        self.assertEqual(out[2], "FIRST_LINE=%s" % first)
        self.assertEqual(out[3], "LAST_LINE=%s" % last)

class PlaylistPrepareHealthTests(unittest.TestCase):
    """Health classification while a playlist is preparing (normalizing).

    Runs the real lib/health.sh with a mocked systemctl and a HLS that is
    never fresh, so the playlist preparation-marker branch is exercised: an
    active playlist with a fresh prepare marker is STARTING, not STALE.
    """

    _FAKE_SYSTEMCTL = """#!/usr/bin/env bash
case "$1" in
    is-active)  printf '%s\n' '__IS_ACTIVE__' ;;
    is-enabled) printf '%s\n' '__IS_ENABLED__' ;;
    show)
        if [ "$2" = "-p" ] && [ "$3" = "Result" ]; then
            printf '%s\n' '__RESULT__'
        fi
        ;;
    *) exit 0 ;;
esac
"""

    _HARNESS = """#!/usr/bin/env bash
set -u
repo="$(cygpath -u "$1" 2>/dev/null || printf '%s' "$1")"
bin="$(cygpath -u "$2" 2>/dev/null || printf '%s' "$2")"
name="$3"
marker_age="$4"
hls_fresh="$5"
export PATH="$bin:$PATH"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
confdir="$tmp/conf"; run="$tmp/run"; hlsroot="$tmp/hls"
mkdir -p "$confdir" "$run" "$hlsroot"
: > "$confdir/$name.playlist"
. "$repo/lib/common.sh"
. "$repo/lib/health.sh"
BLUESTREAM_RELAY_CONF_DIR="$confdir"
BLUESTREAM_PLAYLIST_CONF_DIR="$confdir"
BLUESTREAM_HLS_ROOT="$hlsroot"
BLUESTREAM_RUN_DIR="$run"
if [ "$hls_fresh" = "1" ]; then
    bs_hls_is_fresh() { return 0; }
else
    bs_hls_is_fresh() { return 1; }
fi
if [ "$marker_age" != "none" ]; then
    : > "$run/$name.prepare"
    touch -d "$marker_age" "$run/$name.prepare" 2>/dev/null || true
fi
health_state playlist "$name"
printf '%s\n' "$HEALTH_STATE"
"""

    def _run(self, name, is_active, is_enabled, result, marker_age, hls_fresh="0"):
        import subprocess
        import tempfile

        import webapp.engine as engine_module

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            bindir = tmpdir / "bin"
            bindir.mkdir()
            fake = bindir / "systemctl"
            fake.write_text(
                self._FAKE_SYSTEMCTL.replace("__IS_ACTIVE__", is_active)
                .replace("__IS_ENABLED__", is_enabled)
                .replace("__RESULT__", result),
                encoding="utf-8",
            )
            fake.chmod(0o700)
            harness = tmpdir / "harness.sh"
            harness.write_text(self._HARNESS, encoding="utf-8")
            bash = engine_module.default_bash_path()
            proc = subprocess.run(
                [bash, str(harness), str(REPO_ROOT), str(bindir), name, marker_age, hls_fresh],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            return proc.stdout.decode("utf-8").strip()

    def test_01_fresh_prepare_marker_is_starting_not_stale(self):
        self.assertEqual(
            self._run("pl1", "active", "enabled", "success", "1 minute ago"), "STARTING"
        )

    def test_02_stale_prepare_marker_degrades_to_stale(self):
        self.assertEqual(
            self._run("pl1", "active", "enabled", "success", "2 hours ago"), "STALE"
        )

    def test_03_no_marker_falls_back_to_standard_starting_stale(self):
        # no marker, HLS not fresh -> standard logic (age unknown -> STALE)
        self.assertEqual(self._run("pl1", "active", "enabled", "success", "none"), "STALE")

    def test_04_fresh_hls_beats_prepare_marker(self):
        self.assertEqual(
            self._run("pl1", "active", "enabled", "success", "1 minute ago", "1"), "HEALTHY"
        )


if __name__ == "__main__":
    unittest.main()
