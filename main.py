import tkinter as tk
from audio_recorder import AudioRecorder
import json


def main():
    root = tk.Tk()

    # Load configurations from the config file
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)

    app = AudioRecorder(root, model_name=config['model_name'], shortcut=config['shortcut'])
    root.mainloop()


if __name__ == "__main__":
    main()
