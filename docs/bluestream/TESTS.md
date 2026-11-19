# TESTS
- Studio core: `cd webapp && python -m pytest test_studio.py -q` -> 21 passed, 1 skipped (dev box, no tzdata).
- Existing suite: `cd webapp && python -m pytest -q` (CI python job; green on main).
- Not yet run: real FFmpeg output validation, failure injection, benchmarks (need Linux + ffmpeg), 72h soak (PENDING, needs staging VPS).
