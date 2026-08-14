from __future__ import annotations

from types import SimpleNamespace

from ports_gfx.input_capture import (
    EVIOCGRAB,
    ControllerEventDescriptor,
    ExclusiveControllerCapture,
)
from ports_gfx.savesync_conflict_popup import (
    _acquire_window_input_grab,
    _wait_for_input_release,
)


def _descriptor(fd: int, path: str, name: str) -> ControllerEventDescriptor:
    return ControllerEventDescriptor(
        fd=fd,
        path=path,
        name=name,
        device_number=fd + 100,
    )


def test_exclusive_capture_grabs_every_controller_and_releases_exactly_once():
    records = []
    ioctls = []
    closes = []
    capture = ExclusiveControllerCapture(
        lambda event, **fields: records.append((event, fields)),
        descriptors=lambda: [
            _descriptor(4, "/dev/input/event4", "Pad One"),
            _descriptor(7, "/dev/input/event7", "Pad Two"),
        ],
        duplicate=lambda fd: fd + 100,
        ioctl=lambda fd, request, value: ioctls.append((fd, request, value)),
        close=closes.append,
    )

    assert capture.acquire(2)
    assert capture.acquired
    assert capture.captured_paths == ("/dev/input/event4", "/dev/input/event7")

    capture.release(reason="resolved")
    capture.release(reason="duplicate-cleanup")

    assert ioctls == [
        (104, EVIOCGRAB, 1),
        (107, EVIOCGRAB, 1),
        (107, EVIOCGRAB, 0),
        (104, EVIOCGRAB, 0),
    ]
    assert closes == [107, 104]
    assert [event for event, _fields in records].count(
        "conflict_input_capture_released"
    ) == 1


def test_exclusive_capture_partial_failure_releases_prior_grab_and_aborts():
    ioctls = []
    closes = []

    def ioctl(fd, request, value):
        ioctls.append((fd, request, value))
        if value == 1 and fd == 107:
            raise OSError("busy")

    capture = ExclusiveControllerCapture(
        lambda *_args, **_kwargs: None,
        descriptors=lambda: [
            _descriptor(4, "/dev/input/event4", "Pad One"),
            _descriptor(7, "/dev/input/event7", "Pad Two"),
        ],
        duplicate=lambda fd: fd + 100,
        ioctl=ioctl,
        close=closes.append,
    )

    assert not capture.acquire(2)
    assert not capture.acquired
    assert (104, EVIOCGRAB, 0) in ioctls
    assert closes == [107, 104]


def test_exclusive_capture_fails_closed_when_a_controller_cannot_be_mapped():
    records = []
    capture = ExclusiveControllerCapture(
        lambda event, **fields: records.append((event, fields)),
        descriptors=lambda: [_descriptor(4, "/dev/input/event4", "Pad One")],
    )

    assert not capture.acquire(2)
    assert any(
        event == "conflict_input_capture_failed"
        and "mapped 1" in fields["error"]
        for event, fields in records
    )


def test_exclusive_capture_allows_keyboard_only_popup_without_evdev_devices():
    ioctls = []
    capture = ExclusiveControllerCapture(
        lambda *_args, **_kwargs: None,
        descriptors=lambda: [],
        ioctl=lambda fd, request, value: ioctls.append((fd, request, value)),
    )

    assert capture.acquire(0)
    capture.release(reason="keyboard-only")
    assert ioctls == []


def test_exclusive_capture_context_releases_on_exception():
    ioctls = []
    closes = []
    capture = ExclusiveControllerCapture(
        lambda *_args, **_kwargs: None,
        descriptors=lambda: [_descriptor(4, "/dev/input/event4", "Pad")],
        duplicate=lambda fd: fd + 100,
        ioctl=lambda fd, request, value: ioctls.append((fd, request, value)),
        close=closes.append,
    )

    try:
        with capture:
            assert capture.acquire(1)
            raise RuntimeError("popup failed")
    except RuntimeError:
        pass

    assert ioctls[-1] == (104, EVIOCGRAB, 0)
    assert closes == [104]


def test_window_grab_is_verified_and_keyboard_grab_is_used_when_available():
    enabled = []
    keyboard = []
    pygame = SimpleNamespace(
        event=SimpleNamespace(
            set_grab=enabled.append,
            get_grab=lambda: bool(enabled[-1]),
            set_keyboard_grab=keyboard.append,
        )
    )

    assert _acquire_window_input_grab(pygame, lambda *_args, **_kwargs: None)
    assert enabled == [True]
    assert keyboard == [True]


def test_release_barrier_retains_capture_until_actual_release_state():
    now = 0.0
    release_states = iter((False, False, True))
    records = []
    pygame = SimpleNamespace(
        QUIT=99,
        event=SimpleNamespace(get=lambda: [], pump=lambda: None),
    )
    inputs = SimpleNamespace(
        handle_event=lambda *_args, **_kwargs: None,
        all_controls_released=lambda: next(release_states),
    )

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    assert _wait_for_input_release(
        pygame,
        inputs,
        lambda event, **fields: records.append((event, fields)),
        phase="closing",
        timeout=1.0,
        clock=clock,
        sleep=sleep,
    )
    assert records[0][0] == "conflict_input_release_barrier_started"
    assert records[-1][0] == "conflict_input_all_released"
    assert now > 0


def test_release_barrier_timeout_is_bounded():
    now = 0.0
    pygame = SimpleNamespace(
        QUIT=99,
        event=SimpleNamespace(get=lambda: [], pump=lambda: None),
    )
    inputs = SimpleNamespace(
        handle_event=lambda *_args, **_kwargs: None,
        all_controls_released=lambda: False,
    )

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    assert not _wait_for_input_release(
        pygame,
        inputs,
        lambda *_args, **_kwargs: None,
        phase="closing",
        timeout=0.03,
        clock=clock,
        sleep=sleep,
    )
    assert now == 0.03
