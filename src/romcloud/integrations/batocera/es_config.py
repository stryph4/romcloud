"""EmulationStation system config overlay.

Verified command format on Batocera 42
---------------------------------------
``/usr/share/emulationstation/es_systems.cfg`` uses::

    emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% \\
        -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%

``batocera-run`` does not exist on Batocera 42.

How the ROMCloud wrapper plugs in
----------------------------------
1. The ``romcloud-run`` Python script is installed at
   ``/userdata/system/romcloud/bin/romcloud-run``.

2. The ES ``<command>`` for a system is changed to::

       /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%

   This is exactly the original command with the executable replaced.
   **All arguments and their order are preserved.**

3. ``romcloud-run`` inspects only the ``-rom`` value:

   * Not a ``.romcloud`` file → ``exec emulatorlauncher`` with original argv unchanged.
   * Is a ``.romcloud`` file → resolve/cache the real ROM, replace only the
     ``-rom`` value, ``exec emulatorlauncher`` with every other arg preserved.

4. No automatic modification of ES config is performed — the ``<command>``
   change must be made manually per system after verifying the wrapper works.

Spike tasks still open
-----------------------
- [x] **Confirmed**: ``emulatorlauncher`` selects emulator/core automatically —
      no ``-emulator`` or ``-core`` args needed (verified Batocera 42, SNES).
- [x] **Confirmed**: per-game settings are keyed by filename; the argv-passthrough
      design plus filename-preserving cache satisfies this requirement.
- [ ] Confirm ``romcloud-run`` is invoked correctly by ES with the full argv
      (quoting, spaces in paths, ordering).
- [ ] Test that controller config is preserved (``%CONTROLLERSCONFIG%`` arg
      forwarded to ``emulatorlauncher`` correctly).
- [ ] Verify ``emulatorlauncher`` blocks until the emulator exits (required for
      future save-sync post-launch hook).
- [ ] Test on other Batocera systems beyond SNES to confirm the
      ``<command>`` format is consistent across systems.
- [!] **Known limitation**: ``folder["/userdata/roms/<system>"].*`` overrides
      will not apply to cached ROMs because the cache directory path differs.
      Document this for users; no fix planned for v0.1.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.infrastructure.logging import get_logger

log = get_logger("batocera.es_config")

# SPIKE: these paths need validation
_ES_SYSTEMS_OVERRIDE = Path(
    "/userdata/system/configs/emulationstation/es_systems.cfg"
)
_WRAPPER_SCRIPT = Path("/userdata/system/romcloud/bin/romcloud-run")

_WRAPPER_CONTENT = """\
#!/usr/bin/env python3
\"\"\"romcloud-run — Batocera 42+ EmulationStation launch wrapper.

Receives the exact argv that EmulationStation would pass to emulatorlauncher.

  - Non-.romcloud ROM:  exec emulatorlauncher with original argv unchanged.
  - .romcloud proxy:    resolve/cache the real ROM, replace only the -rom
                        value, exec emulatorlauncher with all other args intact.

Example <command> for es_systems.cfg:
    /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%
\"\"\"
import sys as _sys

_APP = "/userdata/system/romcloud/app"
if _APP not in _sys.path:
    _sys.path.insert(0, _APP)

from romcloud.integrations.batocera.launcher import run_launcher_wrapper

run_launcher_wrapper(_sys.argv)
"""


def install_wrapper_script() -> None:
    """Write the romcloud-run wrapper script and make it executable."""
    _WRAPPER_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    _WRAPPER_SCRIPT.write_text(_WRAPPER_CONTENT, encoding="utf-8")
    _WRAPPER_SCRIPT.chmod(0o755)
    log.info("Wrote wrapper script: %s", _WRAPPER_SCRIPT)


def is_wrapper_installed() -> bool:
    return _WRAPPER_SCRIPT.exists()


def generate_es_systems_note() -> str:
    """Return a human-readable note about the ES configuration change needed.

    Shown to the user during setup; never applied automatically.
    """
    return (
        "ROMCloud EmulationStation integration — manual setup required.\n\n"
        "Verified on Batocera 42. 'batocera-run' does not exist.\n\n"
        "Steps:\n\n"
        "1. Confirm the wrapper script exists:\n"
        f"   {_WRAPPER_SCRIPT}\n\n"
        "2. In /userdata/system/configs/emulationstation/es_systems.cfg,\n"
        "   copy the target system entry from /usr/share/emulationstation/es_systems.cfg\n"
        "   and change only the <command> line — replacing 'emulatorlauncher' with\n"
        "   the romcloud-run wrapper while preserving every argument:\n\n"
        "   Original:\n"
        "     emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%\n"
        "       -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%\n\n"
        "   Replace with:\n"
        "     /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG%\n"
        "       -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML%\n"
        "       -systemname %SYSTEMNAME%\n\n"
        "3. Add .romcloud to the system's <extension> list.\n\n"
        "4. Restart EmulationStation.\n\n"
        "Do not apply automatically until confirmed working on real hardware."
    )
