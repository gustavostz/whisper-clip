"""Tests for the visualizer's input-device caption.

Only the name-cleanup is covered: it is pure string logic, so it needs no
QApplication (importing PyQt5 is enough, instantiating a widget is not).
"""
import pytest

pytest.importorskip("PyQt5", reason="Visualizer is optional — PyQt5 not installed")

from audio_visualizer import AudioVisualizer  # noqa: E402

pretty = AudioVisualizer.pretty_device_name

# Real names from `sounddevice.query_devices()` on a Windows box
BLUETOOTH = ("Headset (@System32\\drivers\\bthhfenum.sys,#2;"
             "%1 Hands-Free%0\r\n;(Gustavo's Buds2))")


@pytest.mark.parametrize("raw,expected", [
    # Ordinary names pass through untouched
    ("Microphone (HD Pro Webcam C920)", "Microphone (HD Pro Webcam C920)"),
    ("Microphone Array (Realtek(R) Audio)", "Microphone Array (Realtek(R) Audio)"),
    ("Microsoft Sound Mapper - Input", "Microsoft Sound Mapper - Input"),
    # Bluetooth driver resource strings collapse to the friendly product name
    (BLUETOOTH, "Gustavo's Buds2"),
    ("Input (@System32\\drivers\\bthhfenum.sys,#4;%1 Hands-Free HF Audio%0"
     "\r\n;(Galaxy Tab S9 FE+))", "Galaxy Tab S9 FE+"),
    # ...even when MME truncated it before the friendly part
    ("Headset (@System32\\drivers\\bth", "Headset"),
    # MME cuts names at 31 chars; the unclosed "(" gets an ellipsis
    ("Desktop Microphone (Wireless ME", "Desktop Microphone (Wireless ME…"),
    # Nothing to show
    ("", None),
    (None, None),
    ("   ", None),
])
def test_pretty_device_name(raw, expected):
    assert pretty(raw) == expected


def test_no_control_characters_survive():
    """Embedded CRLFs would render as boxes and blow up the caption's width."""
    for ch in ("\r", "\n", "\t"):
        assert ch not in pretty(BLUETOOTH)
