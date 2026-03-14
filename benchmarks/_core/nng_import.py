"""Safe importer for the compiled ``nng`` package.

When the benchmark suite is run from the workspace root, Python's ``sys.path``
starts with ``'.'``, which makes ``import nng`` resolve to the *source*
``nng/`` directory.  That directory has no compiled ``_nng`` extension, so the
import fails with ``ModuleNotFoundError: No module named 'nng._nng'``.

Calling :func:`import_nng` removes the workspace root from ``sys.path`` for
the duration of the import, ensuring the *installed* package (in site-packages
or the editable-install build tree) is found instead.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

# Absolute path to the workspace root (…/python-nng).
# benchmarks/_core/nng_import.py → benchmarks/_core → benchmarks → workspace root
_WORKSPACE_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def import_nng() -> ModuleType:
    """Import and return the compiled ``nng`` package, bypassing the source dir.

    Idempotent — if ``nng`` is already in ``sys.modules`` the cached module is
    returned immediately.
    """
    if "nng" in sys.modules:
        return sys.modules["nng"]

    # Build a filtered path that excludes the workspace root so Python skips the
    # unbuilt source nng/ directory and finds the installed extension instead.
    filtered = [p for p in sys.path if Path(p).resolve() != Path(_WORKSPACE_ROOT)]

    original_path = sys.path[:]
    sys.path[:] = filtered
    try:
        import nng as _nng  # noqa: PLC0415
        return _nng
    finally:
        sys.path[:] = original_path
