"""Transparent pytest entry point.

If a real pytest is installed anywhere on the system, delegate to it unchanged.
Otherwise fall back to a tiny stdlib runner that implements the small subset of
the pytest API used by this repository (``raises`` and the ``tmp_path``
fixture), so the suite stays runnable in offline stdlib-only environments.
"""

import runpy
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parent


def _real_pytest_dir():
    for base in sys.path:
        if not base:
            continue
        root = Path(base).resolve()
        if root in (_PKG_DIR, _REPO_ROOT):
            continue  # never delegate to ourselves
        candidate = root / 'pytest'
        if (candidate / '__main__.py').exists() and (candidate / '__init__.py').exists():
            return candidate
    return None


def _targets(argv):
    flags_with_values = {'-p', '-k', '-m', '--ignore', '--rootdir', '--junitxml'}
    positional = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in flags_with_values:
            skip_next = True
            continue
        if token.startswith('-'):
            continue
        positional.append(token)
    cwd = Path.cwd()
    paths = [Path(a) if Path(a).is_absolute() else cwd / a for a in positional]
    return paths or [cwd / 'tests']


def main():
    real = _real_pytest_dir()
    if real is not None:
        sys.path[:] = [p for p in sys.path if not (
            p and Path(p).resolve() in (_PKG_DIR, _REPO_ROOT))]
        sys.argv[0] = str(real / '__main__.py')
        runpy.run_module('pytest', run_name='__main__', alter_sys=True)
        return
    from ._mini import run

    sys.exit(run(_targets(sys.argv[1:])))


if __name__ == '__main__':
    main()
