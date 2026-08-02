"""Move our own window to the user's current virtual desktop (Windows).

Uses only the DOCUMENTED shell COM interface IVirtualDesktopManager
(stable since Windows 10 RTM) via raw ctypes — no pywin32/comtypes/pyvda.
The public API has no GetCurrentDesktop, so we use the sanctioned probe
trick: a newly shown window always lands on the CURRENT desktop, so we
create an invisible owned Toplevel, read ITS desktop id, and move the real
window there.

Empirically verified on this machine (Windows 11 build 26200):
- The probe window must be activatable and tracked by the shell: NOT
  overrideredirect / WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE, or
  GetWindowDesktopId fails with 0x8002802B (TYPE_E_ELEMENTNOTFOUND).
  An alpha-0 off-screen Tk Toplevel works after one update().
- Withdrawn windows belong to NO desktop (GetWindowDesktopId fails) and
  IsWindowOnCurrentVirtualDesktop reports TRUE for them, so the
  hidden->deiconify path needs no COM at all: re-shown windows appear on
  the current desktop by design.

Everything here must run on the Tk main thread (STA COM, single thread).
"""
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("whisperclip")

_ole32 = ctypes.OleDLL("ole32")
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            _ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(self))


_CLSID_VDM = _GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
_IID_IVDM = _GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")

_HR = ctypes.c_long
_IS_ON = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, wintypes.HWND,
                            ctypes.POINTER(wintypes.BOOL))
_GET_ID = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, wintypes.HWND,
                             ctypes.POINTER(_GUID))
_MOVE = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, wintypes.HWND,
                           ctypes.POINTER(_GUID))


def tk_toplevel_hwnd(widget) -> int:
    """The real top-level HWND. winfo_id() returns the client child."""
    return _user32.GetParent(widget.winfo_id())


class VirtualDesktopHelper:
    """Documented IVirtualDesktopManager wrapper. Create and use on ONE
    thread (the Tk main thread)."""

    def __init__(self):
        COINIT_APARTMENTTHREADED = 0x2
        try:
            _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        except OSError:
            pass  # S_FALSE / RPC_E_CHANGED_MODE — already initialized
        self._ptr = ctypes.c_void_p()
        CLSCTX_ALL = 0x17
        _ole32.CoCreateInstance(ctypes.byref(_CLSID_VDM), None, CLSCTX_ALL,
                                ctypes.byref(_IID_IVDM), ctypes.byref(self._ptr))
        vtable = ctypes.cast(
            self._ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        self._is_on = _IS_ON(vtable[3])
        self._get_id = _GET_ID(vtable[4])
        self._move = _MOVE(vtable[5])

    def is_window_on_current_desktop(self, hwnd: int) -> bool:
        on = wintypes.BOOL()
        hr = self._is_on(self._ptr, hwnd, ctypes.byref(on))
        if hr != 0:
            raise OSError(f"IsWindowOnCurrentVirtualDesktop hr=0x{hr & 0xFFFFFFFF:08X}")
        return bool(on.value)

    def move_window_to_current_desktop(self, tk_root, hwnd: int):
        import tkinter as tk
        probe = tk.Toplevel(tk_root)
        try:
            probe.attributes("-alpha", 0.0)
            probe.geometry("1x1+-3000+-3000")
            probe.update()
            current = _GUID()
            hr = self._get_id(self._ptr, tk_toplevel_hwnd(probe),
                              ctypes.byref(current))
            if hr != 0:
                raise OSError(f"GetWindowDesktopId(probe) hr=0x{hr & 0xFFFFFFFF:08X}")
        finally:
            probe.destroy()
        hr = self._move(self._ptr, hwnd, ctypes.byref(current))
        if hr != 0:
            raise OSError(f"MoveWindowToDesktop hr=0x{hr & 0xFFFFFFFF:08X}")


def bring_window_to_current_desktop(tk_root, helper_holder: list):
    """Show `tk_root` on the CURRENT virtual desktop, then raise+focus it.

    `helper_holder` is a one-element list caching the VirtualDesktopHelper
    across calls (lazy init on first use). Must run on the Tk main thread.
    Order matters: move FIRST, then deiconify/lift/focus — activating a
    window that lives on another desktop makes Windows switch desktops,
    which is exactly what we're preventing.
    """
    try:
        if helper_holder[0] is None:
            helper_holder[0] = VirtualDesktopHelper()
        helper = helper_holder[0]
        hwnd = tk_toplevel_hwnd(tk_root)
        # Withdrawn windows report TRUE here (verified) and re-shown windows
        # land on the current desktop anyway, so this gate is safe for them.
        if not helper.is_window_on_current_desktop(hwnd):
            helper.move_window_to_current_desktop(tk_root, hwnd)
            log.debug("Window moved to current virtual desktop")
    except Exception as e:
        # COM can fail on future Windows builds or during shell restarts.
        # Hiding strips desktop affinity, so re-show lands on the current
        # desktop — a flicker, but correct.
        log.warning("Virtual desktop move failed (%s) — using hide/re-show", e)
        helper_holder[0] = None
        try:
            tk_root.withdraw()
        except Exception:
            pass
    tk_root.deiconify()
    tk_root.lift()
    tk_root.focus_force()
