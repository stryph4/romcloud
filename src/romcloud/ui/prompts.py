"""Masked password prompt.

Displays ``*`` for each typed character on terminals that support raw
single-character input, and transparently falls back to fully hidden input
(no echo at all, via Click's ``hide_input``) when that is not possible —
e.g. stdin is not a real TTY (piped input, most test runners), or the
platform lacks the POSIX ``termios``/``tty`` modules.

Security invariants (never relaxed):

- never display the plaintext password while typing
- never log the plaintext password anywhere
- never fall back to fully visible/echoed plaintext input — the fallback
  is always *hidden* input, never plaintext
"""

from __future__ import annotations

import sys

import click

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX platform (e.g. Windows)
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

_BACKSPACE_CHARS = ("\x7f", "\b")
_INTERRUPT_CHARS = ("\x03",)
_EOF_CHARS = ("\x04",)
_ENTER_CHARS = ("\r", "\n")


def masked_input_supported() -> bool:
    """Whether raw ``*``-masked input can be attempted on the current
    stdin/stdout — a real TTY, on a POSIX platform."""
    return termios is not None and tty is not None and _isatty()


def _isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def prompt_password(label: str = "Password") -> str:
    """Prompt for a password, echoing ``*`` per character when possible.

    Falls back to fully hidden input if masked input is not supported or
    fails for any reason. Never falls back to visible plaintext.
    """
    if masked_input_supported():
        try:
            return _read_masked(label)
        except (OSError, termios.error):  # type: ignore[union-attr]
            pass  # fall through to hidden input below

    return click.prompt(label, hide_input=True)


def _read_masked(label: str) -> str:
    click.echo(f"{label}: ", nl=False)
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if not ch or ch in _ENTER_CHARS:
                break
            if ch in _INTERRUPT_CHARS:
                raise KeyboardInterrupt
            if ch in _EOF_CHARS:
                break
            if ch in _BACKSPACE_CHARS:
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return "".join(chars)
