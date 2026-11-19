"""Project persistence: separate DRAFT and PUBLISHED revisions, bounded history.

Layout under the studio dir (default /var/lib/bluestream/studio):
    <name>.json            published (what a running stream uses)
    <name>.draft.json      editor working copy
    <name>.rev/<n>.json    last KEEP_REVISIONS published snapshots (rollback)
Writes are atomic (temp file + os.replace); names are validated, so no path
traversal is possible.
"""
import json
import os
import re
import tempfile

from .model import NAME_RE, OutputProfile, Overlay, Project, ValidationError

DEFAULT_DIR = "/var/lib/bluestream/studio"
KEEP_REVISIONS = 10
_FIELDS = set(Overlay.__dataclass_fields__)
_PFIELDS = set(OutputProfile.__dataclass_fields__)


def studio_dir():
    return os.environ.get("BLUESTREAM_STUDIO_DIR", DEFAULT_DIR)


def to_dict(project):
    return {
        "name": project.name, "source_kind": project.source_kind,
        "source_ref": project.source_ref, "revision": project.revision,
        "slide_seconds": project.slide_seconds, "slide_shuffle": project.slide_shuffle,
        "profile": dict(project.profile.__dict__),
        "overlays": [dict(o.__dict__) for o in project.overlays],
    }


def from_dict(doc):
    if not isinstance(doc, dict):
        raise ValidationError("project must be an object")
    try:
        prof = {k: v for k, v in (doc.get("profile") or {}).items() if k in _PFIELDS}
        overlays = []
        for o in doc.get("overlays") or []:
            if not isinstance(o, dict):
                raise ValidationError("overlay must be an object")
            overlays.append(Overlay(**{k: v for k, v in o.items() if k in _FIELDS}))
        p = Project(name=doc["name"], source_kind=doc["source_kind"],
                    source_ref=doc["source_ref"], overlays=overlays,
                    profile=OutputProfile(**prof), revision=int(doc.get("revision", 1)),
                    slide_seconds=int(doc.get("slide_seconds", 10)),
                    slide_shuffle=bool(doc.get("slide_shuffle", False)))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("malformed project: %s" % type(exc).__name__)
    return p.validate()


def _path(name, suffix):
    if not NAME_RE.match(name or ""):
        raise ValidationError("invalid project name")
    return os.path.join(studio_dir(), name + suffix)


def _atomic_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".w")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return from_dict(json.load(fh))


def exists(name):
    return os.path.exists(_path(name, ".json")) or os.path.exists(_path(name, ".draft.json"))


def save_draft(project):
    project.validate()
    _atomic_json(_path(project.name, ".draft.json"), to_dict(project))


def load_draft(name):
    path = _path(name, ".draft.json")
    return _read(path if os.path.exists(path) else _path(name, ".json"))


def load_published(name):
    return _read(_path(name, ".json"))


def has_unpublished_changes(name):
    d, p = _path(name, ".draft.json"), _path(name, ".json")
    if not os.path.exists(d):
        return False
    if not os.path.exists(p):
        return True
    a, b = to_dict(_read(d)), to_dict(_read(p))
    a.pop("revision"), b.pop("revision")
    return a != b


def publish(name):
    """Promote the draft to the published revision (what a restart will use)."""
    draft = load_draft(name)
    try:
        draft.revision = load_published(name).revision + 1
    except (OSError, ValueError):
        draft.revision = 1
    doc = to_dict(draft)
    _atomic_json(_path(name, ".json"), doc)
    revdir = _path(name, ".rev")
    _atomic_json(os.path.join(revdir, "%d.json" % draft.revision), doc)
    for old in sorted((int(f[:-5]) for f in os.listdir(revdir) if f[:-5].isdigit()))[:-KEEP_REVISIONS]:
        try:
            os.unlink(os.path.join(revdir, "%d.json" % old))
        except OSError:
            pass
    return draft.revision


def revisions(name):
    revdir = _path(name, ".rev")
    if not os.path.isdir(revdir):
        return []
    return sorted(int(f[:-5]) for f in os.listdir(revdir) if f[:-5].isdigit())


def rollback(name, revision):
    """Restore a previous published snapshot into the DRAFT (then publish to apply)."""
    if revision not in revisions(name):
        raise ValidationError("unknown revision")
    p = _read(os.path.join(_path(name, ".rev"), "%d.json" % revision))
    save_draft(p)
    return p


def delete(name):
    import shutil
    for suffix in (".json", ".draft.json"):
        try:
            os.unlink(_path(name, suffix))
        except OSError:
            pass
    shutil.rmtree(_path(name, ".rev"), ignore_errors=True)


def list_projects():
    d = studio_dir()
    if not os.path.isdir(d):
        return []
    names = set()
    for f in os.listdir(d):
        base = f[:-11] if f.endswith(".draft.json") else (f[:-5] if f.endswith(".json") else "")
        if base and NAME_RE.match(base):
            names.add(base)
    return sorted(names)
