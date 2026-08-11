"""Hardware-binding identifier probing for credential encryption.

Terminology note (important — do not drift from this in logs/UI/docs):
the DMI values read here are **hardware-binding inputs**, not cryptographic
secrets. They are not guaranteed to be secret, unpredictable, or
high-entropy — some boards expose the exact same manufacturer placeholder
string on thousands of units. Their entire security value comes from one
fact: they are read live from ``/sys`` and are never written into any file
under ROMCloud's own persisted storage (``/userdata``). Copying that
storage therefore does not carry them along, unlike ``/etc/machine-id``
(which on Batocera is itself a symlink into ``/userdata/system``).

Priority order reflects how likely a field is to be genuine hardware
identity rather than a manufacturer boilerplate string across the Batocera
hardware matrix (Steam Deck, generic x86 PCs, laptops, mini PCs):

1. ``product_uuid``  — SMBIOS Type 1 UUID, spec-intended to be unique.
2. ``board_serial``   — motherboard serial; common on OEM hardware.
3. ``product_serial`` — chassis/system serial; common on branded machines.

Deliberately excluded: storage-device serials. Binding to the storage
device instead of the board would mean physically moving the same drive to
new hardware still decrypts fine — the opposite of the desired "new
hardware can't unlock old credentials" behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

_DMI_BASE = Path("/sys/class/dmi/id")
_MACHINE_ID_PATH = Path("/etc/machine-id")

IDENTIFIER_PRIORITY: tuple[str, ...] = ("product_uuid", "board_serial", "product_serial")

_DMI_FILENAMES = {
    "product_uuid": "product_uuid",
    "board_serial": "board_serial",
    "product_serial": "product_serial",
}

# Well-known manufacturer boilerplate that provides no real binding — seen
# across many generic/DIY/virtualized boards. Matched case-insensitively
# after whitespace normalization.
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "none",
        "n/a",
        "na",
        "not specified",
        "not applicable",
        "default string",
        "to be filled by o.e.m.",
        "to be filled by oem",
        "system serial number",
        "system manufacturer",
        "system product name",
        "unknown",
        "0123456789",
        "00000000-0000-0000-0000-000000000000",
        "03000200-0400-0500-0006-000700080009",  # common QEMU/virt default
    }
)


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def is_usable_identifier(value: Optional[str]) -> bool:
    """Reject empty/placeholder DMI values that provide no real binding."""
    if not value:
        return False
    normalized = _normalize(value)
    if normalized in _PLACEHOLDER_VALUES:
        return False
    # All-same-character strings (e.g. "00000000", "ffffffff") are another
    # common placeholder pattern not worth enumerating individually.
    stripped = normalized.replace("-", "").replace(" ", "")
    if stripped and len(set(stripped)) <= 1:
        return False
    return True


def read_dmi_identifier(name: str, *, base: Path = _DMI_BASE) -> Optional[str]:
    """Read one DMI field, or None if absent, unreadable, or a placeholder."""
    filename = _DMI_FILENAMES.get(name)
    if filename is None:
        return None
    try:
        raw = (base / filename).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return raw if is_usable_identifier(raw) else None


def probe_binding_types(
    *, reader: Optional[Callable[[str], Optional[str]]] = None, base: Path = _DMI_BASE
) -> tuple[str, ...]:
    """Return the ordered, filtered hardware-binding identifier *types*
    usable right now on this machine.

    Values are intentionally not returned here and must never be persisted
    — callers re-read them live (see :func:`gather_binding_material`) at
    both encryption and decryption time.
    """
    read = reader or (lambda name: read_dmi_identifier(name, base=base))
    return tuple(name for name in IDENTIFIER_PRIORITY if read(name))


def gather_binding_material(binding_types: tuple[str, ...], *, base: Path = _DMI_BASE) -> str:
    """Recompute the hardware-binding material for previously-recorded
    *binding_types* — held only in memory, never persisted.

    If a previously-usable identifier is no longer available or has become
    a placeholder (e.g. after a firmware/BIOS change), the material differs
    from what was used at encryption time, so key derivation — and
    therefore decryption — will fail. That is the intended, safe behavior.
    """
    parts = []
    for name in binding_types:
        value = read_dmi_identifier(name, base=base)
        if value:
            parts.append(f"{name}\x1f{value}")
    return "\x1e".join(parts)


def read_machine_id(*, path: Path = _MACHINE_ID_PATH) -> Optional[str]:
    """Read ``/etc/machine-id``.

    NOT a hardware-binding identifier: on Batocera this path is a symlink
    into ``/userdata/system/machine-id`` — the same persistent storage that
    holds ROMCloud's own credential store — so it provides no protection
    against an offline copy of that storage. Used only as the degraded-mode
    key-derivation input when no real hardware-binding identifier is usable.
    """
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None
