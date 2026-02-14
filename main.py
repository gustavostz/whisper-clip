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
from audio_recorder import AudioRecorder
import json


def main():
    root = tk.Tk()

    # Load configurations from the config file
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)

    # Set default values for missing keys (if you want to change it, you must change the config.json file, not here)
    default_config = {
        'model_name': 'turbo',
        'shortcut': 'alt+shift+r',
        'notify_clipboard_saving': True,
        'llm_context_prefix': True,
        'compute_type': 'int8',
    }
    config = {**default_config, **config}

    app = AudioRecorder(root, **config)
    root.mainloop()


if __name__ == "__main__":
    main()
