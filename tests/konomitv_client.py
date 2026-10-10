"""Load KonomiTV's `CtrlCmdUtil` (the real EDCB client) for the tests.

Only the modules `CtrlCmdUtil` needs are loaded; the rest of KonomiTV's `app` package is stubbed.
The vendored copy in `fixtures/konomitv/` is used unless `KONOMITV_DIR` points at a KonomiTV checkout.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

VENDORED_DIR = Path(__file__).parent / "fixtures" / "konomitv"


def _edcb_dir() -> Path:
    konomitv_dir = os.environ.get("KONOMITV_DIR")
    if konomitv_dir:
        return Path(konomitv_dir) / "server" / "app" / "utils" / "edcb"
    return VENDORED_DIR


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _EDCBUtil:
    """Stub of `app.utils.edcb.EDCBUtil.EDCBUtil` with just what `CtrlCmdUtil.__init__()` calls."""

    @staticmethod
    def getEDCBHost(url):  # noqa: N802 - mirrors KonomiTV's method name
        return urlparse(str(url)).hostname

    @staticmethod
    def getEDCBPort(url):  # noqa: N802 - mirrors KonomiTV's method name
        return urlparse(str(url)).port


def load_ctrlcmdutil() -> type:
    """Return KonomiTV's `CtrlCmdUtil` class."""
    if "app.utils.edcb.CtrlCmdUtil" in sys.modules:
        return sys.modules["app.utils.edcb.CtrlCmdUtil"].CtrlCmdUtil
    edcb_dir = _edcb_dir()
    for pkg in ("app", "app.utils"):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))
    constants = types.ModuleType("app.constants")
    constants.JST = dt.timezone(dt.timedelta(hours=9))  # ty: ignore[unresolved-attribute]
    sys.modules["app.constants"] = constants
    edcb_pkg = _load("app.utils.edcb", edcb_dir / "__init__.py")
    edcb_pkg.__path__ = [str(edcb_dir)]
    edcbutil = types.ModuleType("app.utils.edcb.EDCBUtil")
    edcbutil.EDCBUtil = _EDCBUtil  # ty: ignore[unresolved-attribute]
    sys.modules["app.utils.edcb.EDCBUtil"] = edcbutil
    return _load("app.utils.edcb.CtrlCmdUtil", edcb_dir / "CtrlCmdUtil.py").CtrlCmdUtil
