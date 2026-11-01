"""Shared test bootstrap: sys.path + heavy-dependency stubs, single-sourced.

`python -m unittest discover -s tests` runs every test module in one process, so the first
module's `sys.modules` stubs already leaked suite-wide (in alphabetical import order). This file
makes that deterministic: every test module that imports app code does `import conftest` as its
first import, which installs the union of the stubs the suite needs before any `app.*` import.

Not pytest-specific — the name just future-proofs a pytest move. A test that needs a *richer*
stub overwrites the sys.modules entry after this runs and before importing the module under test.
"""
import sys
from pathlib import Path
from types import ModuleType

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND, BACKEND / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _install(name, mod):
    sys.modules[name] = mod


def _install_import_stubs() -> None:
    httpx_module = ModuleType("httpx")
    httpx_module.AsyncClient = object
    httpx_module.HTTPError = Exception
    httpx_module.TransportError = type("TransportError", (Exception,), {})
    _install("httpx", httpx_module)

    pydantic_module = ModuleType("pydantic")
    pydantic_module.BaseModel = type("BaseModel", (), {})
    pydantic_module.Field = lambda *a, **k: k.get("default") or (
        k["default_factory"]() if "default_factory" in k else None)
    _install("pydantic", pydantic_module)


_install_import_stubs()
