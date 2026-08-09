"""ports_gfx — graphical Batocera Ports UI for ROMCloud.

**Runs under Batocera's SYSTEM Python** (which already provides pygame/SDL
on real hardware — confirmed pygame 2.5.2 / SDL 2.32.8), never inside
ROMCloud's isolated venv. ROMCloud's venv deliberately never installs
pygame and never enables ``--system-site-packages``; this package is the
other half of that boundary.

Hard rule: nothing in this package may ``import romcloud`` or any
``romcloud.*`` module. The only way this package talks to ROMCloud's
backend (catalog, cache, config, SMB, etc. — all of which remain venv-only)
is through :func:`ports_gfx.client.call_backend`, which shells out to the
installed ``romcloud`` CLI binary (``romcloud uidata <action>``) and parses
a single JSON object from its stdout. That subprocess/JSON boundary is
enforced by convention here and exercised by
``tests/unit/test_ports_gfx_client.py`` — keep it that way even as this
package grows.

See ``scripts/install.sh`` for how this package is deployed: it is copied
(not pip-installed) to ``${ROMCLOUD_HOME}/ports-gfx/ports_gfx`` and run as
``<system-python> -m ports_gfx`` with that directory on ``PYTHONPATH`` —
entirely independent of ROMCloud's venv.
"""

from __future__ import annotations
