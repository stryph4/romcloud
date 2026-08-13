"""Batocera game lifecycle hook for background SaveSync."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

HOOK_PATH = Path("/userdata/system/scripts/romcloud-autosync")


def hook_content(romcloud_bin: Path) -> str:
    binary = str(romcloud_bin).replace('"', '\\"')
    return (
        "#!/bin/bash\n"
        f'ROMCLOUD_BIN="{binary}"\n'
        'case "$1" in\n'
        "  gameStart)\n"
        '    "$ROMCLOUD_BIN" _autosync game-start "$2" "$3" "$4" "$5" '
        ">/dev/null 2>&1 || true\n"
        "    ;;\n"
        "  gameStop)\n"
        '    nohup "$ROMCLOUD_BIN" _autosync game-stop "$2" "$3" "$4" "$5" '
        ">/dev/null 2>&1 </dev/null &\n"
        '    nohup "$ROMCLOUD_BIN" _autosync menu-loop '
        ">/dev/null 2>&1 </dev/null &\n"
        "    ;;\n"
        "  emulationstationStart|systemStart|frontendStart)\n"
        '    nohup "$ROMCLOUD_BIN" _autosync menu-loop '
        ">/dev/null 2>&1 </dev/null &\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )


def install_hook(
    romcloud_bin: Path, *, hook_path: Optional[Path] = None
) -> Path:
    """Atomically install the managed hook and make it executable."""
    hook_path = Path(hook_path or HOOK_PATH)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = hook_path.with_name(f".{hook_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(hook_content(romcloud_bin), encoding="utf-8")
    temporary.chmod(0o755)
    temporary.replace(hook_path)
    return hook_path
