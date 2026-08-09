"""Regression guards for ROMCloud's backend package ownership boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "romcloud"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_does_not_import_outer_layers() -> None:
    forbidden = (
        "romcloud.infrastructure",
        "romcloud.integrations",
        "romcloud.lifecycle",
        "romcloud.services",
        "romcloud.ui",
    )
    offenders = []
    for path in sorted((SRC_ROOT / "core").glob("**/*.py")):
        if any(module.startswith(forbidden) for module in _imported_modules(path)):
            offenders.append(str(path))
    assert offenders == []


def test_normal_services_do_not_import_lifecycle() -> None:
    offenders = []
    for path in sorted((SRC_ROOT / "services").glob("**/*.py")):
        if any(module.startswith("romcloud.lifecycle") for module in _imported_modules(path)):
            offenders.append(str(path))
    assert offenders == []


def test_lifecycle_modules_use_canonical_locations() -> None:
    assert (SRC_ROOT / "lifecycle" / "setup.py").is_file()
    assert (SRC_ROOT / "lifecycle" / "install.py").is_file()
    assert (SRC_ROOT / "lifecycle" / "update.py").is_file()
    assert not (SRC_ROOT / "infrastructure" / "installer.py").exists()
    assert not (SRC_ROOT / "infrastructure" / "update.py").exists()