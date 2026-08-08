"""Unit tests for `romcloud.ui.prompts` (masked password input).

Covers both the "real TTY" masked path (with `termios`/`tty` faked out —
never a real terminal in the test suite) and the non-TTY fallback path
(fully hidden input via Click), which is what actually runs under
`click.testing.CliRunner` in the rest of the test suite.
"""

from __future__ import annotations

import io

import click

from romcloud.ui import prompts


class _FakeStdin:
    """A minimal stand-in for a raw, unbuffered stdin used by `_read_masked`.

    Feeds characters one at a time from a string, and reports a fixed fd
    (irrelevant here since `termios`/`tty` are faked out too).
    """

    def __init__(self, chars: str):
        self._chars = list(chars)

    def fileno(self):
        return 0

    def read(self, n: int) -> str:
        assert n == 1
        if not self._chars:
            return ""
        return self._chars.pop(0)

    def isatty(self) -> bool:
        return True


class _FakeTermios:
    TCSADRAIN = object()

    class error(Exception):
        pass

    def tcgetattr(self, fd):
        return "old-settings"

    def tcsetattr(self, fd, when, settings):
        pass


class _FakeTty:
    def setraw(self, fd):
        pass


class TestMaskedInputSupported:
    def test_false_when_not_a_tty(self, monkeypatch):
        monkeypatch.setattr(prompts.sys.stdin, "isatty", lambda: False, raising=False)
        assert prompts.masked_input_supported() is False

    def test_false_when_termios_unavailable(self, monkeypatch):
        monkeypatch.setattr(prompts, "termios", None)
        assert prompts.masked_input_supported() is False


class TestPromptPasswordFallback:
    def test_falls_back_to_hidden_input_when_not_a_tty(self, monkeypatch):
        monkeypatch.setattr(prompts, "masked_input_supported", lambda: False)

        captured = {}

        def fake_click_prompt(label, hide_input=False):
            captured["label"] = label
            captured["hide_input"] = hide_input
            return "hunter2"

        monkeypatch.setattr(prompts.click, "prompt", fake_click_prompt)

        result = prompts.prompt_password("Password")

        assert result == "hunter2"
        assert captured["hide_input"] is True

    def test_falls_back_when_masked_read_raises(self, monkeypatch):
        monkeypatch.setattr(prompts, "masked_input_supported", lambda: True)
        monkeypatch.setattr(prompts, "termios", _FakeTermios())

        def _boom(label):
            raise OSError("no pty available")

        monkeypatch.setattr(prompts, "_read_masked", _boom)
        monkeypatch.setattr(prompts.click, "prompt", lambda label, hide_input=False: "fallback-secret")

        result = prompts.prompt_password("Password")

        assert result == "fallback-secret"

    def test_never_falls_back_to_visible_plaintext(self, monkeypatch):
        """Even in the fallback path, Click must be told to hide input."""
        monkeypatch.setattr(prompts, "masked_input_supported", lambda: False)
        seen = {}
        monkeypatch.setattr(
            prompts.click,
            "prompt",
            lambda label, hide_input=False: seen.setdefault("hide_input", hide_input) or "x",
        )

        prompts.prompt_password("Password")

        assert seen["hide_input"] is True


class TestReadMasked:
    def test_echoes_star_per_character_and_returns_password(self, monkeypatch, capsys):
        fake_stdin = _FakeStdin("hunter2\n")
        monkeypatch.setattr(prompts.sys, "stdin", fake_stdin)
        monkeypatch.setattr(prompts, "termios", _FakeTermios())
        monkeypatch.setattr(prompts, "tty", _FakeTty())

        result = prompts._read_masked("Password")

        assert result == "hunter2"
        out = capsys.readouterr().out
        assert "hunter2" not in out  # never echoes plaintext
        assert out.count("*") == len("hunter2")

    def test_backspace_removes_last_character(self, monkeypatch, capsys):
        fake_stdin = _FakeStdin("abc\x7f\n")  # 'abc' then backspace then enter -> "ab"
        monkeypatch.setattr(prompts.sys, "stdin", fake_stdin)
        monkeypatch.setattr(prompts, "termios", _FakeTermios())
        monkeypatch.setattr(prompts, "tty", _FakeTty())

        result = prompts._read_masked("Password")

        assert result == "ab"

    def test_ctrl_c_raises_keyboard_interrupt(self, monkeypatch):
        fake_stdin = _FakeStdin("ab\x03")
        monkeypatch.setattr(prompts.sys, "stdin", fake_stdin)
        monkeypatch.setattr(prompts, "termios", _FakeTermios())
        monkeypatch.setattr(prompts, "tty", _FakeTty())

        try:
            prompts._read_masked("Password")
            assert False, "expected KeyboardInterrupt"
        except KeyboardInterrupt:
            pass

    def test_restores_terminal_settings_on_exit(self, monkeypatch):
        fake_stdin = _FakeStdin("ab\n")
        monkeypatch.setattr(prompts.sys, "stdin", fake_stdin)
        fake_termios = _FakeTermios()
        restored = {}

        def fake_tcsetattr(fd, when, settings):
            restored["settings"] = settings

        fake_termios.tcsetattr = fake_tcsetattr
        monkeypatch.setattr(prompts, "termios", fake_termios)
        monkeypatch.setattr(prompts, "tty", _FakeTty())

        prompts._read_masked("Password")

        assert restored["settings"] == "old-settings"
