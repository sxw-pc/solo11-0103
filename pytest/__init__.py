"""pytest-compatible shim package; delegates to real pytest when available."""

import sys as _sys
from pathlib import Path as _Path

_PKG_DIR = _Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parent
_real = next(
    (
        _Path(p) / 'pytest'
        for p in _sys.path
        if p and _Path(p).resolve() not in (_PKG_DIR, _REPO_ROOT)
        and (_Path(p) / 'pytest' / '__init__.py').exists()
    ),
    None,
)
if _real is not None:  # pragma: no cover - exercised only with real pytest
    _sys.path[:] = [
        p for p in _sys.path
        if not p or _Path(p).resolve() not in (_PKG_DIR, _REPO_ROOT)
    ]
    import pytest as _pytest  # noqa: F401
    from pytest import raises  # noqa: F401
else:
    from ._mini import raises  # noqa: F401
