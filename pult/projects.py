"""Which directories a job may run in."""

import glob
import html
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import CFG
from .db import meta_get

def list_projects():
    """Directories the user can point Claude at, de-duplicated and sorted."""
    found = []
    for pattern in CFG["project_globs"]:
        for path in sorted(glob.glob(pattern)):
            if os.path.isdir(path) and path not in found:
                found.append(path)
    return found
def current_project():
    return meta_get("workdir", CFG["workdir"])
def project_label(path):
    if path.rstrip("/") == "/var/www":
        return "hammasi"
    return os.path.basename(path.rstrip("/")) or path
