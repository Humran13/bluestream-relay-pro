#!/usr/bin/env python3
"""BlueStream web-ctl JSON serialization helper (GUI-1A.1).

web-ctl is a Bash bridge over the existing BlueStream Bash libraries.  To keep
JSON valid for arbitrary string values (spaces, quotes, backslashes, newlines,
tabs, ``?a=b&c``, Unicode, ...) no JSON is ever hand-assembled with shell
interpolation.  Instead web-ctl emits a NUL-delimited key/value stream on
stdin and this helper produces exactly one JSON document on stdout:

    object mode -> {"ok": true,  "data": {key: value, ...}}
    array  mode -> {"ok": true,  "data": [{key: value, ...}, ...]}
    error  mode -> {"ok": false, "error": ..., "code": ...}

Protocol (both modes): consecutive ``key\\0value\\0`` pairs.  Values may
contain any bytes except NUL.  In array mode an empty key (``\\0\\0``) starts a
new object; nothing else is interpreted.  Input is never evaluated, and the
only output ever produced is the JSON envelope.

Exit status: 0 for a successful object/array response, 1 for an error envelope
(including internal helper failures, which still print valid JSON).

BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
"""

import json
import sys

_MODE_OBJECT = "object"
_MODE_ARRAY = "array"
_MODE_ERROR = "error"


def _read_tokens():
    """Return stdin split on NUL, dropping one trailing empty token."""
    data = sys.stdin.buffer.read()
    if not data:
        return []
    parts = data.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def _decode(value):
    return value.decode("utf-8", "replace")


def _pairs(tokens):
    """Build a dict from an even number of NUL-delimited key/value tokens."""
    if len(tokens) % 2 != 0:
        raise ValueError("malformed input: expected NUL-delimited key/value pairs")
    out = {}
    for i in range(0, len(tokens), 2):
        key = _decode(tokens[i])
        if key == "":
            raise ValueError("unexpected object separator in object mode")
        out[key] = _decode(tokens[i + 1])
    return out


def _array(tokens):
    """Build a list of dicts; an empty key (``\\0\\0``) starts a new object."""
    items = []
    current = None
    i = 0
    while i < len(tokens):
        if tokens[i] == b"":
            if current is not None:
                items.append(current)
            current = {}
            i += 1
            continue
        if current is None:
            raise ValueError("value before any object separator")
        if i + 1 >= len(tokens):
            raise ValueError("missing value for key")
        current[_decode(tokens[i])] = _decode(tokens[i + 1])
        i += 2
    if current is not None:
        items.append(current)
    return items


def _emit(doc, exit_code):
    json.dump(doc, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return exit_code


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: json_helper.py object|array|error\n")
        return _emit(
            {"ok": False, "error": "missing helper mode", "code": "JSON_HELPER_USAGE"},
            1,
        )
    mode = sys.argv[1]
    try:
        tokens = _read_tokens()
        if mode == _MODE_OBJECT:
            return _emit({"ok": True, "data": _pairs(tokens)}, 0)
        if mode == _MODE_ARRAY:
            return _emit({"ok": True, "data": _array(tokens)}, 0)
        if mode == _MODE_ERROR:
            pairs = _pairs(tokens)
            return _emit(
                {
                    "ok": False,
                    "error": pairs.get("error", "unknown error"),
                    "code": pairs.get("code", "ERROR"),
                },
                1,
            )
        raise ValueError("unknown mode: %r" % mode)
    except Exception as exc:  # noqa: BLE001 - fail closed with valid JSON
        return _emit(
            {
                "ok": False,
                "error": "json_helper: %s" % exc,
                "code": "JSON_HELPER_ERROR",
            },
            1,
        )


if __name__ == "__main__":
    sys.exit(main())
