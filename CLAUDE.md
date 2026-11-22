# BlueStream Relay Pro / Studio - project instructions

- FIRST read `docs/bluestream/STATUS.md` and `TASKS.md`; run `git status`. On `proceed`: resume the next unfinished task, no re-planning, no routine questions.
- Existing Free Relay/Playlist/Publisher behaviour must stay backwards compatible. Studio is opt-in, isolated (`webapp/studio/`), feature-flagged.
- Engine = bash libs (`lib/*.sh`) + systemd units + Flask console calling an allowlisted `web-ctl`. Add new ops to the allowlist + validate args in `webapp/engine.py`.
- Never: deploy to production, use real stream keys, touch firewall/CloudPanel/nginx on the VPS, print secrets, shell-concatenate user input (argv lists only).
- Run targeted tests (`cd webapp && python -m pytest test_studio.py -q`); full suite only before a PR. CI: python, shell, node, browser jobs on ubuntu-24.04.
- Never claim a gate passed without evidence (72h test, 4-vCPU benchmark, payments are PENDING until truly done). Dev box has no ffmpeg/Docker/tzdata: media tests there are UNTESTED, not passed.
- Branch: `feature/bluestream-studio`. Small commits; update STATUS.md after each milestone.
