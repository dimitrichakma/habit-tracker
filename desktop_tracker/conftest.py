"""Keep this module's tests out of the repo-root pytest run.

desktop_tracker has its own environment (pyobjc, rumps, ...), so collecting
its tests from the root venv fails on import. Collect them only when pytest's
rootdir is this folder, i.e. when run from `desktop_tracker/`.
"""

from pathlib import Path

_HERE = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    if Path(config.rootpath).resolve() != _HERE:
        return True
    return None
