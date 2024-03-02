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
        'model_name': 'medium',
        'shortcut': 'alt+shift+r',
        'notify_clipboard_saving': True
    }
    config = {**default_config, **config}

    app = AudioRecorder(root, **config)
    root.mainloop()


if __name__ == "__main__":
    main()
