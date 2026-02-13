import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pyperclip
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import threading
import queue
import time
import os
from datetime import datetime
from whisper_client import WhisperClient
import keyboard
from pystray import Icon, MenuItem, Menu
from PIL import Image
import platform
from visualizer_manager import VisualizerManager


class AudioRecorder:
    def __init__(self, master, model_name="medium.en", shortcut="alt+shift+r", notify_clipboard_saving=True, llm_context_prefix=True):
        self.system_platform = platform.system()
        self.output_folder = "output"
        self.master = master
        self.master.title("WhisperClip")
        self.master.geometry("200x150")
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

        # Create main frame for better layout control
        main_frame = tk.Frame(self.master, bg="white")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Top frame for the file selection button
        top_frame = tk.Frame(main_frame, bg="white", height=25)
        top_frame.pack(fill=tk.X, padx=5, pady=(5, 0))

        # File selection button - positioned in the top-right corner
        self.file_button = tk.Button(top_frame, text="📁", command=self.select_audio_file,
                                    font=("Arial", 12), bg="#f0f0f0", fg="#666",
                                    relief=tk.FLAT, cursor="hand2", padx=5, pady=2)
        self.file_button.pack(side=tk.RIGHT)

        # Hover effects for file button
        def on_button_enter(e):
            self.file_button.config(bg="#e0e0e0")
            # Show tooltip
            tooltip = tk.Toplevel()
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{e.x_root + 10}+{e.y_root + 10}")
            label = tk.Label(tooltip, text="Select audio file to transcribe", justify=tk.LEFT,
                           background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                           font=("Arial", 9))
            label.pack()
            self.file_button.tooltip = tooltip

        def on_button_leave(e):
            self.file_button.config(bg="#f0f0f0")
            # Hide tooltip
            if hasattr(self.file_button, 'tooltip'):
                self.file_button.tooltip.destroy()
                del self.file_button.tooltip

        self.file_button.bind("<Enter>", on_button_enter)
        self.file_button.bind("<Leave>", on_button_leave)

        # Center frame for record button
        center_frame = tk.Frame(main_frame, bg="white")
        center_frame.pack(expand=True, fill=tk.BOTH)

        self.record_button = tk.Button(center_frame, text="🎙", command=self.toggle_recording,
                                      font=("Arial", 24), bg="white", relief=tk.RAISED,
                                      cursor="hand2")
        self.record_button.pack(expand=True)

        # Bottom frame for checkbox
        bottom_frame = tk.Frame(main_frame, bg="white")
        bottom_frame.pack(fill=tk.X, pady=(0, 5))

        self.save_to_clipboard = tk.BooleanVar(value=True)
        self.clipboard_checkbox = tk.Checkbutton(bottom_frame, text="Save to Clipboard",
                                                variable=self.save_to_clipboard, bg="white")
        self.clipboard_checkbox.pack()

        self.llm_context_prefix = tk.BooleanVar(value=llm_context_prefix)
        self.llm_prefix_checkbox = tk.Checkbutton(bottom_frame, text="LLM Context Prefix",
                                                  variable=self.llm_context_prefix, bg="white")
        self.llm_prefix_checkbox.pack()

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
        
        # Stop recording in visualizer (it will transition to transcription state)
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
                
                # Show transcription progress
                self.visualizer_manager.start_transcription()
                
                # Transcribe
                transcription = self.transcriber.transcribe(filename)
                self.transcription_queue.task_done()
                
                # Show success animation first
                self.visualizer_manager.stop_transcription()
                
                if self.save_to_clipboard.get():
                    if self.llm_context_prefix.get():
                        transcription = "[Transcribed via speech-to-text (Whisper). Some words may be inaccurate — please interpret based on context.]\n\n" + transcription
                    pyperclip.copy(transcription)
                    if self.notify_clipboard_saving:
                        # Delay audio notification to sync with visual feedback
                        threading.Timer(0.3, self.play_notification_sound).start()
                
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

    def select_audio_file(self):
        """Open file dialog to select an audio file for transcription"""
        # Get the absolute path to the output folder
        output_path = os.path.abspath(self.output_folder)

        # Ensure the output folder exists
        if not os.path.exists(output_path):
            os.makedirs(output_path, exist_ok=True)

        # Open file dialog
        file_path = filedialog.askopenfilename(
            title="Select Audio File to Transcribe",
            initialdir=output_path,
            filetypes=[
                ("WAV files", "*.wav"),
                ("All files", "*.*")
            ]
        )

        if file_path:
            # Verify it's a valid audio file
            if not file_path.lower().endswith('.wav'):
                messagebox.showwarning("Invalid File", "Please select a WAV audio file.")
                return

            # Extract timestamp from filename if possible for display
            filename = os.path.basename(file_path)
            timestamp_info = ""
            if filename.startswith("audio_") and filename.endswith(".wav"):
                try:
                    # Extract timestamp from filename (audio_TIMESTAMP.wav)
                    timestamp_str = filename[6:-4]  # Remove "audio_" and ".wav"
                    timestamp = int(timestamp_str)
                    readable_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                    timestamp_info = f" (Recorded: {readable_time})"
                except:
                    pass  # If parsing fails, just continue without timestamp info

            # Show confirmation
            result = messagebox.askyesno(
                "Transcribe Audio",
                f"Do you want to transcribe this file?\n\n{filename}{timestamp_info}\n\n" +
                "You can preview the audio using your system's media player before confirming."
            )

            if result:
                # Start model loading
                self.model_ready.clear()
                self.model_loading_thread = threading.Thread(target=self.load_model_async)
                self.model_loading_thread.start()

                # Show visualizer in transcription state
                self.visualizer_manager.start_loading()
                threading.Timer(1.0, self.visualizer_manager.start_transcription).start()

                # Add to transcription queue
                self.transcription_queue.put((file_path, self.model_loading_thread))

                # Show success message
                messagebox.showinfo(
                    "Processing",
                    f"The file has been queued for transcription.\n" +
                    "The transcription will be copied to your clipboard when complete."
                )
