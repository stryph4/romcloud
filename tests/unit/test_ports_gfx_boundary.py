"""Regression guard for the `ports_gfx` <-> `romcloud` process boundary.

``ports_gfx`` runs under Batocera's system Python; ROMCloud's actual
backend only ever runs inside the isolated venv and is reached exclusively
via ``subprocess`` + the installed ``romcloud`` CLI binary (see
``ports_gfx/client.py`` and ``ports_gfx/operation.py``). This module
statically scans every ``ports_gfx`` source file so a future change can
never accidentally reintroduce a direct ``romcloud`` import — the failure
mode this guards against is exactly the one the whole package layout
(a separate top-level tree, copied rather than pip-installed) exists to
prevent.
"""

from __future__ import annotations

from pathlib import Path

PORTS_GFX_ROOT = Path(__file__).resolve().parents[2] / "ports_gfx"
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "src" / "romcloud"
PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _source_files() -> list[Path]:
    return sorted(PORTS_GFX_ROOT.glob("**/*.py"))


class TestNoRomcloudImports:
    def test_no_source_file_imports_the_romcloud_package(self):
        assert _source_files(), "expected to find ports_gfx source files"

        offenders = []
        for path in _source_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("import romcloud", "from romcloud")):
                    offenders.append(str(path))
                    break

        assert offenders == []


class TestNoPortsGfxImports:
    def test_backend_never_imports_ports_gfx_package(self):
        offenders = []
        for path in sorted(BACKEND_ROOT.glob("**/*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("import ports_gfx", "from ports_gfx")):
                    offenders.append(str(path))
                    break

        assert offenders == []

    def test_ports_gfx_is_excluded_from_backend_package_discovery(self):
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
        assert 'where = ["src"]' in pyproject


class TestSubprocessOnlyBackendBoundary:
    def test_client_only_reaches_backend_via_subprocess(self):
        source = (PORTS_GFX_ROOT / "client.py").read_text(encoding="utf-8")
        assert "import subprocess" in source

    def test_operation_runner_only_reaches_backend_via_subprocess(self):
        source = (PORTS_GFX_ROOT / "operation.py").read_text(encoding="utf-8")
        assert "import subprocess" in source
        # No higher-level networking/RPC import should ever be needed here
        # — the whole point is a plain subprocess + stdout/stderr pipe.
        for forbidden in ("socket", "requests", "urllib", "http.client"):
            assert forbidden not in source
