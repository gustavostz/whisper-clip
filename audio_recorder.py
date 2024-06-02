import tkinter as tk
from tkinter import ttk
import pyperclip
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import threading
import queue
import time
import os
from whisper_client import WhisperClient
import keyboard
from pystray import Icon, MenuItem, Menu
from PIL import Image
import platform


class AudioRecorder:
    def __init__(self, master, model_name="medium.en", shortcut="alt+shift+r", notify_clipboard_saving=True):
        self.system_platform = platform.system()
        self.output_folder = "output"
        self.master = master
        self.master.title("WhisperClip")
        self.master.geometry("200x100")
        # self.master.iconbitmap('./assets/whisper_clip-centralized.ico')

        self.is_recording = False
        self.recordings = []
        self.transcription_queue = queue.Queue()
        self.transcriber = WhisperClient(model_name=model_name)
        self.keep_transcribing = True
        self.shortcut = shortcut
        self.notify_clipboard_saving = notify_clipboard_saving

        self.record_button = tk.Button(self.master, text="🎙", command=self.toggle_recording, font=("Arial", 24),
                                       bg="white")
        self.record_button.pack(expand=True)

        self.save_to_clipboard = tk.BooleanVar(value=False)
        self.clipboard_checkbox = tk.Checkbutton(self.master, text="Save to Clipboard", variable=self.save_to_clipboard)
        self.clipboard_checkbox.pack()

        self.transcription_thread = threading.Thread(target=self.process_transcriptions)
        self.transcription_thread.start()

        # Set up the global shortcut and system tray icon
        self.setup_global_shortcut()
        self.setup_system_tray()

        # Stop all processes when the window is closed
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)

    def toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        self.is_recording = True
        self.record_button.config(bg="red")
        self.record_thread = threading.Thread(target=self.record_audio)
        self.record_thread.start()

    def stop_recording(self):
        self.is_recording = False
        self.record_button.config(bg="white")
        sd.stop()
        self.record_thread.join()
        if self.recordings:
            audio_data = np.concatenate(self.recordings)
            audio_data = (audio_data * 32767).astype(np.int16)
            os.makedirs(self.output_folder, exist_ok=True)
            filename = f"{self.output_folder}/audio_{int(time.time())}.wav"
            write(filename, 44100, audio_data)
            self.recordings = []
            self.transcription_queue.put(filename)
        else:
            print("No audio data recorded. Please check your audio input device.")

    def play_notification_sound(self):
        sound_file = './assets/saved-on-clipboard-sound.wav'

        if self.system_platform == 'Windows':
            import winsound
            winsound.PlaySound(sound_file, winsound.SND_FILENAME)
        elif self.system_platform == 'Darwin':  # MacOS
            os.system(f'afplay {sound_file}')
        else:
            print(f'Unsupported platform. Please open an issue to request support for your operating system. System: '
                  f'{self.system_platform}')

    def process_transcriptions(self):
        while self.keep_transcribing:
            try:
                filename = self.transcription_queue.get(timeout=1)
                transcription = self.transcriber.transcribe(filename)
                print(f"Transcription for {filename}:", transcription)
                self.transcription_queue.task_done()
                if self.save_to_clipboard.get():
                    pyperclip.copy(transcription)
                    # Play a notification sound
                    if self.notify_clipboard_saving:
                        self.play_notification_sound()
            except queue.Empty:
                continue

    def on_close(self):
        self.master.withdraw()  # Hide the window

    def record_audio(self):
        with sd.InputStream(callback=self.audio_callback):
            while self.is_recording:
                sd.sleep(1000)

    def audio_callback(self, indata, frames, time, status):
        self.recordings.append(indata.copy())

    def setup_global_shortcut(self):
        # Use the shortcut passed during initialization
        keyboard.add_hotkey(self.shortcut, self.toggle_recording)

    def setup_system_tray(self):
        # Load the icon image from a file
        icon_image = Image.open('./assets/whisper_clip-centralized.png')

        # Define the menu items
        menu = Menu(
            MenuItem('Toggle Recording (' + self.shortcut + ')', self.toggle_recording),
            MenuItem('Show Window', self.show_window, default=True, visible=False),
            MenuItem('Exit', self.exit_application)
        )

        # Create and run the system tray icon
        self.icon = Icon('WhisperClip', icon_image, 'WhisperClip', menu)
        self.icon.run_detached()

    def show_window(self):
        # Show the window again
        self.master.deiconify()

    def exit_application(self):
        self.keep_transcribing = False
        self.transcription_thread.join()
        self.icon.stop()
        self.master.quit();
