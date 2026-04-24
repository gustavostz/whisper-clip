"""Global hotkey listener with automatic Win32 + subprocess fallback.

Windows global-hotkey reliability is a minefield for Python apps. We solve
several stacked root causes:

1. **`RegisterHotKey` conflicts (error 1409).** Another process on the user's
   system (NVIDIA App, GeForce Experience, Xbox Game Bar, ShareX, AutoHotkey,
   OBS, ...) may already own the shortcut system-wide.

2. **`WH_KEYBOARD_LL` hooks die silently under GIL pressure.** Windows 10+
   hard-caps low-level keyboard callbacks at ~1000 ms and permanently removes
   the hook if a callback misses the budget. Whisper model loading / CUDA
   calls routinely stall the main-process GIL well past 1 s.

3. **`WH_KEYBOARD_LL` hooks die silently on session events.** Lock screen
   transitions, session switches, display changes, sleep/resume, UAC prompts
   — any of these can silently detach a low-level hook with no API to detect
   it. This is independent of GIL pressure — process isolation alone is NOT
   enough. We observed it in production: a subprocess-isolated hook went
   dead after ~6 hours while the subprocess kept heartbeating happily.

Our strategy:

- **Primary: `RegisterHotKey` in a dedicated thread.** Kernel-posted
  `WM_HOTKEY` — no timeout, no silent removal, survives lock/unlock,
  not restricted by UIPI. The bulletproof path when we can get it.

- **Fallback: `keyboard` library in a SEPARATE subprocess, with periodic
  hook reinstallation every 30 s.** Process isolation solves problem (2).
  Periodic refresh solves problem (3). The subprocess also listens for
  `FORCE_REFRESH` signals so the main process can tell it to reinstall
  immediately on session unlock.

- **Subprocess heartbeat + main-process watchdog.** Subprocess emits
  `HEARTBEAT` every 15 s. If the main doesn't hear from it for 60 s, it
  kills and respawns — guards against anything truly zombied.

- **Session-change listener in main.** Registers for
  `WM_WTSSESSION_CHANGE` via a message-only window so we can signal
  `FORCE_REFRESH` the instant Windows comes back from lock / resume / etc.

- **Button-click hint.** If the user clicks the record button while we're
  in fallback mode, we infer the hotkey was dead and force-refresh — a
  zero-cost recovery signal.

- **Automatic upgrade.** A retry loop keeps attempting `RegisterHotKey`
  every 15 s so we climb back to the reliable primary path the moment
  the conflicting app exits.

- **Diagnostics.** Every transition is logged. `diagnose()` returns an
  actionable report for the tray-menu "Diagnose Hotkey" entry.
"""
import ctypes
import logging
import multiprocessing
import os
import platform
import threading
import time
from ctypes import wintypes
from enum import Enum
from typing import Any, Callable, Optional


log = logging.getLogger("whisperclip.hotkey")


class HotkeyMode(str, Enum):
    WIN32 = "win32"              # RegisterHotKey — bulletproof
    FALLBACK = "fallback"        # keyboard library in subprocess — best effort
    UNAVAILABLE = "unavailable"  # nothing is listening


# --- Win32 constants ----------------------------------------------

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

_HOTKEY_ID = 1
_PROBE_HOTKEY_ID = 9999  # Distinct ID used by diagnose() probes

_MODIFIER_MAP = {
    'alt': _MOD_ALT,
    'ctrl': _MOD_CONTROL, 'control': _MOD_CONTROL,
    'shift': _MOD_SHIFT,
    'win': _MOD_WIN, 'windows': _MOD_WIN, 'super': _MOD_WIN,
}

_NAMED_KEYS = {
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73,
    'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77,
    'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'space': 0x20, 'enter': 0x0D, 'return': 0x0D,
    'tab': 0x09, 'escape': 0x1B, 'esc': 0x1B,
    'backspace': 0x08, 'delete': 0x2E, 'insert': 0x2D,
    'home': 0x24, 'end': 0x23,
    'pageup': 0x21, 'page_up': 0x21,
    'pagedown': 0x22, 'page_down': 0x22,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'printscreen': 0x2C, 'print_screen': 0x2C,
    'pause': 0x13, 'capslock': 0x14, 'numlock': 0x90,
}

_WIN_ERROR_NAMES = {
    0: "ERROR_SUCCESS",
    1409: "ERROR_HOTKEY_ALREADY_REGISTERED",
    1418: "ERROR_HOTKEY_NOT_REGISTERED",
}


def _win_error_name(code: int) -> str:
    return _WIN_ERROR_NAMES.get(code, f"UNKNOWN({code})")


def parse_shortcut(shortcut: str) -> tuple[int, int]:
    """Parse e.g. 'alt+shift+r' into (modifiers_bitfield, virtual_key_code)."""
    parts = [p.strip().lower() for p in shortcut.split('+')]
    modifiers = 0
    key = None

    for part in parts:
        if part in _MODIFIER_MAP:
            modifiers |= _MODIFIER_MAP[part]
        else:
            key = part

    if key is None:
        raise ValueError(f"No non-modifier key found in shortcut '{shortcut}'")

    if len(key) == 1:
        vk = ctypes.windll.user32.VkKeyScanW(ord(key)) & 0xFF
    else:
        vk = _NAMED_KEYS.get(key)
        if vk is None:
            raise ValueError(f"Unknown key '{key}' in shortcut '{shortcut}'")

    return modifiers, vk


# --- Subprocess worker (fallback path) ----------------------------

# How often the subprocess reinstalls its WH_KEYBOARD_LL hook proactively.
# Windows can silently remove the hook on session events; periodic reinstall
# is the canonical workaround used by AutoHotkey and similar tools.
_SUBPROCESS_REFRESH_INTERVAL = 30.0

# How often the subprocess sends a heartbeat to the main process.
_SUBPROCESS_HEARTBEAT_INTERVAL = 15.0


def _subprocess_worker(shortcut: str, trigger_queue, signal_queue,
                       shutdown_flag, log_file_path: str):
    """Runs in a dedicated Python process. Owns a WH_KEYBOARD_LL hook via
    the `keyboard` library and emits events back to the parent.

    Events emitted to `trigger_queue` (parent consumes):
        "TRIGGER"                        — hotkey pressed
        ("HEARTBEAT", presses, refreshes) — liveness beacon

    Signals received on `signal_queue` (parent produces):
        "FORCE_REFRESH" — reinstall the hook immediately
                          (sent after Windows unlock, etc.)
        "__READER_STOP__" — ignored here; used by parent's reader loop

    Why refresh the hook even though we're isolated from GIL pressure?
    Because `WH_KEYBOARD_LL` silently dies on events the process can't
    see (session transitions, display changes, some driver activity).
    Reinstalling every 30 s catches that within one refresh cycle.
    """
    import logging as _logging
    import queue as _queue

    logger = _logging.getLogger("whisperclip.hotkey.worker")
    logger.setLevel(_logging.DEBUG)
    logger.propagate = False

    try:
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        handler = _logging.FileHandler(log_file_path, encoding="utf-8")
        handler.setFormatter(_logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [subprocess pid=%(process)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
    except Exception:
        pass  # Logging is best-effort; the hotkey must still work

    logger.info("=" * 60)
    logger.info("Fallback hotkey subprocess starting, shortcut=%r", shortcut)
    logger.info("Python: %s  Platform: %s",
                os.sys.version.split()[0], platform.platform())

    try:
        import keyboard
    except ImportError as e:
        logger.critical("'keyboard' module unavailable in subprocess: %s", e)
        return

    press_count = 0
    refresh_count = 0

    def on_trigger():
        nonlocal press_count
        press_count += 1
        logger.debug("Hotkey pressed (#%d) — enqueueing TRIGGER", press_count)
        try:
            trigger_queue.put("TRIGGER", block=False)
        except Exception as e:
            logger.error("Queue put failed: %s", e)

    def install_hook(reason: str) -> bool:
        """(Re)install the keyboard hook. Returns True on success."""
        nonlocal refresh_count
        try:
            keyboard.unhook_all()
        except Exception as e:
            logger.warning("unhook_all before (re)install failed: %s", e)
        try:
            keyboard.add_hotkey(shortcut, on_trigger, suppress=False)
            refresh_count += 1
            logger.info("Hook installed (#%d, reason=%s) for %r",
                        refresh_count, reason, shortcut)
            return True
        except Exception as e:
            logger.error("add_hotkey failed (reason=%s): %s", reason, e,
                         exc_info=True)
            return False

    if not install_hook("initial"):
        logger.critical("Initial hook install failed — subprocess exiting")
        return

    logger.info(
        "Entering main loop (refresh every %.0fs, heartbeat every %.0fs)",
        _SUBPROCESS_REFRESH_INTERVAL, _SUBPROCESS_HEARTBEAT_INTERVAL,
    )

    last_refresh = time.monotonic()
    last_heartbeat = time.monotonic()
    consecutive_refresh_failures = 0

    # Tight loop: poll the signal queue with a short timeout, then check
    # timers. The short wait lets us react to FORCE_REFRESH within ~0.5 s.
    while not shutdown_flag.is_set():
        try:
            signal = signal_queue.get(timeout=0.5)
        except _queue.Empty:
            signal = None
        except (EOFError, OSError) as e:
            logger.error("Signal queue broken: %s — exiting subprocess", e)
            break

        now = time.monotonic()

        if signal == "FORCE_REFRESH":
            logger.info("FORCE_REFRESH received from main process")
            if install_hook("force"):
                last_refresh = now
                consecutive_refresh_failures = 0
            else:
                consecutive_refresh_failures += 1

        # Periodic refresh
        if now - last_refresh >= _SUBPROCESS_REFRESH_INTERVAL:
            if install_hook("periodic"):
                last_refresh = now
                consecutive_refresh_failures = 0
            else:
                consecutive_refresh_failures += 1

        # If refresh keeps failing, give up — parent watchdog will respawn us
        if consecutive_refresh_failures >= 5:
            logger.critical(
                "Hook refresh failed %d times consecutively — exiting so "
                "the parent watchdog respawns this subprocess",
                consecutive_refresh_failures,
            )
            break

        # Heartbeat
        if now - last_heartbeat >= _SUBPROCESS_HEARTBEAT_INTERVAL:
            try:
                trigger_queue.put(
                    ("HEARTBEAT", press_count, refresh_count),
                    block=False,
                )
            except Exception as e:
                logger.warning("Heartbeat put failed: %s", e)
            last_heartbeat = now
            logger.debug(
                "Heartbeat sent (presses=%d, refreshes=%d)",
                press_count, refresh_count,
            )

    logger.info(
        "Shutdown received — unhooking (presses=%d, refreshes=%d)",
        press_count, refresh_count,
    )
    try:
        keyboard.unhook_all()
    except Exception as e:
        logger.error("unhook_all on exit failed: %s", e)

    logger.info("Fallback hotkey subprocess exiting cleanly")


# --- Main-process listener ----------------------------------------

class HotkeyListener:
    """Manages a global hotkey for the main app.

    Usage:
        listener = HotkeyListener('alt+shift+r', on_trigger=my_callback)
        listener.start()
        ...
        listener.stop()

    Mode is exposed via `get_mode()` and `get_status_description()` so the
    UI can show the user whether the reliable Win32 path is active or not.
    """

    _UPGRADE_RETRY_INTERVAL = 15.0  # seconds between upgrade attempts
    _HEARTBEAT_STALE_THRESHOLD = 60.0  # seconds without heartbeat = zombie
    _WATCHDOG_POLL_INTERVAL = 10.0   # how often the watchdog checks

    def __init__(self, shortcut: str, on_trigger: Callable[[], None],
                 log_dir: Optional[str] = None):
        if platform.system() != "Windows":
            raise RuntimeError("HotkeyListener currently only supports Windows")

        self.shortcut = shortcut
        self.on_trigger = on_trigger
        self.log_dir = log_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs"
        )

        self._modifiers, self._vk = parse_shortcut(shortcut)
        log.debug("Parsed shortcut %r into modifiers=0x%04x vk=0x%02x",
                  shortcut, self._modifiers, self._vk)

        self._mode: HotkeyMode = HotkeyMode.UNAVAILABLE
        self._state_lock = threading.Lock()
        self._shutdown = threading.Event()

        # Win32 path state
        self._win32_thread: Optional[threading.Thread] = None
        self._win32_thread_id: Optional[int] = None
        self._win32_registered = threading.Event()
        self._win32_stopped = threading.Event()

        # Subprocess path state
        self._subprocess: Optional[multiprocessing.Process] = None
        self._subprocess_shutdown: Optional[Any] = None  # multiprocessing.Event
        self._subprocess_queue: Optional[Any] = None    # multiprocessing.Queue (child->parent)
        self._subprocess_signal_queue: Optional[Any] = None  # multiprocessing.Queue (parent->child)
        self._subprocess_reader: Optional[threading.Thread] = None
        self._subprocess_respawn_attempts = 0
        self._subprocess_respawn_limit = 3
        self._last_subprocess_heartbeat: float = 0.0
        self._last_subprocess_press_count: int = 0
        self._last_subprocess_refresh_count: int = 0

        # Upgrade loop + watchdog
        self._upgrade_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        # Session-change listener (Windows lock/unlock detection)
        self._session_thread: Optional[threading.Thread] = None
        self._session_hwnd: Optional[int] = None

    # --- Public API ------------------------------------------------

    def start(self):
        log.info("Starting hotkey listener for shortcut=%r", self.shortcut)

        if self._try_start_win32():
            log.info("*** Hotkey ACTIVE via Win32 RegisterHotKey (reliable path) ***")
        else:
            log.warning(
                "Win32 path unavailable — %r is owned by another process. "
                "Starting subprocess fallback. Run Diagnose Hotkey from the tray "
                "to identify the conflicting app.",
                self.shortcut,
            )
            self._start_subprocess()
            log.info(
                "*** Hotkey ACTIVE via subprocess fallback "
                "(with 30s periodic refresh + watchdog + session-unlock recovery) ***"
            )

        self._upgrade_thread = threading.Thread(
            target=self._upgrade_loop,
            name="hotkey-upgrade-worker",
            daemon=True,
        )
        self._upgrade_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="hotkey-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        self._session_thread = threading.Thread(
            target=self._session_listener_loop,
            name="hotkey-session-listener",
            daemon=True,
        )
        self._session_thread.start()

    def stop(self):
        log.info("Stopping hotkey listener")
        self._shutdown.set()
        self._stop_session_listener()
        self._stop_win32()
        self._stop_subprocess()
        log.info("Hotkey listener stopped")

    def notice_button_click(self):
        """Hint from the UI layer that the user clicked the record button.

        If we're in fallback mode and the user clicks the button, odds are
        high that they tried the hotkey first and it was dead. Force-refresh
        the subprocess's hook immediately so the next hotkey press works.
        Logged as WARNING so we can correlate "user-clicked-because-hotkey-
        was-dead" events in the logs.
        """
        mode = self.get_mode()
        if mode == HotkeyMode.FALLBACK:
            log.warning(
                "Button click while in FALLBACK mode — user may have tried "
                "the hotkey first. Forcing subprocess hook refresh.",
            )
            self._signal_subprocess("FORCE_REFRESH")

    def get_mode(self) -> HotkeyMode:
        with self._state_lock:
            return self._mode

    def get_status_description(self) -> str:
        mode = self.get_mode()
        if mode == HotkeyMode.WIN32:
            return f"{self.shortcut} — reliable (Win32)"
        if mode == HotkeyMode.FALLBACK:
            return f"{self.shortcut} — fallback (another app owns this shortcut)"
        return f"{self.shortcut} — NOT ACTIVE"

    def diagnose(self) -> dict:
        """Synchronously probe the Win32 hotkey state. Safe from any thread.

        Returns a dict with `shortcut`, `current_mode`, `win32_probe`
        (either 'success' or 'failed (error N: NAME)'), `win32_error_code`
        (int or None), and a list of `suggestions` for the user.

        The probe briefly registers then unregisters the hotkey with a
        DIFFERENT id than our production listener, so the live listener
        is not disturbed. HOWEVER: if our own Win32 listener is active,
        Windows will return 1409 from this probe because we already own
        it — that's the correct signal that Win32 mode is working.
        """
        report = {
            "shortcut": self.shortcut,
            "current_mode": self.get_mode().value,
            "win32_probe": None,
            "win32_error_code": None,
            "suggestions": [],
        }

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        # If we already own the hotkey on the Win32 path, the probe will
        # report 1409 against *us* — skip it and report the good state.
        if report["current_mode"] == HotkeyMode.WIN32.value:
            report["win32_probe"] = "skipped (we own it — Win32 path is already active)"
            report["suggestions"].append(
                "Hotkey is active on the reliable Win32 path. Nothing to fix."
            )
            return report

        try:
            ok = user32.RegisterHotKey(None, _PROBE_HOTKEY_ID, self._modifiers, self._vk)
            if ok:
                user32.UnregisterHotKey(None, _PROBE_HOTKEY_ID)
                report["win32_probe"] = "success"
                report["suggestions"].append(
                    "Win32 registration works right now. The listener will "
                    "upgrade to the reliable Win32 path within 15 seconds. "
                    "Or restart WhisperClip to pick it up immediately."
                )
            else:
                err = ctypes.get_last_error()
                report["win32_error_code"] = err
                report["win32_probe"] = f"failed (error {err}: {_win_error_name(err)})"
                if err == 1409:
                    report["suggestions"].extend([
                        "Windows reports this shortcut is owned by another process "
                        "(ERROR_HOTKEY_ALREADY_REGISTERED).",
                        "Likely suspects: NVIDIA App / GeForce Experience, Xbox Game Bar, "
                        "ShareX, OBS Studio, AutoHotkey scripts, Steam, Discord overlay.",
                        "Disabling the UI toggle isn't always enough — some apps still "
                        "register the hotkey as long as the process is running. Fully "
                        "QUIT the suspect app from its tray icon and try again.",
                        "Tool to identify the culprit: HotKeysList from NirSoft — "
                        "enumerates every app holding a hotkey. "
                        "https://www.nirsoft.net/utils/hotkeys_list.html",
                        "Quick workaround: change 'shortcut' in config.json to something "
                        "uncontested, e.g. 'ctrl+shift+space', 'ctrl+alt+r', or 'f9'.",
                    ])
                else:
                    report["suggestions"].append(
                        f"Unexpected Win32 error {err}. Check the log file for details."
                    )
        except Exception as e:
            report["win32_probe"] = f"probe threw: {e}"

        return report

    # --- Win32 primary path ---------------------------------------

    def _try_start_win32(self) -> bool:
        """Spawn the Win32 listener thread. Returns True if it registered
        successfully within 2 seconds, False otherwise."""
        if self._shutdown.is_set():
            return False
        if self._win32_thread is not None and self._win32_thread.is_alive():
            log.debug("Win32 listener thread already running")
            return self.get_mode() == HotkeyMode.WIN32

        self._win32_registered.clear()
        self._win32_stopped.clear()

        self._win32_thread = threading.Thread(
            target=self._win32_message_loop,
            name="hotkey-win32-listener",
            daemon=True,
        )
        self._win32_thread.start()

        if not self._win32_registered.wait(timeout=2.0):
            log.warning("Win32 registration did not complete within 2s")
            return False

        if self.get_mode() == HotkeyMode.WIN32:
            return True

        # Registered event fired but mode wasn't set — means it failed fast
        return False

    def _win32_message_loop(self):
        """Register hotkey + pump WM_HOTKEY on the SAME thread.

        Windows binds a hotkey to the thread that called RegisterHotKey,
        so all three operations (register, GetMessage, Unregister) must
        happen on this thread.
        """
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._win32_thread_id = kernel32.GetCurrentThreadId()

        log.debug("Win32 listener thread started (tid=%d, modifiers=0x%04x, vk=0x%02x)",
                  self._win32_thread_id, self._modifiers, self._vk)

        ok = user32.RegisterHotKey(None, _HOTKEY_ID, self._modifiers, self._vk)
        if not ok:
            err = ctypes.get_last_error()
            log.warning(
                "RegisterHotKey failed: error %d (%s). "
                "Shortcut %r is currently owned by another process.",
                err, _win_error_name(err), self.shortcut,
            )
            self._win32_registered.set()  # signal "done trying"
            self._win32_stopped.set()
            return

        with self._state_lock:
            self._mode = HotkeyMode.WIN32
        log.info("RegisterHotKey succeeded for %r (tid=%d)",
                 self.shortcut, self._win32_thread_id)
        self._win32_registered.set()

        presses = 0
        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:
                    log.debug("Win32 loop: GetMessageW returned 0 (WM_QUIT)")
                    break
                if ret == -1:
                    err = ctypes.get_last_error()
                    log.error("Win32 loop: GetMessageW returned -1, error %d (%s)",
                              err, _win_error_name(err))
                    break
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    presses += 1
                    log.debug("WM_HOTKEY received (#%d, Win32 path)", presses)
                    self._dispatch_trigger()
        except Exception as e:
            log.error("Win32 message loop crashed: %s", e, exc_info=True)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            log.info("Win32 listener stopped (handled %d presses)", presses)
            with self._state_lock:
                if self._mode == HotkeyMode.WIN32:
                    self._mode = HotkeyMode.UNAVAILABLE
            self._win32_stopped.set()

    def _stop_win32(self):
        if self._win32_thread is None or not self._win32_thread.is_alive():
            return
        if self._win32_thread_id is None:
            return
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
            ]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            ok = user32.PostThreadMessageW(self._win32_thread_id, _WM_QUIT, 0, 0)
            log.debug("PostThreadMessageW(WM_QUIT) returned %d", ok)
        except Exception as e:
            log.error("Failed to signal Win32 thread to quit: %s", e)
        self._win32_thread.join(timeout=2.0)
        if self._win32_thread.is_alive():
            log.warning("Win32 thread did not exit within 2s")

    # --- Subprocess fallback path --------------------------------

    def _start_subprocess(self):
        if self._shutdown.is_set():
            return
        if self._subprocess is not None and self._subprocess.is_alive():
            log.debug("Subprocess already running (pid=%d)", self._subprocess.pid)
            return

        self._subprocess_shutdown = multiprocessing.Event()
        self._subprocess_queue = multiprocessing.Queue(maxsize=128)
        self._subprocess_signal_queue = multiprocessing.Queue(maxsize=32)
        self._last_subprocess_heartbeat = time.monotonic()  # reset

        log_file = os.path.join(
            self.log_dir,
            f"hotkey-fallback_{time.strftime('%Y-%m-%d')}.log",
        )

        self._subprocess = multiprocessing.Process(
            target=_subprocess_worker,
            args=(self.shortcut, self._subprocess_queue,
                  self._subprocess_signal_queue,
                  self._subprocess_shutdown, log_file),
            name="whisperclip-hotkey-fallback",
            daemon=True,
        )
        self._subprocess.start()
        log.info("Fallback subprocess started (pid=%d, log=%s)",
                 self._subprocess.pid, log_file)

        with self._state_lock:
            self._mode = HotkeyMode.FALLBACK

        self._subprocess_reader = threading.Thread(
            target=self._subprocess_reader_loop,
            name="hotkey-subprocess-reader",
            daemon=True,
        )
        self._subprocess_reader.start()

    def _signal_subprocess(self, signal: str):
        """Send a command to the running subprocess (e.g. FORCE_REFRESH)."""
        q = self._subprocess_signal_queue
        if q is None:
            log.debug("Cannot signal %r — no subprocess signal queue", signal)
            return
        try:
            q.put_nowait(signal)
            log.debug("Signal %r sent to subprocess", signal)
        except Exception as e:
            log.warning("Failed to send signal %r: %s", signal, e)

    def _stop_subprocess(self):
        proc = self._subprocess
        if proc is None:
            return

        if self._subprocess_shutdown is not None:
            self._subprocess_shutdown.set()

        # Wake the reader thread so it exits its blocking get() immediately
        # instead of waiting up to 500 ms for the next timeout.
        if self._subprocess_queue is not None:
            try:
                self._subprocess_queue.put_nowait("__READER_STOP__")
            except Exception:
                pass

        # Also drain/signal the child's signal queue so its own get() returns
        # promptly (the child wakes on shutdown_flag anyway, this just avoids
        # a 500 ms wait in the last loop iteration).
        if self._subprocess_signal_queue is not None:
            try:
                self._subprocess_signal_queue.put_nowait("SHUTDOWN")
            except Exception:
                pass

        proc.join(timeout=3.0)
        if proc.is_alive():
            log.warning("Subprocess (pid=%d) did not exit in 3s — terminating",
                        proc.pid)
            proc.terminate()
            proc.join(timeout=1.0)
        if proc.is_alive():
            log.error("Subprocess (pid=%d) did not exit even after terminate — killing",
                      proc.pid)
            proc.kill()
            proc.join(timeout=1.0)

        log.info("Fallback subprocess stopped (exit code %s)", proc.exitcode)

        self._subprocess = None
        self._subprocess_shutdown = None
        self._subprocess_queue = None
        self._subprocess_signal_queue = None

        # Wait for the reader thread to drain so it can't emit a late
        # trigger after we've returned from stop(). Only join if it's
        # not the current thread (the reader itself calls _stop_subprocess
        # via _handle_subprocess_death).
        reader = self._subprocess_reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
            if reader.is_alive():
                log.warning("Subprocess reader thread did not exit within 1s")
            self._subprocess_reader = None

        with self._state_lock:
            if self._mode == HotkeyMode.FALLBACK:
                self._mode = HotkeyMode.UNAVAILABLE

    def _subprocess_reader_loop(self):
        import queue as _queue
        q = self._subprocess_queue
        while not self._shutdown.is_set():
            if q is None:
                break
            try:
                item = q.get(timeout=0.5)
            except _queue.Empty:
                if self._subprocess is not None and not self._subprocess.is_alive():
                    log.error(
                        "Fallback subprocess died unexpectedly (exit code %s)",
                        self._subprocess.exitcode,
                    )
                    self._handle_subprocess_death()
                    break
                continue
            except (EOFError, OSError) as e:
                log.warning("Subprocess queue closed: %s", e)
                break

            if self._shutdown.is_set():
                break
            if item == "__READER_STOP__":
                log.debug("Reader stop sentinel received — exiting reader loop")
                break
            if item == "TRIGGER":
                log.debug("Trigger received from fallback subprocess")
                self._dispatch_trigger()
            elif isinstance(item, tuple) and item and item[0] == "HEARTBEAT":
                self._last_subprocess_heartbeat = time.monotonic()
                if len(item) >= 3:
                    presses, refreshes = item[1], item[2]
                    # Log at DEBUG only when counters advance, to keep noise
                    # low but still make the refresh cadence visible.
                    if (presses != self._last_subprocess_press_count
                            or refreshes != self._last_subprocess_refresh_count):
                        log.debug(
                            "Subprocess heartbeat: presses=%d refreshes=%d",
                            presses, refreshes,
                        )
                    self._last_subprocess_press_count = presses
                    self._last_subprocess_refresh_count = refreshes
            else:
                log.warning("Unknown item from subprocess queue: %r", item)

    def _handle_subprocess_death(self):
        """Try to respawn the subprocess if it crashed. Gives up after
        a small number of attempts."""
        with self._state_lock:
            if self._mode == HotkeyMode.FALLBACK:
                self._mode = HotkeyMode.UNAVAILABLE

        if self._shutdown.is_set():
            return

        self._subprocess_respawn_attempts += 1
        if self._subprocess_respawn_attempts > self._subprocess_respawn_limit:
            log.critical(
                "Fallback subprocess has died %d times — giving up. "
                "Hotkey is NOT listening. Restart the app.",
                self._subprocess_respawn_attempts,
            )
            return

        log.warning(
            "Respawning fallback subprocess (attempt %d/%d)",
            self._subprocess_respawn_attempts, self._subprocess_respawn_limit,
        )
        self._subprocess = None
        self._subprocess_queue = None
        self._subprocess_shutdown = None

        # Wait with shutdown-aware sleep. While we sleep the upgrade loop
        # may take over the Win32 path or a stop() may arrive; re-check
        # before spawning so we don't end up with both paths active.
        if self._shutdown.wait(timeout=1.0):
            return
        if self.get_mode() == HotkeyMode.WIN32:
            log.info("Win32 path came up during respawn wait — skipping subprocess respawn")
            return
        self._start_subprocess()

    # --- Watchdog ------------------------------------------------

    def _watchdog_loop(self):
        """Kill + respawn the subprocess if its heartbeat goes stale.

        The subprocess may appear alive to the OS while its internal
        `keyboard` library state (or the WH_KEYBOARD_LL hook itself) is
        broken. We observed this in production: 16 hours of heartbeats,
        but the hook only actually saw ~50% of real presses. If the
        subprocess stops heartbeating at all, respawning is a clean way
        out of any accumulated weirdness.
        """
        while not self._shutdown.wait(timeout=self._WATCHDOG_POLL_INTERVAL):
            if self.get_mode() != HotkeyMode.FALLBACK:
                continue
            if self._last_subprocess_heartbeat == 0:
                continue  # subprocess hasn't sent its first heartbeat yet
            age = time.monotonic() - self._last_subprocess_heartbeat
            if age > self._HEARTBEAT_STALE_THRESHOLD:
                log.error(
                    "Subprocess heartbeat stale (%.1fs > %.0fs) — "
                    "killing and respawning",
                    age, self._HEARTBEAT_STALE_THRESHOLD,
                )
                try:
                    self._stop_subprocess()
                except Exception as e:
                    log.error("Error stopping stale subprocess: %s", e,
                              exc_info=True)
                if not self._shutdown.is_set():
                    self._subprocess_respawn_attempts += 1
                    if self._subprocess_respawn_attempts <= self._subprocess_respawn_limit:
                        log.info(
                            "Watchdog respawning subprocess (attempt %d/%d)",
                            self._subprocess_respawn_attempts,
                            self._subprocess_respawn_limit,
                        )
                        self._start_subprocess()
                    else:
                        log.critical(
                            "Watchdog respawn limit (%d) reached — giving up",
                            self._subprocess_respawn_limit,
                        )

    # --- Session listener (Windows lock/unlock) -----------------

    def _session_listener_loop(self):
        """Create a message-only window and receive WM_WTSSESSION_CHANGE.

        When Windows returns from lock, sleep, or fast user switch, it
        reuses the old desktop but many low-level keyboard hooks get
        silently detached during the transition. Catching the unlock
        event lets us force-refresh the subprocess's hook immediately
        instead of waiting up to 30 s for the periodic refresh.
        """
        WM_WTSSESSION_CHANGE = 0x02B1
        WTS_SESSION_UNLOCK = 0x8
        WTS_SESSION_LOGON = 0x5
        WTS_CONSOLE_CONNECT = 0x1
        NOTIFY_FOR_THIS_SESSION = 0

        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            wtsapi32 = ctypes.WinDLL('wtsapi32', use_last_error=True)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        except OSError as e:
            log.warning("Session listener: could not load system DLLs: %s", e)
            return

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
            wintypes.WPARAM, wintypes.LPARAM,
        )

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE:
                if wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON,
                              WTS_CONSOLE_CONNECT):
                    log.warning(
                        "Session event (wparam=0x%x) — forcing subprocess refresh",
                        wparam,
                    )
                    self._signal_subprocess("FORCE_REFRESH")
                else:
                    log.debug("Session event (wparam=0x%x) — ignored", wparam)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wc = WNDCLASS()
        wc.lpfnWndProc = WNDPROC(wnd_proc)
        wc.lpszClassName = "WhisperClipHotkeySessionListener"
        wc.hInstance = kernel32.GetModuleHandleW(None)

        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL

        wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            # 1410 = ERROR_CLASS_ALREADY_EXISTS. Harmless — we can still
            # CreateWindowEx with this class name. Happens if the listener
            # thread restarts within one process lifetime.
            if err != 1410:
                log.warning("Session listener: RegisterClassW failed (error %d)", err)
                return
            log.debug("Session listener: window class already registered — reusing")

        HWND_MESSAGE = wintypes.HWND(-3)
        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "WhisperClipHotkeySessionListener",
            0, 0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None,
        )
        if not hwnd:
            err = ctypes.get_last_error()
            log.warning("Session listener: CreateWindowExW failed (error %d)", err)
            return

        self._session_hwnd = hwnd

        if not wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
            err = ctypes.get_last_error()
            log.warning(
                "Session listener: WTSRegisterSessionNotification failed (error %d) — "
                "falling back to periodic refresh only", err,
            )
            user32.DestroyWindow(hwnd)
            self._session_hwnd = None
            return

        log.info("Session listener registered — will force-refresh hook on unlock/logon/connect")

        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            except Exception as e:
                log.debug("WTSUnRegisterSessionNotification failed: %s", e)
            try:
                user32.DestroyWindow(hwnd)
            except Exception as e:
                log.debug("DestroyWindow failed: %s", e)
            log.info("Session listener stopped")

    def _stop_session_listener(self):
        hwnd = self._session_hwnd
        if hwnd is None:
            return
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostMessageW.argtypes = [
                wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
            ]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW(hwnd, _WM_QUIT, 0, 0)
        except Exception as e:
            log.debug("Failed to post WM_QUIT to session window: %s", e)
        self._session_hwnd = None

    # --- Upgrade loop --------------------------------------------

    def _upgrade_loop(self):
        """Every N seconds, try to upgrade from fallback to Win32.

        If another app was holding the shortcut and exits, we want to
        reclaim the reliable path instead of staying in fallback mode
        for the rest of the session.
        """
        while not self._shutdown.wait(timeout=self._UPGRADE_RETRY_INTERVAL):
            mode = self.get_mode()

            if mode == HotkeyMode.WIN32:
                # Sanity check: if the thread died while we were "WIN32",
                # drop back to fallback.
                if self._win32_thread is None or not self._win32_thread.is_alive():
                    log.warning("Win32 thread died while in WIN32 mode — "
                                "falling back to subprocess")
                    with self._state_lock:
                        self._mode = HotkeyMode.UNAVAILABLE
                    self._start_subprocess()
                continue

            # Mode is FALLBACK or UNAVAILABLE — try to upgrade
            log.debug("Upgrade attempt: retrying RegisterHotKey")
            if self._try_start_win32():
                log.info("*** Upgraded to Win32 hotkey path — stopping subprocess ***")
                self._stop_subprocess()

    # --- Callback dispatch ----------------------------------------

    def _dispatch_trigger(self):
        """Invoke the user's on_trigger callback.

        We call it directly (not in a new thread) because WhisperClip's
        `toggle_recording` already spawns its own worker thread and
        returns instantly. If you pass a slow callback, wrap it yourself.
        """
        try:
            self.on_trigger()
        except Exception as e:
            log.error("on_trigger callback raised: %s", e, exc_info=True)
