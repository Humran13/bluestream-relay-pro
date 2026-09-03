#!/usr/bin/env bash
#
# BlueStream web-ctl (GUI-1A.1) focused test.
#
# Run from the repository root:
#   bash web-ctl-selftest.sh
#
# Checks the read-only JSON bridge: the four operations, fail-closed dispatch,
# strict JSON validity (including special characters through the serializer),
# Python syntax, bash syntax, and that no existing engine files were changed.
#
# Checks that need a real installed VPS (populated relay/playlist lists with
# live systemd state and HLS health) are reported as SKIP here.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PASS=0
FAIL=0
SKIP=0

check() {  # <label> <0|1>
    if [ "$2" = "0" ]; then PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"
    else FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; fi
}
skip() { SKIP=$((SKIP + 1)); printf 'SKIP  %s\n' "$1"; }

PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# 1. bash -n on shell files (engine files included; they must stay valid)
# ---------------------------------------------------------------------------
SYNTAX_FAILED=0
for _f in web-ctl web-ctl-selftest.sh bluestream-manager status.sh install.sh uninstall.sh lib/*.sh; do
    if ! bash -n "$_f" 2>/dev/null; then
        printf 'SYNTAX  %s\n' "$_f" >&2
        SYNTAX_FAILED=1
    fi
done
if [ "$SYNTAX_FAILED" = "0" ]; then
    check "bash -n on web-ctl, selftest and engine shell files" 0
else
    check "bash -n on web-ctl, selftest and engine shell files" 1
fi
unset _f SYNTAX_FAILED

# ---------------------------------------------------------------------------
# 2. Python syntax check (ast.parse leaves no bytecode artifacts behind)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    if "$PY" -c 'import ast; ast.parse(open("webapp/json_helper.py", encoding="utf-8").read())' 2>/dev/null; then
        check "python syntax: webapp/json_helper.py" 0
    else
        check "python syntax: webapp/json_helper.py" 1
    fi
else
    skip "python3/python not found - python syntax check skipped"
fi

# ---------------------------------------------------------------------------
# Helper: read JSON on stdin and run a python assertion body ($1)
# ---------------------------------------------------------------------------
assert_json() {
    "$PY" -c "import json,sys; d=json.load(sys.stdin); $1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# 3. web-ctl version
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl version 2>/dev/null)"; _rc=$?
    if [ "$_rc" -eq 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is True; assert d["data"]["version"] == "0.1.0"'; then
        check "web-ctl version: exit 0, valid JSON, version 0.1.0" 0
    else
        check "web-ctl version: exit 0, valid JSON, version 0.1.0" 1
    fi
    unset _out _rc
else
    skip "web-ctl version (no python)"
fi

# ---------------------------------------------------------------------------
# 4. web-ctl snapshot
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl snapshot 2>/dev/null)"; _rc=$?
    if [ "$_rc" -eq 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is True; dd=d["data"]; assert all(k in dd for k in ("version","hostname","uptime","domain","https_configured","nginx_active","ffmpeg_available","ffprobe_available","ytdlp_available","relay_count","playlist_count")); assert dd["version"] == "0.1.0"'; then
        check "web-ctl snapshot: exit 0, valid JSON, all fields present" 0
    else
        check "web-ctl snapshot: exit 0, valid JSON, all fields present" 1
    fi
    unset _out _rc
else
    skip "web-ctl snapshot (no python)"
fi

# ---------------------------------------------------------------------------
# 5. web-ctl relay_list
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl relay_list 2>/dev/null)"; _rc=$?
    if [ "$_rc" -eq 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is True; assert isinstance(d["data"], list)'; then
        check "web-ctl relay_list: exit 0, valid JSON, data is array" 0
    else
        check "web-ctl relay_list: exit 0, valid JSON, data is array" 1
    fi
    case "$_out" in
        *'"data": []'*) skip "relay_list populated entries (requires VPS: real relay configs + systemd)" ;;
    esac
    unset _out _rc
else
    skip "web-ctl relay_list (no python)"
fi

# ---------------------------------------------------------------------------
# 6. web-ctl playlist_list
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl playlist_list 2>/dev/null)"; _rc=$?
    if [ "$_rc" -eq 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is True; assert isinstance(d["data"], list)'; then
        check "web-ctl playlist_list: exit 0, valid JSON, data is array" 0
    else
        check "web-ctl playlist_list: exit 0, valid JSON, data is array" 1
    fi
    case "$_out" in
        *'"data": []'*) skip "playlist_list populated entries (requires VPS: real playlist configs + systemd)" ;;
    esac
    unset _out _rc
else
    skip "web-ctl playlist_list (no python)"
fi

# ---------------------------------------------------------------------------
# 7. unknown operation rejected (fail closed)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl bogus_op 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False; assert d["code"] == "UNKNOWN_OPERATION"; assert isinstance(d["error"], str) and len(d["error"]) > 0'; then
        check "unknown operation: nonzero exit, valid failure JSON, code=UNKNOWN_OPERATION" 0
    else
        check "unknown operation: nonzero exit, valid failure JSON, code=UNKNOWN_OPERATION" 1
    fi
    unset _out _rc
else
    skip "unknown operation rejected (no python)"
fi

# ---------------------------------------------------------------------------
# 8. missing operation rejected (fail closed)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False; assert d["code"] == "MISSING_OPERATION"'; then
        check "missing operation: nonzero exit, valid failure JSON, code=MISSING_OPERATION" 0
    else
        check "missing operation: nonzero exit, valid failure JSON, code=MISSING_OPERATION" 1
    fi
    unset _out _rc
else
    skip "missing operation rejected (no python)"
fi

# ---------------------------------------------------------------------------
# 9. special-character JSON serialization through webapp/json_helper.py
#    (spaces, double/single quotes, backslash, newline, tab, ?token=a&b=c,
#     Unicode) - object, array and error modes, round-tripped via json.loads
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    if "$PY" - <<'PYEOF' 2>/dev/null
import json
import subprocess
import sys

helper = "webapp/json_helper.py"
tests = [
    ("spaces and words", "v1"),
    ('double "quotes"', "v2"),
    ("'single quotes'", "v3"),
    ("backslash \\ and slash /", "v4"),
    ("line one\nline two\nline three", "v5"),
    ("tab\there\tand\tthere", "v6"),
    ("?token=a&b=c&q=weird", "v7"),
    ("Unicode héllo — 世界 🎥", "v8"),
]


def run(mode, payload):
    return subprocess.run(
        [sys.executable, helper, mode],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


# object mode
stream = b"".join(b"%s\0%s\0" % (k.encode(), v.encode()) for k, v in tests)
p = run("object", stream)
assert p.returncode == 0, p.stderr
doc = json.loads(p.stdout.decode("utf-8"))
assert doc["ok"] is True
for k, v in tests:
    assert doc["data"][k] == v, (k, repr(doc["data"][k]))

# array mode: one object holding every special value
stream = b"\0" + b"".join(b"%s\0%s\0" % (k.encode(), v.encode()) for k, v in tests)
p = run("array", stream)
assert p.returncode == 0, p.stderr
doc = json.loads(p.stdout.decode("utf-8"))
assert len(doc["data"]) == 1
for k, v in tests:
    assert doc["data"][0][k] == v, (k, repr(doc["data"][0][k]))

# error mode carries special characters too
stream = b"error\0boom: \"q\" \\ 's'\n\ttab ?token=a&b=c\0code\0SPECIAL\0"
p = run("error", stream)
assert p.returncode == 1
doc = json.loads(p.stdout.decode("utf-8"))
assert doc["ok"] is False and doc["code"] == "SPECIAL"
assert "q" in doc["error"]

print("SPECIAL_OK")
PYEOF
    then
        check "special-char JSON serialization (object/array/error modes)" 0
    else
        check "special-char JSON serialization (object/array/error modes)" 1
    fi
else
    skip "special-char JSON serialization (no python)"
fi

# ---------------------------------------------------------------------------
# 10. GUI-1B.1 lifecycle bridge validation (fail-closed only: these inputs
#     never reach a real relay/playlist or systemd)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    for _op in relay_start relay_stop relay_restart playlist_start playlist_stop playlist_restart; do
        _out="$(bash web-ctl "$_op" 2>/dev/null)"; _rc=$?
        if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_TARGET"'; then :; else _ok=0; fi
        unset _out _rc
    done
    if [ "$_ok" -eq 1 ]; then
        check "lifecycle bridge: missing target rejected for all six ops" 0
    else
        check "lifecycle bridge: missing target rejected for all six ops" 1
    fi
    unset _ok
else
    skip "lifecycle bridge: missing target (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    for _bad in '--help' '../evil' 'x;rm -rf /' 'a b'; do
        _out="$(bash web-ctl relay_start "$_bad" 2>/dev/null)"; _rc=$?
        if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
        unset _out _rc
    done
    if [ "$_ok" -eq 1 ]; then
        check "lifecycle bridge: invalid target rejected (option/traversal/meta/space)" 0
    else
        check "lifecycle bridge: invalid target rejected (option/traversal/meta/space)" 1
    fi
    unset _ok
else
    skip "lifecycle bridge: invalid names (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_start goodname extra 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_start goodname "" 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_start goodname "" extra4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_start goodname a b c 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "lifecycle bridge: exact argv count enforced (extra/empty/4th/many)" 0
    else
        check "lifecycle bridge: exact argv count enforced (extra/empty/4th/many)" 1
    fi
    unset _ok
else
    skip "lifecycle bridge: exact argv count (no python)"
fi

if [ -n "$PY" ]; then
    _out="$(bash web-ctl playlist_bogus 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "UNKNOWN_OPERATION"'; then
        check "lifecycle bridge: unknown operation rejected" 0
    else
        check "lifecycle bridge: unknown operation rejected" 1
    fi
    unset _out _rc
else
    skip "lifecycle bridge: unknown operation (no python)"
fi

# ---------------------------------------------------------------------------
# 10.5 GUI-1C.1 create/import bridge validation (fail-closed only: every case
#      below stops at name/URL/media validation - nothing is ever written)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _out="$(bash web-ctl media_list 2>/dev/null)"; _rc=$?
    if [ "$_rc" -eq 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is True; assert isinstance(d["data"], list)'; then
        check "media bridge: media_list exit 0, valid JSON, data is array" 0
    else
        check "media bridge: media_list exit 0, valid JSON, data is array" 1
    fi
    unset _out _rc
else
    skip "media bridge: media_list (no python)"
fi

if [ -n "$PY" ]; then
    _out="$(bash web-ctl media_list extra 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then
        check "media bridge: media_list rejects extra arguments" 0
    else
        check "media bridge: media_list rejects extra arguments" 1
    fi
    unset _out _rc
else
    skip "media bridge: media_list extra args (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_create_url 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_create_url goodname 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_create_url goodname 'https://x/y.m3u8' extra 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "create bridge: relay_create_url exact argc (missing/extra) fail closed" 0
    else
        check "create bridge: relay_create_url exact argc (missing/extra) fail closed" 1
    fi
    unset _ok
else
    skip "create bridge: relay_create_url argc (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_create_url --help https://x/y.m3u8 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_create_url goodname 'file:///etc/passwd' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_URL"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_create_url goodname 'https://x/a b.m3u8' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_URL"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "create bridge: relay_create_url invalid name/URL rejected" 0
    else
        check "create bridge: relay_create_url invalid name/URL rejected" 1
    fi
    unset _ok
else
    skip "create bridge: relay_create_url validation (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_create_media goodname '../evil.mp4' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_MEDIA"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_create_media --help 'a.mp4' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "create bridge: relay_create_media invalid media/name rejected" 0
    else
        check "create bridge: relay_create_media invalid media/name rejected" 1
    fi
    unset _ok
else
    skip "create bridge: relay_create_media validation (no python)"
fi

if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl media_import_staged 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_TARGET"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl media_import_staged '../evil.mp4' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_STAGING"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl media_import_staged 'a b.mp4' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_STAGING"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl media_import_staged ok.mp4 extra 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "import bridge: media_import_staged fail-closed validation" 0
    else
        check "import bridge: media_import_staged fail-closed validation" 1
    fi
    unset _ok
else
    skip "import bridge: media_import_staged (no python)"
fi

# ---------------------------------------------------------------------------
# 10.6 GUI-1D.1 playlist_create bridge validation (fail-closed only: every
#      case below stops at dispatch/name/media validation - nothing is ever
#      written)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl playlist_create 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create goodname 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create goodname a.mp4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_FEW_ITEMS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create --help a.mp4 b.mp4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create goodname '../evil.mp4' b.mp4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_MEDIA"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create goodname 'C:\\x.mp4' b.mp4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_MEDIA"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_create goodname a.mp4 a.mp4 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "DUPLICATE_ITEM"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "playlist_create bridge: fail-closed validation (missing/too-few/name/media/duplicate)" 0
    else
        check "playlist_create bridge: fail-closed validation (missing/too-few/name/media/duplicate)" 1
    fi
    unset _ok
else
    skip "playlist_create bridge: fail-closed validation (no python)"
fi

# ---------------------------------------------------------------------------
# 10.7 GUI-4 Phase 1B destination-attachment bridge validation (fail-closed
#      only: every case below stops at dispatch/name/list validation - no
#      config file is ever written, and no systemd unit is ever touched)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    # get: exact argc == 2
    _out="$(bash web-ctl relay_destinations_get 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_TARGET"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_destinations_get a b 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    # get: invalid target name rejected before any filesystem access
    _out="$(bash web-ctl relay_destinations_get ../evil 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    # set: exact argc == 3 (the list value must be present, even if empty)
    _out="$(bash web-ctl relay_destinations_set news 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_destinations_set news a b 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    # set: invalid target name rejected
    _out="$(bash web-ctl playlist_destinations_set --help '' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "attachment bridge: destinations get/set fail-closed validation" 0
    else
        check "attachment bridge: destinations get/set fail-closed validation" 1
    fi
    unset _ok
else
    skip "attachment bridge: destinations get/set validation (no python)"
fi

# ---------------------------------------------------------------------------
# 10.8 GUI-5A safe-delete bridge validation (fail-closed only: every case
#      below stops at dispatch/name validation - nothing is ever deleted)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_delete 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_TARGET"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_delete a b 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_delete ../evil 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl media_delete '../etc/passwd' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_MEDIA"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl media_delete 'sub/file.mp4' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_MEDIA"'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "safe-delete bridge: relay/playlist/media delete fail-closed validation" 0
    else
        check "safe-delete bridge: relay/playlist/media delete fail-closed validation" 1
    fi
    unset _ok
else
    skip "safe-delete bridge: delete validation (no python)"
fi

# ---------------------------------------------------------------------------
# 10.9 GUI-6A relay_set_source bridge validation (fail-closed only: every case
#      below stops at dispatch/name/URL validation - no config is ever written)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl relay_set_source news 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_set_source news 'https://x/y.m3u8' extra 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_set_source --help 'https://x/y.m3u8' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_set_source news 'file:///etc/passwd' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] in ("INVALID_URL","NOT_FOUND")'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl relay_set_source news 'https://x/`id`.m3u8' 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] in ("INVALID_URL","NOT_FOUND")'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "edit-source bridge: relay_set_source fail-closed validation" 0
    else
        check "edit-source bridge: relay_set_source fail-closed validation" 1
    fi
    unset _ok
else
    skip "edit-source bridge: relay_set_source validation (no python)"
fi

# ---------------------------------------------------------------------------
# 10.10 GUI-7A public YouTube Live classification (engine-side, no network):
#       a YouTube page URL classifies as its own TYPE, look-alike hosts do
#       not, and the resolver in lib/youtube.sh uses a fixed argv (no eval).
# ---------------------------------------------------------------------------
_ok=1
( . "$SCRIPT_DIR/lib/common.sh" 2>/dev/null
  bs_is_youtube_url "https://www.youtube.com/watch?v=abc" || exit 1
  bs_is_youtube_url "https://youtu.be/abc"                || exit 1
  bs_is_youtube_url "https://youtube.com.evil.com/x"      && exit 1
  bs_is_youtube_url "http://www.youtube.com/watch?v=x"    && exit 1
  [ "$(bs_classify_source_type 'https://www.youtube.com/live/x')" = "youtube" ] || exit 1
  [ "$(bs_classify_source_type 'https://cdn.example/a.m3u8')" = "remote-hls" ] || exit 1
  exit 0 ) || _ok=0
# The resolver must never build a shell string / use eval / read cookies.
# Inspect executable lines only (comments describe the safety rules verbatim).
_yt_code="$(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/lib/youtube.sh")"
printf '%s' "$_yt_code" | grep -q 'eval ' && _ok=0
printf '%s' "$_yt_code" | grep -qE 'bash -c|sh -c' && _ok=0
printf '%s' "$_yt_code" | grep -q -- '--no-cookies' || _ok=0
printf '%s' "$_yt_code" | grep -q -- '--ignore-config' || _ok=0
printf '%s' "$_yt_code" | grep -q 'local -a cmd=(' || _ok=0
unset _yt_code
if [ "$_ok" -eq 1 ]; then
    check "youtube: classification + resolver is fixed-argv, no-cookies, no eval" 0
else
    check "youtube: classification + resolver is fixed-argv, no-cookies, no eval" 1
fi
unset _ok

# ---------------------------------------------------------------------------
# 10.11 GUI-8A one-time playlist schedule bridge validation (fail-closed only:
#       every case below stops at dispatch/name/timestamp validation - no
#       sidecar or timer unit is ever written, no systemd unit is ever touched)
# ---------------------------------------------------------------------------
if [ -n "$PY" ]; then
    _ok=1
    _out="$(bash web-ctl playlist_schedule_get 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_TARGET"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_get a b 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_set promo 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "MISSING_ARGUMENT"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_set promo 4000000000 x y 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "TOO_MANY_ARGUMENTS"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_set ../evil 4000000000 2099-01-01T00:00:00Z 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] == "INVALID_NAME"'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_set promo not-a-number 2099-01-01T00:00:00Z 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] in ("INVALID_SCHEDULE","NOT_FOUND")'; then :; else _ok=0; fi
    unset _out _rc
    _out="$(bash web-ctl playlist_schedule_set promo 4000000000 "2099 \$(id)" 2>/dev/null)"; _rc=$?
    if [ "$_rc" -ne 0 ] && printf '%s' "$_out" | assert_json 'assert d["ok"] is False and d["code"] in ("INVALID_SCHEDULE","NOT_FOUND")'; then :; else _ok=0; fi
    unset _out _rc
    if [ "$_ok" -eq 1 ]; then
        check "schedule bridge: playlist_schedule get/set/clear fail-closed validation" 0
    else
        check "schedule bridge: playlist_schedule get/set/clear fail-closed validation" 1
    fi
    unset _ok
else
    skip "schedule bridge: playlist_schedule validation (no python)"
fi

# GUI-8A missed-time safety: the generated timer must be Persistent=false (a
# start missed while the box was offline is never run late), the engine must
# expose a pending/missed state, and the start wrapper must be able to skip a
# very-late fire without calling playlist_start.
_ok=1
grep -q "printf 'Persistent=false" "$SCRIPT_DIR/lib/schedule.sh" || _ok=0
grep -q "Persistent=true" "$SCRIPT_DIR/lib/schedule.sh" && _ok=0
grep -q 'schedule_state()' "$SCRIPT_DIR/lib/schedule.sh" || _ok=0
grep -q 'schedule_expire_timer()' "$SCRIPT_DIR/lib/schedule.sh" || _ok=0
grep -q 'BLUESTREAM_SCHEDULE_GRACE' "$SCRIPT_DIR/config/systemd/run-playlist-start.sh" || _ok=0
grep -q 'schedule_expire_timer "\$NAME"' "$SCRIPT_DIR/config/systemd/run-playlist-start.sh" || _ok=0
if [ "$_ok" -eq 1 ]; then
    check "schedule: timer Persistent=false + pending/missed state + very-late-fire guard" 0
else
    check "schedule: timer Persistent=false + pending/missed state + very-late-fire guard" 1
fi
unset _ok

# ---------------------------------------------------------------------------
# 11. no existing engine files changed unexpectedly
# ---------------------------------------------------------------------------
if [ -d .git ]; then
    _modified="$(git diff --name-only 2>/dev/null || true)"
    if [ -z "$_modified" ]; then
        check "no tracked engine files modified" 0
    else
        check "no tracked engine files modified" 1
        printf '%s\n' "$_modified" >&2
    fi
    unset _modified
else
    skip "no .git - tracked-file check skipped"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\nweb-ctl selftest: %s passed, %s failed, %s skipped\n' "$PASS" "$FAIL" "$SKIP"
printf 'NOTE: relay_list/playlist_list entry contents (live systemd state, HLS health)\n'
printf '      and snapshot live values (nginx/ffmpeg/ffprobe) require the installed VPS.\n'

[ "$FAIL" -eq 0 ]
