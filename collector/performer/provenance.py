"""Source identity works both from a checkout and from an installed package."""
import hashlib
import subprocess
from pathlib import Path

from . import __version__
from ._build import BUILD_COMMIT


def source_fingerprint(root):
    digest = hashlib.sha256()
    paths = []
    for directory, pattern in (("collector/performer", "*.py"), ("collector/profiles", "*.yaml"),
                               ("probes", "*.bt"), ("schema", "*.json")):
        paths.extend(path for path in (root / directory).rglob(pattern)
                     if path.name != "_build.py" and "__pycache__" not in path.parts)
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_commit(root):
    if BUILD_COMMIT:
        return BUILD_COMMIT
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=3)
        value = result.stdout.strip()
        if result.returncode == 0 and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
            return value
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def tool_versions():
    root = Path(__file__).resolve().parents[2]
    return {"performer": __version__, "performer_commit": source_commit(root),
            "performer_source_sha256": source_fingerprint(root)}
