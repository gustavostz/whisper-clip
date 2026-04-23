import os
import sys

# On Windows, register NVIDIA DLL paths (cuDNN/cuBLAS) before CTranslate2 loads.
# This MUST run before any import that could transitively load ctranslate2.
if sys.platform == "win32":
    from pathlib import Path
    import site

    _nvidia_dirs = [
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",
        Path(site.getusersitepackages()) / "nvidia",
    ]
    _dll_paths = []
    for _nvidia_dir in _nvidia_dirs:
        if _nvidia_dir.is_dir():
            for _pkg in _nvidia_dir.iterdir():
                _bin = _pkg / "bin"
                if _bin.is_dir():
                    _dll_paths.append(str(_bin))

    if _dll_paths:
        os.environ["PATH"] = os.pathsep.join(_dll_paths) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            for _path in _dll_paths:
                os.add_dll_directory(_path)

import tkinter as tk
import json
import logging
import threading
from datetime import datetime


def setup_logging(enabled):
    """Configure logging to file and console. No-op when disabled."""
    logger = logging.getLogger("whisperclip")
    logger.setLevel(logging.DEBUG)

    if not enabled:
        logger.addHandler(logging.NullHandler())
        return

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler (one file per day)
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"whisper-clip_{datetime.now():%Y-%m-%d}.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Catch unhandled exceptions
    def handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = handle_exception

    def handle_thread_exception(args):
        logger.critical("Unhandled exception in thread '%s'", args.thread.name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = handle_thread_exception

    logger.info("Logging initialized — log file: %s", log_file)


def _start_server(whisper_client, port, api_key, llm_context_prefix_default):
    """Start the FastAPI server in a daemon thread.

    Errors are logged but never propagated — the desktop app keeps running
    even if the server fails to start (e.g. missing dependencies).
    """
    log = logging.getLogger("whisperclip")
    try:
        import uvicorn
        from server import create_app
    except ImportError as e:
        log.error(
            "Cannot start API server — missing dependency: %s. "
            "Install with: pip install fastapi uvicorn[standard] python-multipart", e
        )
        return

    try:
        fastapi_app = create_app(whisper_client, api_key, llm_context_prefix_default)

        uvi_config = uvicorn.Config(
            app=fastapi_app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            log_config=None,  # Use our own logging; uvicorn's default crashes when sys.stdout is None (no console)
        )
        server = uvicorn.Server(uvi_config)
        # Signals must be handled on the main thread (Tkinter), not here
        server.install_signal_handlers = lambda: None

        thread = threading.Thread(target=server.run, name="api-server", daemon=True)
        thread.start()

        log.info("API server started on 0.0.0.0:%d", port)
    except Exception as e:
        log.error("Failed to start API server: %s", e, exc_info=True)


def main():
    # Load configurations from the config file
    if not os.path.exists('config.json'):
        print(
            "ERROR: config.json not found.\n"
            "\n"
            "To get started:\n"
            "  1. Copy the example config:  cp config.example.json config.json\n"
            "  2. Edit config.json with your settings\n"
            "\n"
            "See the README for full configuration details:\n"
            "  https://github.com/gustavostz/whisper-clip#configuration"
        )
        sys.exit(1)

    with open('config.json', 'r') as config_file:
        config = json.load(config_file)

    # Set default values for missing keys (if you want to change it, you must change the config.json file, not here)
    default_config = {
        'model_name': 'turbo',
        'shortcut': 'alt+shift+r',
        'notify_clipboard_saving': True,
        'llm_context_prefix': True,
        'compute_type': 'int8',
        'hotwords': '',
        'debug_logs': False,
        'server_enabled': False,
        'server_port': 8787,
        'server_api_key': '',
    }
    config = {**default_config, **config}

    setup_logging(config.pop('debug_logs'))

    # Extract server config (pop so they don't get passed to AudioRecorder)
    server_enabled = config.pop('server_enabled')
    server_port = config.pop('server_port')
    server_api_key = config.pop('server_api_key')

    from audio_recorder import AudioRecorder

    root = tk.Tk()
    app = AudioRecorder(root, **config)

    # Start API server in daemon thread if enabled
    if server_enabled:
        if not server_api_key:
            log = logging.getLogger("whisperclip")
            log.error(
                "server_enabled=true but server_api_key is empty. "
                "Generate a key with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        else:
            _start_server(app.transcriber, server_port, server_api_key, config.get('llm_context_prefix', True))

    root.mainloop()


if __name__ == "__main__":
    # Required on Windows for multiprocessing-spawned subprocesses
    # (e.g. the hotkey fallback listener and the audio visualizer).
    import multiprocessing
    multiprocessing.freeze_support()
    main()
