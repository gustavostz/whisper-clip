"""Tests for the hotkey listener: unit tests for the recovery logic and
real end-to-end integration tests using synthetic (SendInput) key events.

The integration tests require an interactive Windows desktop session —
they inject real input and register real hotkeys. They are skipped on
non-Windows platforms and when the session cannot receive input (locked /
headless CI).
"""
import ctypes
import platform
import sys
import threading
import time
from ctypes import wintypes

import pytest

import hotkey_listener as hl
from hotkey_listener import (HotkeyListener, RespawnGovernor,
                             detect_nvidia_binding, parse_shortcut)

IS_WINDOWS = platform.system() == "Windows"

windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only")


def _interactive_session_available():
    if not IS_WINDOWS:
        return False
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    DESKTOP_READOBJECTS = 0x0001
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if hdesk:
        user32.CloseDesktop(hdesk)
        return True
    return False


interactive_only = pytest.mark.skipif(
    not _interactive_session_available(),
    reason="requires an unlocked interactive Windows session",
)


# --- Synthetic input helper ---------------------------------------

def send_combo(*vks):
    """Press the given virtual keys in order, then release in reverse.

    Scan codes are filled in via MapVirtualKeyW — the keyboard library
    matches hotkeys by scan code, so injected events must carry real ones.
    """
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    KEYEVENTF_KEYUP = 0x0002
    MAPVK_VK_TO_VSC = 0

    def _send(vk, flags):
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        inp = hl._INPUT()
        inp.type = 1
        inp.u.ki = hl._KEYBDINPUT(vk, scan, flags, 0, 0)
        assert user32.SendInput(1, ctypes.byref(inp),
                                ctypes.sizeof(hl._INPUT)) == 1, \
            f"SendInput rejected vk=0x{vk:02x} (err={ctypes.get_last_error()})"

    try:
        for vk in vks:
            _send(vk, 0)
            time.sleep(0.02)
    finally:
        for vk in reversed(vks):
            _send(vk, KEYEVENTF_KEYUP)
            time.sleep(0.02)


VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10
VK_F11, VK_F12 = 0x7A, 0x7B


# --- Unit: parse_shortcut -----------------------------------------

@windows_only
class TestParseShortcut:
    def test_alt_shift_letter(self):
        mods, vk = parse_shortcut("alt+shift+r")
        assert mods == hl._MOD_ALT | hl._MOD_SHIFT
        assert vk == ord('R')

    def test_ctrl_alias(self):
        mods, _ = parse_shortcut("control+f9")
        assert mods == hl._MOD_CONTROL

    def test_named_key(self):
        _, vk = parse_shortcut("ctrl+alt+f9")
        assert vk == 0x78

    def test_no_key_raises(self):
        with pytest.raises(ValueError):
            parse_shortcut("ctrl+shift")

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError):
            parse_shortcut("ctrl+notakey")


# --- Unit: RespawnGovernor ----------------------------------------

class TestRespawnGovernor:
    def test_allows_up_to_limit(self):
        gov = RespawnGovernor(max_events=3, window=600.0)
        t = 1000.0
        assert gov.try_acquire(t)
        assert gov.try_acquire(t + 1)
        assert gov.try_acquire(t + 2)
        assert not gov.try_acquire(t + 3)

    def test_window_frees_up(self):
        gov = RespawnGovernor(max_events=2, window=100.0)
        t = 1000.0
        assert gov.try_acquire(t)
        assert gov.try_acquire(t + 1)
        assert not gov.try_acquire(t + 2)
        # After the window slides past the first events, acquiring works again
        assert gov.try_acquire(t + 102)

    def test_never_permanently_blocked(self):
        gov = RespawnGovernor(max_events=1, window=10.0)
        t = 0.0
        for cycle in range(5):
            assert gov.try_acquire(t), f"cycle {cycle} should be allowed"
            assert not gov.try_acquire(t + 1)
            t += 11.0  # window slides


# --- Unit: NVIDIA binding detection --------------------------------

@windows_only
class TestNvidiaDetection:
    def test_parses_matching_binding(self, tmp_path, monkeypatch):
        console = tmp_path / "NVIDIA Corporation" / "NVIDIA Overlay" / "console.log"
        console.parent.mkdir(parents=True)
        console.write_text(
            "2026-07-31 09:26:18.829 INFO  HotkeyService  "
            "Hot key mapping for PMOCOverlayCycle : [ 18, 16, 82 ]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        mods, vk = parse_shortcut("alt+shift+r")
        result = detect_nvidia_binding(mods, vk)
        assert result is not None
        assert "PMOCOverlayCycle" in result

    def test_no_match_for_other_combo(self, tmp_path, monkeypatch):
        console = tmp_path / "NVIDIA Corporation" / "NVIDIA Overlay" / "console.log"
        console.parent.mkdir(parents=True)
        console.write_text(
            "Hot key mapping for PMOCOverlayCycle : [ 18, 16, 82 ]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        mods, vk = parse_shortcut("ctrl+shift+f9")
        assert detect_nvidia_binding(mods, vk) is None

    def test_missing_log_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        mods, vk = parse_shortcut("alt+shift+r")
        assert detect_nvidia_binding(mods, vk) is None


# --- Integration: real hotkey paths -------------------------------

@windows_only
@interactive_only
class TestHotkeyEndToEnd:
    def test_win32_path_receives_synthetic_press(self):
        """RegisterHotKey path: ctrl+alt+f11 (uncontested) must fire."""
        fired = threading.Event()
        listener = HotkeyListener("ctrl+alt+f11", on_trigger=fired.set)
        try:
            listener.start()
            assert listener.get_mode() == hl.HotkeyMode.WIN32, \
                "expected Win32 path for an uncontested shortcut"
            time.sleep(0.3)
            send_combo(VK_CONTROL, VK_MENU, VK_F11)
            assert fired.wait(timeout=3.0), "Win32 path did not deliver the press"
        finally:
            listener.stop()

    def test_fallback_path_receives_synthetic_press(self):
        """When another 'process' (this test) owns the hotkey, the listener
        must fall back to the subprocess hook and still deliver presses."""
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        MOD_CONTROL, MOD_ALT = 0x2, 0x1
        # Own ctrl+alt+f12 ourselves so the listener can't get it.
        owned = user32.RegisterHotKey(None, 4243, MOD_CONTROL | MOD_ALT, VK_F12)
        assert owned, f"test could not pre-own hotkey (err={ctypes.get_last_error()})"

        fired = threading.Event()
        listener = HotkeyListener("ctrl+alt+f12", on_trigger=fired.set)
        try:
            listener.start()
            assert listener.get_mode() == hl.HotkeyMode.FALLBACK, \
                "expected fallback mode while the shortcut is owned elsewhere"
            # Give the subprocess time to boot and install its hook + pass
            # its initial liveness probe.
            time.sleep(4.0)
            send_combo(VK_CONTROL, VK_MENU, VK_F12)
            assert fired.wait(timeout=3.0), "fallback path did not deliver the press"
        finally:
            listener.stop()
            user32.UnregisterHotKey(None, 4243)

    def test_fallback_subprocess_survives_probe_cycle(self):
        """The liveness probe must not kill a healthy subprocess."""
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        MOD_CONTROL, MOD_ALT = 0x2, 0x1
        owned = user32.RegisterHotKey(None, 4244, MOD_CONTROL | MOD_ALT, VK_F11)
        assert owned

        listener = HotkeyListener("ctrl+alt+f11", on_trigger=lambda: None)
        try:
            listener.start()
            assert listener.get_mode() == hl.HotkeyMode.FALLBACK
            proc = listener._subprocess
            assert proc is not None and proc.is_alive()
            pid = proc.pid
            # Request an explicit probe (the unlock/resume path) and wait.
            listener._signal_subprocess("PROBE_NOW")
            time.sleep(4.0)
            assert listener._subprocess is not None
            assert listener._subprocess.is_alive()
            assert listener._subprocess.pid == pid, \
                "healthy subprocess should not have been respawned"
            assert listener.get_mode() == hl.HotkeyMode.FALLBACK
        finally:
            listener.stop()
            user32.UnregisterHotKey(None, 4244)
