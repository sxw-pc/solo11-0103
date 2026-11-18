"""Minimal pytest-compatible API surface for offline stdlib-only runs."""

import importlib.util
import inspect
import re
import tempfile
import traceback
from pathlib import Path


class raises:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(
                f'{self.expected.__name__} was not raised')
        if not issubclass(exc_type, self.expected):
            return False
        if self.match is not None and not re.search(self.match, str(exc)):
            raise AssertionError(
                f'pattern {self.match!r} not found in {str(exc)!r}')
        return True


def _load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect(path):
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob('test_*.py'))
    tests = []
    for file in files:
        module = _load_module(file)
        for name, fn in vars(module).items():
            if name.startswith('test_') and callable(fn):
                tests.append((f'{file.name}::{name}', fn))
    return tests


def run(targets):
    collected = []
    for target in targets:
        collected.extend(_collect(target))
    passed = failed = 0
    failures = []
    for label, fn in collected:
        params = inspect.signature(fn).parameters
        kwargs = {}
        tmp = None
        if 'tmp_path' in params:
            tmp = tempfile.TemporaryDirectory(prefix='meterround-test-')
            kwargs['tmp_path'] = Path(tmp.name)
        try:
            fn(**kwargs)
        except Exception:  # noqa: BLE001 - test runner reports every failure
            failed += 1
            failures.append((label, traceback.format_exc()))
        else:
            passed += 1
        finally:
            if tmp is not None:
                tmp.cleanup()
    for label, tb in failures:
        print(f'FAILED {label}\n{tb}')
    print(f'{passed} passed, {failed} failed, {len(collected)} collected')
    return 1 if failed else 0
