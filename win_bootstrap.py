"""Windows process hygiene for WhisperClip startup.

Production forensics showed that hard kills (Task Scheduler battery stop,
crashes, Task Manager) orphan the multiprocessing daemon children — the old
hotkey subprocess kept installing keyboard hooks for ~24 h with a dead
parent. Three layers of defense:

1. **Single-instance mutex** — a second launch exits immediately instead of
   double-registering hotkeys and double-toggling recordings.
2. **Job object with kill-on-close** — the OS terminates every child the
   instant the main process dies, no matter how it dies. This is the real
   fix; nothing survives a TerminateProcess anymore.
3. **Orphan reaper** — one startup sweep that kills leftover
   `--multiprocessing-fork` children of dead parents from BEFORE this fix
   (or from crashes of pre-job-object builds).

All functions are best-effort: any failure is logged and startup continues.
"""
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger("whisperclip")

_ERROR_ALREADY_EXISTS = 183

# Handles that must stay alive for the whole process lifetime.
_mutex_handle = None
_job_handle = None


def acquire_single_instance(name: str = "Local\\WhisperClip.SingleInstance") -> bool:
    """Returns False if another WhisperClip instance already runs in this
    session. The mutex handle is held until the process dies."""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not handle:
            log.warning("CreateMutexW failed (error %d) — cannot enforce "
                        "single instance", err)
            return True
        if err == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception as e:
        log.warning("Single-instance check failed: %s", e)
        return True


def setup_kill_children_with_parent():
    """Assign this process to a job object with KILL_ON_JOB_CLOSE.

    Children (multiprocessing spawns) inherit job membership, and when this
    process dies — cleanly or via TerminateProcess — the kernel closes our
    job handle and terminates every process in the job. No more orphans."""
    global _job_handle
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # Explicit signatures — default int restype truncates 64-bit HANDLEs.
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                      wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            log.warning("CreateJobObjectW failed (error %d)", ctypes.get_last_error())
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("SetInformationJobObject failed (error %d)",
                        ctypes.get_last_error())
            kernel32.CloseHandle(job)
            return

        if not kernel32.AssignProcessToJobObject(
                job, kernel32.GetCurrentProcess()):
            log.warning("AssignProcessToJobObject failed (error %d) — "
                        "children may outlive a hard kill", ctypes.get_last_error())
            kernel32.CloseHandle(job)
            return

        _job_handle = job
        log.info("Job object active — children die with this process")
    except Exception as e:
        log.warning("setup_kill_children_with_parent failed: %s", e)


def _terminate_pid(pid: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        PROCESS_TERMINATE = 0x0001
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        ok = bool(kernel32.TerminateProcess(h, 1))
        kernel32.CloseHandle(h)
        return ok
    except Exception:
        return False


def reap_orphaned_children():
    """Kill leftover multiprocessing children whose parent is dead.

    Matches only: python/pythonw processes with `--multiprocessing-fork` in
    the command line, whose `parent_pid=N` refers to a process that no
    longer exists, and whose executable is the same interpreter family this
    app runs on. Anything else is left alone."""
    try:
        ps = (
            "$procs = Get-CimInstance Win32_Process "
            "-Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
            "Select-Object ProcessId, CommandLine, ExecutablePath; "
            "$all = (Get-Process).Id; "
            "@{procs=@($procs); all=@($all)} | ConvertTo-Json -Depth 3"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if out.returncode != 0 or not out.stdout.strip():
            log.debug("Orphan reaper: process query failed (%s)", out.stderr[:200])
            return
        data = json.loads(out.stdout)
        procs = data.get("procs") or []
        if isinstance(procs, dict):  # single result serializes as an object
            procs = [procs]
        alive = set(data.get("all") or [])

        # Our interpreter family: the venv exe and the base exe it wraps.
        my_exes = {
            os.path.normcase(sys.executable),
            os.path.normcase(getattr(sys, "_base_executable", sys.executable) or sys.executable),
        }

        killed = 0
        for p in procs:
            pid = p.get("ProcessId")
            cmd = p.get("CommandLine") or ""
            exe = os.path.normcase(p.get("ExecutablePath") or "")
            if not pid or pid == os.getpid():
                continue
            if "--multiprocessing-fork" not in cmd:
                continue
            m = re.search(r"parent_pid=(\d+)", cmd)
            if not m:
                continue
            parent = int(m.group(1))
            if parent in alive:
                continue  # parent still running — not an orphan
            if exe and exe not in my_exes:
                continue  # some other app's python — leave it alone
            if _terminate_pid(pid):
                killed += 1
                log.warning("Reaped orphaned child pid=%d (dead parent %d)",
                            pid, parent)
        if killed:
            log.info("Orphan reaper: killed %d stale child process(es)", killed)
    except Exception as e:
        log.warning("Orphan reaper failed: %s", e)


def fatal_error_box(title: str, message: str):
    """Last-resort visible error for pythonw (no console) startups."""
    try:
        MB_ICONERROR = 0x10
        ctypes.WinDLL('user32').MessageBoxW(None, message, title, MB_ICONERROR)
    except Exception:
        pass
