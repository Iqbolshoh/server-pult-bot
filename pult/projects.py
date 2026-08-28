"""Which directories a job may run in."""

import glob
import os
import time

from .config import CFG
from .i18n import t
from .db import meta_get

_cache = {"at": 0.0, "paths": []}
PROJECT_CACHE_SEC = 30
def list_projects(refresh=False):
    """Directories the operator can point an engine at, de-duplicated and sorted.

    Cached briefly: this runs on every keyboard render, and each render used to
    re-glob the whole of /var/www.
    """
    if not refresh and _cache["paths"] and time.time() - _cache["at"] < PROJECT_CACHE_SEC:
        return _cache["paths"]
    found = []
    for pattern in CFG["project_globs"]:
        for path in sorted(glob.glob(pattern)):
            if os.path.isdir(path) and path not in found:
                found.append(path)
    _cache.update(at=time.time(), paths=found)
    return found
def current_project():
    return meta_get("workdir", CFG["workdir"])
def project_label(path):
    """Short name for a directory; the root of all projects gets its own word."""
    if path and path.rstrip("/") == CFG["workdir"].rstrip("/"):
        return t("projects.root")
    return os.path.basename((path or "").rstrip("/")) or path or "?"
