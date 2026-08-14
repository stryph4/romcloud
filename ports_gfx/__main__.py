"""``python -m ports_gfx`` — CLI entry point for the graphical Ports UI.

Run under Batocera's SYSTEM Python (see ``scripts/install.sh``'s generated
``romcloud-ports`` wrapper), never ROMCloud's isolated venv. Resolves the
path to the installed ``romcloud`` CLI binary from the ``ROMCLOUD_BIN``
environment variable (set by that wrapper) or, as a fallback for manual
invocation, the first positional argument.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    conflict_mode = "--savesync-conflicts" in args
    args = [arg for arg in args if arg != "--savesync-conflicts"]
    romcloud_bin = os.environ.get("ROMCLOUD_BIN") or (args[0] if args else None)
    if not romcloud_bin:
        print(
            "error: ROMCLOUD_BIN is not set and no romcloud path was given",
            file=sys.stderr,
        )
        return 1

    if conflict_mode:
        from ports_gfx.savesync_conflict_popup import run_conflict_popup

        return run_conflict_popup(romcloud_bin)

    from ports_gfx.app import run_app

    return run_app(romcloud_bin)


if __name__ == "__main__":
    raise SystemExit(main())
