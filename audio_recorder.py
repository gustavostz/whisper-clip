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


class AudioRecorder:
    def __init__(self, master):
        self.output_folder = "output"
        self.master = master
        self.master.title("Audio Recorder")
        self.master.geometry("200x100")

        self.is_recording = False
        self.recordings = []
        self.transcription_queue = queue.Queue()
        self.transcriber = WhisperClient()
        self.keep_transcribing = True

        self.record_button = tk.Button(self.master, text="🎙", command=self.toggle_recording, font=("Arial", 24),
                                       bg="white")
        self.record_button.pack(expand=True)

        self.save_to_clipboard = tk.BooleanVar(value=False)
        self.clipboard_checkbox = tk.Checkbutton(self.master, text="Save to Clipboard", variable=self.save_to_clipboard)
        self.clipboard_checkbox.pack()

        self.transcription_thread = threading.Thread(target=self.process_transcriptions)
        self.transcription_thread.start()

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
        audio_data = np.concatenate(self.recordings)
        audio_data = (audio_data * 32767).astype(np.int16)
        os.makedirs(self.output_folder, exist_ok=True)
        filename = f"{self.output_folder}/audio_{int(time.time())}.wav"
        write(filename, 44100, audio_data)
        self.recordings = []
        self.transcription_queue.put(filename)

    def process_transcriptions(self):
        while self.keep_transcribing:
            try:
                filename = self.transcription_queue.get(timeout=1)
                transcription = self.transcriber.transcribe(filename)
                print(f"Transcription for {filename}:", transcription)
                self.transcription_queue.task_done()
                if self.save_to_clipboard.get():
                    pyperclip.copy(transcription)
            except queue.Empty:
                continue

    def on_close(self):
        self.keep_transcribing = False
        self.transcription_thread.join()
        self.master.destroy()

    def record_audio(self):
        with sd.InputStream(callback=self.audio_callback):
            while self.is_recording:
                sd.sleep(1000)

    def audio_callback(self, indata, frames, time, status):
        self.recordings.append(indata.copy())
