# TESTS

## Verified (CI, Ubuntu 24.04, run 44639794752 on branch `feature/bluestream-studio`)
- python (full webapp suite incl. real FFmpeg media tests), shell, node, browser: **all passed**.
- Real-FFmpeg tests (`webapp/test_studio_media.py`) confirm: output is 1280x720, 30 fps, H.264 yuv420p, AAC stereo,
  keyframe gaps <= 2 s; static overlays pre-render; dynamic overlays share one filter chain; silence is inserted
  for audio-less sources; DTS stays monotonic across a source loop and across a stream-copy loop; slideshow clip
  matches the profile; the copy path does not re-encode.
- Real bugs found by these runs and fixed: audio-less source rendered forever (`-shortest`), looped image
  overlay kept the render alive (`overlay shortest=1`), path-traversal gap in the path check (`..`).

## Commands
- Studio only: `cd webapp && python -m pytest test_studio.py test_studio_license.py test_studio_security.py test_studio_console.py test_studio_media.py -q`
- Everything: `cd webapp && python -m pytest -q` (what CI runs)
- Media tests need `ffmpeg`, `ffprobe`, `fonts-dejavu-core`; they skip otherwise. tz tests need tzdata.
- Dev box (Windows, no ffmpeg/tzdata): 32 passed, 8 skipped locally for the Studio files.

## NOT run (PENDING - need a real staging VPS)
- Clean install / upgrade / rollback rehearsal; reboot and `kill -9 ffmpeg` on real systemd; disk-full; destination outage with a test destination.
- CPU/RAM/speed benchmark on the 4 vCPU / 8 GB class VPS: `python3 tools/studio_soak.py --minutes 10 --out /var/tmp/studio-quick`
- 72-hour soak: `python3 tools/studio_soak.py --hours 72 --out /var/tmp/studio-soak` (gate = `72h-PASS-CANDIDATE` in verdict.json). **Not started. No 72-hour claim is made.**
- Browser-driven E2E of the Studio editor (route tests only).
