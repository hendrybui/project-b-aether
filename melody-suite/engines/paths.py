"""
Safe path utilities for the Melody Suite.
"""
import os
import uuid

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(_BASE, 'uploads')
OUTPUT_DIR = os.path.join(_BASE, 'output')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Prefix allowlist for output filenames served to clients.
_ALLOWED_PREFIXES = ('trans_', 'melody_', 'harmony_', 'sheet_', 'editor_')


def upload_path(suffix):
    return UPLOAD_DIR + os.sep + "upload_" + uuid.uuid4().hex[:8] + suffix


def output_path(prefix, suffix):
    return OUTPUT_DIR + os.sep + prefix + uuid.uuid4().hex[:8] + suffix


def output_url(filename):
    return "/output/" + os.path.basename(filename)


def save_bytes(prefix, suffix, data):
    """Write bytes to a new file in OUTPUT_DIR. Returns the download URL."""
    p = output_path(prefix, suffix)
    with open(p, 'wb') as f:
        f.write(data)
    return output_url(p)


def save_text(prefix, suffix, text):
    """Write text to a new file in OUTPUT_DIR. Returns the download URL."""
    p = output_path(prefix, suffix)
    with open(p, 'w') as f:
        f.write(text)
    return output_url(p)


def file_exists_url(url_path):
    """Check if a /output/xxx URL maps to an actual file."""
    name = url_path.replace('/output/', '')
    safe = is_safe_output_name(name)
    if safe is None:
        return False
    return os.path.isfile(OUTPUT_DIR + os.sep + safe)


def is_safe_output_name(filename):
    clean = os.path.basename(filename)
    if clean != filename or not clean:
        return None
    for prefix in _ALLOWED_PREFIXES:
        if clean.startswith(prefix):
            return clean
    return None
