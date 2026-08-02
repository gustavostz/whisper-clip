"""Global hotkey listener with automatic Win32 + subprocess fallback.

Windows global-hotkey reliability is a minefield for Python apps. We solve
several stacked root causes (all observed in production):

1. **`RegisterHotKey` conflicts (error 1409).** Another process on the user's
   system may already own the shortcut system-wide. On this machine the NVIDIA
   App overlay registers Alt+Shift+R ("cycle performance overlay") at logon,
   permanently blocking the reliable path. `diagnose()` detects this.

2. **`WH_KEYBOARD_LL` hooks die silently.** Windows removes a low-level hook
   without notification if its callback misses the LowLevelHooksTimeout
   budget (clamped to 1000 ms since Win10 1709), and session transitions
   (lock/unlock, sleep/resume) can detach hooks outright. There is NO API to
   detect removal.

3. **The `keyboard` library cannot reinstall its hook.** It calls
   SetWindowsHookEx exactly once per process (lazy listener thread guarded by
   a never-reset `listening` flag); `unhook_all()` only clears Python-side
   handler dicts. Any "unhook_all + add_hotkey refresh" is a placebo at the
   OS level. The only real recovery is a fresh process.

Our strategy:

- **Primary: `RegisterHotKey` in a dedicated thread.** Kernel-posted
  `WM_HOTKEY` — no timeout, no silent removal, survives lock/unlock.
  A retry loop keeps attempting the upgrade every 15 s.

- **Fallback: `keyboard` library in a SEPARATE subprocess with an
  end-to-end liveness probe.** The subprocess injects a harmless synthetic
  F24 keypress via SendInput and verifies its own hook observed it. If the
  hook is dead, the subprocess exits with a distinct code and the parent
  respawns it — a fresh process is the only way to get a fresh
  SetWindowsHookEx. Probes are gated on recent *real* user activity and are
  skipped while the session is locked, so they never keep the machine awake
  or fight the lock screen.

- **Watchdog with suspend awareness.** Heartbeat staleness is only trusted
  when the watchdog itself hasn't just slept (a big monotonic gap means the
  whole machine was suspended — that's a probe trigger, not a subprocess
  failure). Respawns are governed by a sliding-window rate limiter that
  backs off but NEVER permanently gives up.

- **Session-change listener.** On unlock we immediately request a probe, so
  a hook killed by the lock transition is detected and respawned within
  seconds — exactly when the user is about to dictate.

- **Process hygiene.** The subprocess watches its parent and self-exits if
  the parent dies, so hard kills can't leave orphan hooks behind.

- **Diagnostics.** Every transition is logged. `diagnose()` returns an
  actionable report and names the conflicting NVIDIA binding when it can
  prove it from the NVIDIA overlay's own log.
"""
import ctypes
import logging
import multiprocessing
import os
import platform
import re
import threading
import time
from collections import deque
from ctypes import wintypes
from enum import Enum
from typing import Any, Callable, Optional


log = logging.getLogger("whisperclip.hotkey")


class HotkeyMode(str, Enum):
    WIN32 = "win32"              # RegisterHotKey — bulletproof
    FALLBACK = "fallback"        # keyboard library in subprocess — best effort
    UNAVAILABLE = "unavailable"  # nothing is listening (transient; auto-recovers)


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

# Subprocess exit codes with meaning to the parent.
EXIT_HOOK_DEAD = 42   # liveness probe failed — hook silently removed by the OS


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


# --- Process responsiveness (priority / power throttling) ---------

def ensure_process_responsiveness(logger=None):
    """Raise this process out of below-normal priority and opt out of
    Windows 11 power throttling (EcoQoS) + timer-resolution coalescing.

    Task Scheduler launches tasks at BELOW_NORMAL priority when the task XML
    has no <Priority> element, and hidden background processes are EcoQoS
    candidates — both make WH_KEYBOARD_LL callbacks likelier to miss the
    1000 ms deadline that gets hooks silently removed. Best-effort; failures
    are logged and ignored.
    """
    logger = logger or log
    if platform.system() != "Windows":
        return
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # Without explicit signatures ctypes truncates HANDLEs to 32 bits —
        # GetCurrentProcess()'s pseudo-handle (-1) then becomes an invalid
        # handle on x64 (error 6).
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.SetProcessInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL

        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        IDLE_PRIORITY_CLASS = 0x40
        NORMAL_PRIORITY_CLASS = 0x20

        current = kernel32.GetPriorityClass(kernel32.GetCurrentProcess())
        if current in (BELOW_NORMAL_PRIORITY_CLASS, IDLE_PRIORITY_CLASS):
            if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                         NORMAL_PRIORITY_CLASS):
                logger.info("Raised process priority class 0x%x -> NORMAL", current)
            else:
                logger.warning("SetPriorityClass failed (error %d)",
                               ctypes.get_last_error())

        # PROCESS_POWER_THROTTLING_STATE: opt out of EcoQoS and timer coalescing.
        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG),
                        ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
        ProcessPowerThrottling = 4

        state = PROCESS_POWER_THROTTLING_STATE(
            Version=1,
            ControlMask=(PROCESS_POWER_THROTTLING_EXECUTION_SPEED
                         | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION),
            StateMask=0,  # 0 = force-disable throttling for the masked controls
        )
        ok = kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), ProcessPowerThrottling,
            ctypes.byref(state), ctypes.sizeof(state),
        )
        if not ok:
            logger.debug("SetProcessInformation(PowerThrottling) failed (error %d)",
                         ctypes.get_last_error())
    except Exception as e:
        logger.warning("ensure_process_responsiveness failed: %s", e)


# --- Synthetic input (probe) plumbing -----------------------------

_VK_F24 = 0x87  # phantom key: no physical key, no app binds it — safe to inject


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _send_probe_key(user32) -> bool:
    """Inject a synthetic F24 press+release. Returns True if accepted."""
    KEYEVENTF_KEYUP = 0x0002
    MAPVK_VK_TO_VSC = 0
    # Fill in the real scan code — the keyboard library keys its matching
    # off scan codes, and hooks downstream see a more realistic event.
    scan = user32.MapVirtualKeyW(_VK_F24, MAPVK_VK_TO_VSC)
    ok = True
    for flags in (0, KEYEVENTF_KEYUP):
        inp = _INPUT()
        inp.type = 1  # INPUT_KEYBOARD
        inp.u.ki = _KEYBDINPUT(_VK_F24, scan, flags, 0, 0)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
            ok = False
    return ok


def _session_input_available(user32) -> bool:
    """True when the interactive desktop receives our input (not locked /
    secure desktop). OpenInputDesktop fails while the session is locked."""
    DESKTOP_READOBJECTS = 0x0001
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if hdesk:
        user32.CloseDesktop(hdesk)
        return True
    return False


# --- Subprocess worker (fallback path) ----------------------------

# Send a heartbeat to the parent this often.
_SUBPROCESS_HEARTBEAT_INTERVAL = 15.0

# Probe the hook end-to-end this often — but only when the user has been
# genuinely active recently (see _recent_real_activity), so probes never
# keep an idle machine awake.
_PROBE_INTERVAL = 45.0

# How long after a probe injection we wait for our own hook to see it.
_PROBE_TIMEOUT = 1.5

# Real user activity within this window enables periodic probing.
_ACTIVITY_WINDOW = 90.0


def _subprocess_worker(shortcut: str, trigger_queue, signal_queue,
                       shutdown_flag, log_file_path: str, parent_pid: int):
    """Runs in a dedicated Python process. Owns a WH_KEYBOARD_LL hook via
    the `keyboard` library and emits events back to the parent.

    Events emitted to `trigger_queue` (parent consumes):
        "TRIGGER"                                   — hotkey pressed
        ("HEARTBEAT", presses, probes_sent, probes_ok) — liveness beacon

    Signals received on `signal_queue` (parent produces):
        "PROBE_NOW" — verify the hook end-to-end immediately
                      (sent after Windows unlock, resume, button-click hint)

    Recovery contract: if the end-to-end probe shows the OS hook is dead,
    we exit with EXIT_HOOK_DEAD. Re-adding hotkeys in-process CANNOT revive
    a removed hook (the keyboard library calls SetWindowsHookEx exactly once
    per process), so a fresh process is the only real fix — the parent
    respawns us.

    We also watch the parent process and self-exit when it dies, so hard
    kills (Task Scheduler, crashes) can never leave an orphan hook behind.
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
    logger.info("Fallback hotkey subprocess starting, shortcut=%r parent=%d",
                shortcut, parent_pid)
    logger.info("Python: %s  Platform: %s",
                os.sys.version.split()[0], platform.platform())

    ensure_process_responsiveness(logger)

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # --- Parent-death watch: no more orphan hooks -----------------
    def _parent_watch():
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if handle:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        else:
            # Can't open the parent — poll for its existence instead.
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            while True:
                h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                         False, parent_pid)
                if not h:
                    break
                code = wintypes.DWORD()
                alive = (kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                         and code.value == 259)  # STILL_ACTIVE
                kernel32.CloseHandle(h)
                if not alive:
                    break
                time.sleep(5.0)
        logger.warning("Parent process %d died — exiting to avoid orphan hook",
                       parent_pid)
        os._exit(0)

    threading.Thread(target=_parent_watch, daemon=True,
                     name="parent-watch").start()

    try:
        import keyboard
    except ImportError as e:
        logger.critical("'keyboard' module unavailable in subprocess: %s", e)
        return

    press_count = 0
    probes_sent = 0
    probes_ok = 0
    probe_seen = threading.Event()

    def on_trigger():
        nonlocal press_count
        press_count += 1
        logger.debug("Hotkey pressed (#%d) — enqueueing TRIGGER", press_count)
        try:
            trigger_queue.put("TRIGGER", block=False)
        except Exception as e:
            logger.error("Queue put failed: %s", e)

    # Global observer: powers the liveness probe. Runs inside the same
    # LL hook as the hotkey, so seeing the probe proves the hotkey works.
    # PRIVACY: we only match the probe key — key contents are never
    # inspected beyond that and never logged.
    def _observer(event):
        try:
            if getattr(event, 'name', None) == 'f24':
                probe_seen.set()
        except Exception:
            pass

    try:
        keyboard.hook(_observer)
        keyboard.add_hotkey(shortcut, on_trigger, suppress=False)
        logger.info("Hook installed for %r (+ probe observer)", shortcut)
    except Exception as e:
        logger.critical("Initial hook install failed: %s — subprocess exiting",
                        e, exc_info=True)
        return

    def do_probe(reason: str) -> Optional[bool]:
        """End-to-end hook check. Returns True/False, or None if the
        session can't receive input right now (locked) — not a failure."""
        nonlocal probes_sent, probes_ok
        if not _session_input_available(user32):
            logger.debug("Probe (%s) skipped — session locked", reason)
            return None
        for attempt in (1, 2):
            probe_seen.clear()
            probes_sent += 1
            if not _send_probe_key(user32):
                logger.warning("Probe SendInput rejected (error %d)",
                               ctypes.get_last_error())
                return None  # can't inject — inconclusive, don't kill ourselves
            if probe_seen.wait(timeout=_PROBE_TIMEOUT):
                probes_ok += 1
                logger.debug("Probe OK (%s, attempt %d)", reason, attempt)
                return True
            logger.warning("Probe NOT observed (%s, attempt %d/2)", reason, attempt)
        return False

    def probe_or_die(reason: str):
        if do_probe(reason) is False:
            logger.critical(
                "Hook is DEAD (probe unseen twice, reason=%s) — exiting with "
                "code %d so the parent respawns a fresh process (the only way "
                "to get a new SetWindowsHookEx)", reason, EXIT_HOOK_DEAD,
            )
            os._exit(EXIT_HOOK_DEAD)

    # Activity tracking: GetLastInputInfo advances on ANY input including our
    # own injected probes, so input near a probe is attributed to the probe.
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _last_input_tick = [0]
    _last_probe_time = [0.0]
    _last_real_activity = [time.monotonic()]  # assume active at startup

    def _recent_real_activity() -> bool:
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if user32.GetLastInputInfo(ctypes.byref(info)):
            if info.dwTime != _last_input_tick[0]:
                _last_input_tick[0] = info.dwTime
                if time.monotonic() - _last_probe_time[0] > 2.0:
                    _last_real_activity[0] = time.monotonic()
        return (time.monotonic() - _last_real_activity[0]) < _ACTIVITY_WINDOW

    logger.info(
        "Entering main loop (heartbeat every %.0fs, probe every %.0fs while active)",
        _SUBPROCESS_HEARTBEAT_INTERVAL, _PROBE_INTERVAL,
    )

    # Validate the hook right after birth — catches born-dead installs.
    time.sleep(1.0)
    _last_probe_time[0] = time.monotonic()
    probe_or_die("initial")

    last_probe = time.monotonic()
    last_heartbeat = time.monotonic()

    while not shutdown_flag.is_set():
        try:
            signal = signal_queue.get(timeout=0.5)
        except _queue.Empty:
            signal = None
        except (EOFError, OSError) as e:
            logger.error("Signal queue broken: %s — exiting subprocess", e)
            break

        now = time.monotonic()

        if signal == "PROBE_NOW":
            logger.info("PROBE_NOW received from main process")
            _last_probe_time[0] = now
            probe_or_die("requested")
            last_probe = now

        # Periodic probe — only while the user is genuinely active, so an
        # idle machine is never kept awake by injected input.
        if now - last_probe >= _PROBE_INTERVAL and _recent_real_activity():
            _last_probe_time[0] = now
            probe_or_die("periodic")
            last_probe = now

        # Heartbeat
        if now - last_heartbeat >= _SUBPROCESS_HEARTBEAT_INTERVAL:
            try:
                trigger_queue.put(
                    ("HEARTBEAT", press_count, probes_sent, probes_ok),
                    block=False,
                )
            except Exception as e:
                logger.warning("Heartbeat put failed: %s", e)
            last_heartbeat = now

    logger.info(
        "Shutdown received — unhooking (presses=%d, probes=%d/%d ok)",
        press_count, probes_ok, probes_sent,
    )
    try:
        keyboard.unhook_all()
    except Exception as e:
        logger.error("unhook_all on exit failed: %s", e)

    logger.info("Fallback hotkey subprocess exiting cleanly")


# --- Respawn governor ---------------------------------------------

class RespawnGovernor:
    """Sliding-window rate limiter for subprocess respawns.

    Allows up to `max_events` respawns per `window` seconds; beyond that,
    `try_acquire` returns False until the window frees up. Unlike the old
    lifetime counter this NEVER permanently gives up — a hotkey listener
    that stops listening forever is the one unacceptable outcome.
    """

    def __init__(self, max_events: int = 6, window: float = 600.0):
        self.max_events = max_events
        self.window = window
        self._events: deque = deque()
        self._lock = threading.Lock()
        self._blocked_logged = False

    def try_acquire(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._events and now - self._events[0] > self.window:
                self._events.popleft()
            if len(self._events) >= self.max_events:
                if not self._blocked_logged:
                    log.critical(
                        "Respawn rate limit hit (%d in %.0fs) — backing off; "
                        "will keep retrying as the window frees up",
                        self.max_events, self.window,
                    )
                    self._blocked_logged = True
                return False
            self._events.append(now)
            self._blocked_logged = False
            return True


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

    _UPGRADE_RETRY_INTERVAL = 15.0   # seconds between upgrade attempts
    _HEARTBEAT_STALE_THRESHOLD = 60.0  # seconds without heartbeat = zombie
    _WATCHDOG_POLL_INTERVAL = 10.0   # how often the watchdog checks
    _BUTTON_HINT_DEBOUNCE = 10.0     # min seconds between button-click probes

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
        self._respawn_governor = RespawnGovernor()
        self._last_subprocess_heartbeat: float = 0.0
        self._last_subprocess_press_count: int = 0
        self._last_subprocess_probe_counts: tuple = (0, 0)
        self._last_button_hint: float = 0.0

        # Upgrade loop + watchdog
        self._upgrade_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_watchdog_tick: float = 0.0

        # Session-change listener (Windows lock/unlock detection)
        self._session_thread: Optional[threading.Thread] = None
        self._session_hwnd: Optional[int] = None

    # --- Public API ------------------------------------------------

    def start(self):
        log.info("Starting hotkey listener for shortcut=%r", self.shortcut)

        ensure_process_responsiveness()

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
                "(with end-to-end liveness probe + watchdog + session-unlock recovery) ***"
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
        high that they tried the hotkey first and it was dead. Ask the
        subprocess to verify its hook end-to-end right now; if the hook is
        dead it self-exits and the watchdog respawns it within seconds.
        """
        now = time.monotonic()
        if now - self._last_button_hint < self._BUTTON_HINT_DEBOUNCE:
            return
        self._last_button_hint = now

        mode = self.get_mode()
        if mode == HotkeyMode.FALLBACK:
            log.warning(
                "Button click while in FALLBACK mode — user may have tried "
                "the hotkey first. Requesting hook liveness probe.",
            )
            self._signal_subprocess("PROBE_NOW")

    def get_mode(self) -> HotkeyMode:
        with self._state_lock:
            return self._mode

    def get_status_description(self) -> str:
        mode = self.get_mode()
        if mode == HotkeyMode.WIN32:
            return f"{self.shortcut} — reliable (Win32)"
        if mode == HotkeyMode.FALLBACK:
            return f"{self.shortcut} — fallback (another app owns this shortcut)"
        return f"{self.shortcut} — NOT ACTIVE (recovering)"

    def diagnose(self) -> dict:
        """Synchronously probe the Win32 hotkey state. Safe from any thread.

        Returns a dict with `shortcut`, `current_mode`, `win32_probe`
        (either 'success' or 'failed (error N: NAME)'), `win32_error_code`
        (int or None), `conflict_owner` (str or None) and a list of
        `suggestions` for the user.
        """
        report = {
            "shortcut": self.shortcut,
            "current_mode": self.get_mode().value,
            "win32_probe": None,
            "win32_error_code": None,
            "conflict_owner": None,
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
                    owner = detect_nvidia_binding(self._modifiers, self._vk)
                    if owner:
                        report["conflict_owner"] = owner
                        report["suggestions"].extend([
                            f"FOUND IT: the NVIDIA App overlay owns this shortcut "
                            f"(binding: {owner}).",
                            "Fix: open NVIDIA App -> Settings -> Shortcuts and "
                            "clear or rebind that shortcut. WhisperClip will "
                            "claim the reliable Win32 path within 15 seconds.",
                        ])
                    else:
                        report["suggestions"].extend([
                            "Windows reports this shortcut is owned by another process "
                            "(ERROR_HOTKEY_ALREADY_REGISTERED).",
                            "Likely suspects: NVIDIA App / GeForce Experience, Xbox Game Bar, "
                            "ShareX, OBS Studio, AutoHotkey scripts, Steam, Discord overlay.",
                            "Tool to identify the culprit: HotKeysList from NirSoft — "
                            "https://www.nirsoft.net/utils/hotkeys_list.html",
                        ])
                    report["suggestions"].append(
                        "Quick workaround: change 'shortcut' in config.json to something "
                        "uncontested, e.g. 'ctrl+shift+space' or 'f9'. (On this machine "
                        "avoid all modifier+R combos — NVIDIA and AMD overlays hold them.)"
                    )
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
        successfully within 3 seconds, False otherwise."""
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

        if not self._win32_registered.wait(timeout=3.0):
            log.warning("Win32 registration did not complete within 3s")
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
                  self._subprocess_shutdown, log_file, os.getpid()),
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
        """Send a command to the running subprocess (e.g. PROBE_NOW)."""
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

        # Also nudge the child's signal queue so its own get() returns
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
                    self._handle_subprocess_death()
                    break
                continue
            except (EOFError, OSError) as e:
                log.warning("Subprocess queue closed: %s", e)
                self._handle_subprocess_death()
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
                if len(item) >= 4:
                    presses, probes_sent, probes_ok = item[1], item[2], item[3]
                    if (presses != self._last_subprocess_press_count
                            or (probes_sent, probes_ok) != self._last_subprocess_probe_counts):
                        log.debug(
                            "Subprocess heartbeat: presses=%d probes=%d/%d ok",
                            presses, probes_ok, probes_sent,
                        )
                    self._last_subprocess_press_count = presses
                    self._last_subprocess_probe_counts = (probes_sent, probes_ok)
            else:
                log.warning("Unknown item from subprocess queue: %r", item)

    def _handle_subprocess_death(self):
        """Respawn the subprocess after it died. Rate-limited but never
        gives up permanently."""
        proc = self._subprocess
        exitcode = proc.exitcode if proc is not None else None

        if exitcode == EXIT_HOOK_DEAD:
            log.warning(
                "Fallback subprocess exited: its OS hook was dead (probe "
                "failed). Respawning for a fresh SetWindowsHookEx.",
            )
        else:
            log.error("Fallback subprocess died unexpectedly (exit code %s)",
                      exitcode)

        # Tear down remaining plumbing for the dead process.
        self._subprocess = None
        self._subprocess_queue = None
        self._subprocess_shutdown = None
        self._subprocess_signal_queue = None

        with self._state_lock:
            if self._mode == HotkeyMode.FALLBACK:
                self._mode = HotkeyMode.UNAVAILABLE

        if self._shutdown.is_set():
            return

        if not self._respawn_governor.try_acquire():
            # The watchdog re-attempts on its next poll; nothing else to do.
            return

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
        """Recover from every known stuck state:

        - FALLBACK with a stale heartbeat -> kill + respawn the subprocess
          (rate-limited by the governor, never a permanent give-up).
        - UNAVAILABLE with no live subprocess -> start one (the upgrade
          loop separately keeps retrying the Win32 path).
        - System resume: a large gap in our own poll cadence means the
          machine was suspended — the subprocess's heartbeat is stale
          because it was suspended too, NOT because it died. Reset the
          baseline and probe the hook instead of burning a respawn.
        """
        self._last_watchdog_tick = time.monotonic()
        while not self._shutdown.wait(timeout=self._WATCHDOG_POLL_INTERVAL):
            now = time.monotonic()
            gap = now - self._last_watchdog_tick
            self._last_watchdog_tick = now

            if gap > 3 * self._WATCHDOG_POLL_INTERVAL:
                log.info(
                    "System resume detected (watchdog gap %.0fs) — resetting "
                    "heartbeat baseline and probing the hook", gap,
                )
                self._last_subprocess_heartbeat = now
                if self.get_mode() == HotkeyMode.FALLBACK:
                    self._signal_subprocess("PROBE_NOW")
                continue

            mode = self.get_mode()

            if mode == HotkeyMode.UNAVAILABLE:
                alive = self._subprocess is not None and self._subprocess.is_alive()
                if not alive and self._respawn_governor.try_acquire(now):
                    log.warning("Mode is UNAVAILABLE — watchdog starting fallback subprocess")
                    self._start_subprocess()
                continue

            if mode != HotkeyMode.FALLBACK:
                continue
            if self._last_subprocess_heartbeat == 0:
                continue  # subprocess hasn't sent its first heartbeat yet
            age = now - self._last_subprocess_heartbeat
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
                if not self._shutdown.is_set() and self._respawn_governor.try_acquire():
                    self._start_subprocess()

    # --- Session listener (Windows lock/unlock) -----------------

    def _session_listener_loop(self):
        """Create a message-only window and receive WM_WTSSESSION_CHANGE.

        When Windows returns from lock, sleep, or fast user switch, the
        session transition may have silently detached the fallback's
        low-level hook. Probing on unlock detects that within ~2 s —
        exactly when the user is about to start dictating again.
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
                        "Session event (wparam=0x%x) — requesting hook probe",
                        wparam,
                    )
                    if self.get_mode() == HotkeyMode.FALLBACK:
                        self._signal_subprocess("PROBE_NOW")
                    # UNAVAILABLE is handled by the watchdog within 10 s.
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
                "relying on watchdog + activity-gated probes only", err,
            )
            user32.DestroyWindow(hwnd)
            self._session_hwnd = None
            return

        log.info("Session listener registered — will probe hook on unlock/logon/connect")

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
                # Sanity check 1: if the thread died while we were "WIN32",
                # drop back to fallback.
                if self._win32_thread is None or not self._win32_thread.is_alive():
                    log.warning("Win32 thread died while in WIN32 mode — "
                                "falling back to subprocess")
                    with self._state_lock:
                        self._mode = HotkeyMode.UNAVAILABLE
                    self._start_subprocess()
                    continue
                # Sanity check 2: a late registration success may have left
                # the subprocess running alongside — every press would fire
                # twice (instant start+stop). Stop the duplicate.
                if self._subprocess is not None and self._subprocess.is_alive():
                    log.warning("Win32 active but fallback subprocess still "
                                "running — stopping the duplicate")
                    self._stop_subprocess()
                    with self._state_lock:
                        self._mode = HotkeyMode.WIN32
                continue

            # Mode is FALLBACK or UNAVAILABLE — try to upgrade
            log.debug("Upgrade attempt: retrying RegisterHotKey")
            if self._try_start_win32():
                log.info("*** Upgraded to Win32 hotkey path — stopping subprocess ***")
                self._stop_subprocess()
                with self._state_lock:
                    self._mode = HotkeyMode.WIN32

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


# --- Conflict-owner detection -------------------------------------

# NVIDIA overlay hotkey names -> human-readable descriptions.
_NVIDIA_BINDING_NAMES = {
    "PMOCOverlayCycle": "Cycle performance overlay stats",
    "PMOCOverlay": "Toggle performance overlay",
    "PMOCOverlayVisibility": "Show/hide performance overlay",
    "OpenIGO": "Open overlay",
    "DVRToggle": "Instant replay toggle",
    "DVRSave": "Save instant replay",
    "RecordToggle": "Record toggle",
    "MicToggle": "Microphone toggle",
    "FreestylePresentCycle": "Cycle game filters",
}

# Win32 hotkey modifier bits -> the VK codes NVIDIA's log lists.
_MODIFIER_VKS = {_MOD_ALT: 18, _MOD_CONTROL: 17, _MOD_SHIFT: 16}


def detect_nvidia_binding(modifiers: int, vk: int) -> Optional[str]:
    """Best-effort check of the NVIDIA overlay's own log for a hotkey
    binding matching ours. Returns a human-readable description or None.

    NVIDIA Overlay's console.log contains lines like:
        ... HotkeyService  Hot key mapping for PMOCOverlayCycle : [ 18, 16, 82 ]
    where the list is VK codes (18=Alt, 16=Shift, 17=Ctrl, 82='R').
    """
    try:
        path = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "NVIDIA Corporation", "NVIDIA Overlay", "console.log",
        )
        if not os.path.isfile(path):
            return None

        ours = {mvk for mbit, mvk in _MODIFIER_VKS.items() if modifiers & mbit}
        ours.add(vk)

        pattern = re.compile(
            r"Hot key mapping for (\w+)\s*:\s*\[\s*([\d,\s]+)\]"
        )
        best = None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pattern.search(line)
                if not m:
                    continue
                name = m.group(1)
                vks = {int(x) for x in m.group(2).split(",") if x.strip()}
                if vks == ours:
                    best = name  # keep the LAST match (most recent mapping)
        if best:
            desc = _NVIDIA_BINDING_NAMES.get(best, best)
            return f"{best} — \"{desc}\""
        return None
    except Exception as e:
        log.debug("detect_nvidia_binding failed: %s", e)
        return None
