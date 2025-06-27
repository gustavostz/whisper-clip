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
from visualizer_manager import VisualizerManager


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
        
        # Add thread management
        self.model_loading_thread = None
        self.model_ready = threading.Event()
        
        # Initialize visualizer manager
        self.visualizer_manager = VisualizerManager()
        self.audio_level_thread = None
        self.audio_level_queue = queue.Queue(maxsize=100)
        
        # Pre-start the visualizer process so it's ready immediately
        self.visualizer_manager.start()

        self.record_button = tk.Button(self.master, text="🎙", command=self.toggle_recording, font=("Arial", 24),
                                       bg="white")
        self.record_button.pack(expand=True)

        self.save_to_clipboard = tk.BooleanVar(value=True)
        self.clipboard_checkbox = tk.Checkbutton(self.master, text="Save to Clipboard", variable=self.save_to_clipboard)
        self.clipboard_checkbox.pack()

        self.transcription_thread = threading.Thread(target=self.process_transcriptions)
        self.transcription_thread.start()
        
        # Start audio level processing thread
        self.audio_level_thread = threading.Thread(target=self.process_audio_levels)
        self.audio_level_thread.daemon = True
        self.audio_level_thread.start()

        # Set up the global shortcut and system tray icon
        self.setup_global_shortcut()
        self.setup_system_tray()

        # Stop all processes when the window is closed
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_model_async(self):
        self.transcriber.load_model()
        self.model_ready.set()

    def toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        self.is_recording = True
        self.record_button.config(bg="red")
        
        # Show visualizer in loading state immediately
        self.visualizer_manager.start_loading()
        
        # Start model loading in parallel
        self.model_ready.clear()
        self.model_loading_thread = threading.Thread(target=self.load_model_async)
        self.model_loading_thread.start()
        
        # Start recording immediately
        self.record_thread = threading.Thread(target=self.record_audio)
        self.record_thread.start()

    def stop_recording(self):
        self.is_recording = False
        self.record_button.config(bg="white")
        sd.stop()
        self.record_thread.join()
        
        # Stop visualizer
        self.visualizer_manager.stop_recording()
        
        if self.recordings:
            audio_data = np.concatenate(self.recordings)
            audio_data = (audio_data * 32767).astype(np.int16)
            os.makedirs(self.output_folder, exist_ok=True)
            filename = f"{self.output_folder}/audio_{int(time.time())}.wav"
            write(filename, 44100, audio_data)
            self.recordings = []
            self.transcription_queue.put((filename, self.model_loading_thread))
        else:
            print("No audio data recorded. Please check your audio input device.")
            # If no audio was recorded, wait for model loading and unload it
            if self.model_loading_thread and self.model_loading_thread.is_alive():
                self.model_loading_thread.join()
                self.transcriber.unload_model()

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
                filename, loading_thread = self.transcription_queue.get(timeout=1)
                
                # Wait for model to be ready if it's still loading
                if loading_thread and loading_thread.is_alive():
                    self.model_ready.wait()  # Wait for model loading to complete
                
                transcription = self.transcriber.transcribe(filename)
                print(f"Transcription for {filename}:", transcription)
                self.transcription_queue.task_done()
                
                if self.save_to_clipboard.get():
                    pyperclip.copy(transcription)
                    if self.notify_clipboard_saving:
                        self.play_notification_sound()
                
                # Unload model after transcription is complete
                self.transcriber.unload_model()
                self.model_ready.clear()
                
            except queue.Empty:
                continue
                
    def process_audio_levels(self):
        """Process audio levels and send to visualizer"""
        while True:
            try:
                level = self.audio_level_queue.get(timeout=0.1)
                self.visualizer_manager.update_audio_level(level)
            except queue.Empty:
                continue

    def on_close(self):
        self.master.withdraw()  # Hide the window

    def record_audio(self):
        # Transition visualizer from loading to recording state
        self.visualizer_manager.start_recording()
        
        with sd.InputStream(callback=self.audio_callback):
            while self.is_recording:
                sd.sleep(1000)

    def audio_callback(self, indata, frames, time, status):
        self.recordings.append(indata.copy())
        
        # Calculate RMS (Root Mean Square) for audio level
        if self.is_recording and len(indata) > 0:
            # Calculate RMS level
            rms = np.sqrt(np.mean(indata**2))
            # Convert to dB and normalize (typical range -60dB to 0dB)
            db = 20 * np.log10(rms + 1e-10)  # Add small value to avoid log(0)
            normalized_level = (db + 60) / 60  # Normalize to 0-1 range
            normalized_level = max(0.0, min(1.0, normalized_level))
            
            # Send level to visualizer thread
            try:
                self.audio_level_queue.put_nowait(normalized_level)
            except queue.Full:
                pass  # Skip if queue is full

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
        self.visualizer_manager.stop()
        self.icon.stop()
        self.master.quit()
