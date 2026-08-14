"""Exclusive controller ownership for the short-lived SaveSync popup.

SDL window grabs protect keyboard/pointer routing, but Linux joystick event
devices are process-independent: EmulationStation and ROMCloud can both have
the same ``/dev/input/event*`` node open.  The popup therefore applies
``EVIOCGRAB`` to the *file description SDL already opened*.  A duplicated fd
keeps that exact description alive for deterministic release; opening and
grabbing a new event fd would also starve ROMCloud's SDL reader.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Batocera is Linux; imports stay portable
    _fcntl = None

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2
_EVDEV_TYPE = ord("E")


def _ioc(direction: int, kind: int, number: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (kind << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


EVIOCGRAB = _ioc(_IOC_WRITE, _EVDEV_TYPE, 0x90, ctypes.sizeof(ctypes.c_int))


def _system_ioctl(fd: int, request: int, value, mutate: bool = False):  # noqa: ANN001
    if _fcntl is None:
        raise OSError("Linux evdev ioctl support is unavailable")
    if isinstance(value, (bytearray, memoryview)):
        return _fcntl.ioctl(fd, request, value, mutate)
    return _fcntl.ioctl(fd, request, value)


def _eviocgbit(event_type: int, size: int) -> int:
    return _ioc(_IOC_READ, _EVDEV_TYPE, 0x20 + event_type, size)


@dataclass(frozen=True)
class ControllerEventDescriptor:
    fd: int
    path: str
    name: str
    device_number: int


@dataclass
class _CapturedDevice:
    fd: int
    path: str
    name: str
    device_number: int


def _bit_is_set(bits: bytes | bytearray, code: int) -> bool:
    byte = code // 8
    return byte < len(bits) and bool(bits[byte] & (1 << (code % 8)))


def _is_game_controller_fd(fd: int) -> bool:
    """Exclude keyboard/mouse event nodes from the SDL-fd candidates."""
    # BTN_JOYSTICK..BTN_DIGI and the modern BTN_DPAD_* range cover the
    # controller nodes SDL's Linux evdev joystick backend opens.  Merely
    # checking EV_KEY/EV_ABS would also match keyboards and mice.
    highest_code = 0x223
    bits = bytearray((highest_code // 8) + 1)
    try:
        _system_ioctl(fd, _eviocgbit(0x01, len(bits)), bits, True)  # EV_KEY
    except OSError:
        return False
    return any(_bit_is_set(bits, code) for code in range(0x120, 0x140)) or any(
        _bit_is_set(bits, code) for code in range(0x220, 0x224)
    )


def _device_name(path: str) -> str:
    event_name = Path(path).name
    try:
        return (Path("/sys/class/input") / event_name / "device" / "name").read_text(
            encoding="utf-8"
        ).strip() or event_name
    except (OSError, UnicodeError):
        return event_name


def controller_event_descriptors() -> list[ControllerEventDescriptor]:
    """Find controller event fds already owned by this process's SDL."""
    descriptors: list[ControllerEventDescriptor] = []
    seen_devices: set[int] = set()
    try:
        entries: Iterable[str] = os.listdir("/proc/self/fd")
    except OSError:
        return descriptors
    for entry in entries:
        try:
            fd = int(entry)
            path = os.readlink(f"/proc/self/fd/{entry}")
            if not path.startswith("/dev/input/event"):
                continue
            stat = os.fstat(fd)
            if stat.st_rdev in seen_devices or not _is_game_controller_fd(fd):
                continue
        except (OSError, ValueError):
            continue
        seen_devices.add(stat.st_rdev)
        descriptors.append(
            ControllerEventDescriptor(
                fd=fd,
                path=path,
                name=_device_name(path),
                device_number=stat.st_rdev,
            )
        )
    return descriptors


class ExclusiveControllerCapture:
    """Transactional, idempotently released EVIOCGRAB ownership."""

    mechanism = "linux-evdev-eviocgrab-on-sdl-fd"

    def __init__(
        self,
        record: Callable[..., None],
        *,
        descriptors: Callable[[], list[ControllerEventDescriptor]] = controller_event_descriptors,
        duplicate: Callable[[int], int] = os.dup,
        ioctl: Callable[..., object] = _system_ioctl,
        close: Callable[[int], None] = os.close,
    ) -> None:
        self._record = record
        self._descriptors = descriptors
        self._duplicate = duplicate
        self._ioctl = ioctl
        self._close = close
        self._captured: list[_CapturedDevice] = []
        self._released = False
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired and not self._released

    @property
    def captured_paths(self) -> tuple[str, ...]:
        return tuple(device.path for device in self._captured)

    def acquire(self, expected_controllers: int) -> bool:
        self._record(
            "conflict_input_capture_attempt",
            mechanism=self.mechanism,
            expected_controllers=expected_controllers,
        )
        if self._released or self._acquired:
            self._record(
                "conflict_input_capture_failed",
                mechanism=self.mechanism,
                error="capture object is not reusable",
            )
            return False
        try:
            candidates = self._descriptors()
            if len(candidates) < max(0, expected_controllers):
                raise RuntimeError(
                    f"mapped {len(candidates)} controller event device(s) for "
                    f"{expected_controllers} connected controller(s)"
                )
            for descriptor in candidates:
                duplicate_fd = self._duplicate(descriptor.fd)
                try:
                    self._ioctl(duplicate_fd, EVIOCGRAB, 1)
                except Exception:
                    self._close(duplicate_fd)
                    raise
                self._captured.append(
                    _CapturedDevice(
                        fd=duplicate_fd,
                        path=descriptor.path,
                        name=descriptor.name,
                        device_number=descriptor.device_number,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - transactional fail-closed boundary
            self._record(
                "conflict_input_capture_failed",
                mechanism=self.mechanism,
                error=str(exc),
                captured_before_failure=[device.path for device in self._captured],
            )
            self.release(reason="acquire-failed")
            return False
        self._acquired = True
        self._record(
            "conflict_input_capture_acquired",
            mechanism=self.mechanism,
            controllers=[
                {"path": device.path, "name": device.name}
                for device in self._captured
            ],
        )
        return True

    def release(self, *, reason: str) -> None:
        if self._released:
            return
        released: list[str] = []
        errors: list[str] = []
        for device in reversed(self._captured):
            try:
                self._ioctl(device.fd, EVIOCGRAB, 0)
            except Exception as exc:  # noqa: BLE001 - continue releasing every fd
                # Unplugged devices can return ENODEV; closing the duplicate
                # still drops the kernel grab/file description.
                errors.append(f"{device.path}:{exc}")
            finally:
                try:
                    self._close(device.fd)
                except Exception as exc:  # noqa: BLE001 - release remains best effort
                    errors.append(f"{device.path}:close:{exc}")
            released.append(device.path)
        self._captured.clear()
        self._released = True
        self._acquired = False
        self._record(
            "conflict_input_capture_released",
            mechanism=self.mechanism,
            reason=reason,
            controllers=released,
            errors=errors,
        )

    def __enter__(self) -> "ExclusiveControllerCapture":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.release(reason="exception" if exc_type is not None else "popup-exit")
