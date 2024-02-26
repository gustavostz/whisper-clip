import tkinter as tk
from audio_recorder import AudioRecorder


def main():
    root = tk.Tk()
    app = AudioRecorder(root)
    root.mainloop()


if __name__ == "__main__":
    main()


