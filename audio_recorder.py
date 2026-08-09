import tkinter as tk
from tkinter import filedialog, messagebox
import pyperclip
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import threading
import queue
import time
import os
import logging
from datetime import datetime
from whisper_client import WhisperClient
from pystray import Icon, MenuItem, Menu
from PIL import Image
import platform
from visualizer_manager import VisualizerManager
from hotkey_listener import HotkeyListener

log = logging.getLogger("whisperclip")


class AudioRecorder:
    def __init__(self, master, model_name="turbo", shortcut="alt+shift+s", notify_clipboard_saving=True, llm_context_prefix=True, compute_type="int8", hotwords=""):
        self.system_platform = platform.system()
        self.output_folder = "output"
        self.master = master
        self.master.title("WhisperClip")
        self.master.geometry("200x150")
        # self.master.iconbitmap('./assets/whisper_clip-centralized.ico')

        self.is_recording = False
        self.recordings = []
        self.transcription_queue = queue.Queue()
        self.transcriber = WhisperClient(model_name=model_name, compute_type=compute_type, hotwords=hotwords)
        self.keep_transcribing = True
        self.shortcut = shortcut
        self.notify_clipboard_saving = notify_clipboard_saving
        self._toggle_lock = threading.Lock()
        self._recorded_samplerate = 44100

        # Cross-thread UI marshaling: Tkinter must only be touched from the
        # main thread. Worker/tray/hotkey threads enqueue callables here and
        # a recurring after() pump runs them on the main loop.
        self._ui_queue = queue.Queue()
        self.master.after(50, self._drain_ui_queue)

        # Lazy IVirtualDesktopManager helper (created on the main thread).
        self._vdh_holder = [None]

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
        self.file_button = tk.Button(top_frame, text="\U0001f4c1", command=self.select_audio_file,
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

        self.record_button = tk.Button(center_frame, text="\U0001f399",
                                      command=self._toggle_recording_from_button,
                                      font=("Arial", 24), bg="white", relief=tk.RAISED,
                                      cursor="hand2")
        self.record_button.pack(expand=True)

        shortcut_label = tk.Label(center_frame, text=self.shortcut, font=("Arial", 8),
                                  fg="#999999", bg="white")
        shortcut_label.pack(pady=(0, 5))

        # Bottom frame for checkbox
        bottom_frame = tk.Frame(main_frame, bg="white")
        bottom_frame.pack(fill=tk.X, pady=(0, 5))

        # Plain-bool mirrors of the checkbox state: BooleanVar.get() is a Tcl
        # call and must not run on worker threads (the transcription thread
        # reads these). The command callbacks run on the main thread.
        self._save_to_clipboard_flag = True
        self._llm_prefix_flag = llm_context_prefix

        # Packed top-to-bottom: LLM Context Prefix sits above Save to Clipboard
        self.llm_context_prefix = tk.BooleanVar(value=llm_context_prefix)
        self.llm_prefix_checkbox = tk.Checkbutton(bottom_frame, text="LLM Context Prefix",
                                                  variable=self.llm_context_prefix, bg="white",
                                                  command=self._sync_checkbox_flags)
        self.llm_prefix_checkbox.pack()

        self.save_to_clipboard = tk.BooleanVar(value=True)
        self.clipboard_checkbox = tk.Checkbutton(bottom_frame, text="Save to Clipboard",
                                                variable=self.save_to_clipboard, bg="white",
                                                command=self._sync_checkbox_flags)
        self.clipboard_checkbox.pack()

        # Daemon: exit_application joins with a timeout; a busy transcription
        # must not be able to keep a half-dead headless process alive forever.
        self.transcription_thread = threading.Thread(target=self.process_transcriptions,
                                                     daemon=True)
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

        log.info("WhisperClip started (model=%s, compute_type=%s, shortcut=%s)",
                 model_name, compute_type, shortcut)

    def _ui(self, fn):
        """Schedule a callable to run on the Tk main thread."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                log.error("UI callback failed: %s", e, exc_info=True)
        self.master.after(50, self._drain_ui_queue)

    def _sync_checkbox_flags(self):
        self._save_to_clipboard_flag = self.save_to_clipboard.get()
        self._llm_prefix_flag = self.llm_context_prefix.get()

    def _preload_model(self):
        """Pre-load model in background thread to reduce latency after recording stops."""
        log.debug("Model pre-loading started")
        try:
            self.transcriber.load_model()
            log.debug("Model pre-loading finished")
        except Exception as e:
            log.error("Model pre-loading failed: %s", e, exc_info=True)

    def toggle_recording(self):
        log.debug("toggle_recording triggered")
        # Run in a separate thread to avoid blocking the hotkey listener or GUI
        threading.Thread(target=self._toggle_recording, daemon=True).start()

    def _toggle_recording_from_button(self):
        """Called when the UI record button is clicked. If the hotkey
        listener is in fallback mode, the click is a strong signal that
        the hotkey was dead — we pass that hint to the listener so it can
        force-refresh its hook."""
        log.debug("toggle_recording triggered (from UI button click)")
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.notice_button_click()
            except Exception as e:
                log.error("notice_button_click failed: %s", e, exc_info=True)
        threading.Thread(target=self._toggle_recording, daemon=True).start()

    def _toggle_recording(self):
        if not self._toggle_lock.acquire(blocking=False):
            log.debug("Toggle ignored — already in progress")
            return
        try:
            if self.is_recording:
                self.stop_recording()
            else:
                self.start_recording()
        finally:
            self._toggle_lock.release()

    def _input_device_name(self, device=None):
        """Name of the mic PortAudio is (or would be) recording from.

        With no argument this reports the current default input device; pass an
        open stream's `device` for the one actually in use.
        """
        try:
            if device is None:
                info = sd.query_devices(kind='input')
            else:
                # InputStream.device is an int; a duplex Stream gives (in, out)
                index = device[0] if isinstance(device, (list, tuple)) else device
                info = sd.query_devices(index)
            return str(info.get('name', '')).strip() or None
        except Exception as e:
            log.debug("Could not resolve input device name: %s", e)
            return None

    def start_recording(self):
        log.info("Recording started")
        self.is_recording = True
        self._ui(lambda: self.record_button.config(bg="red"))

        # Show visualizer in loading state immediately
        self.visualizer_manager.start_loading()
        # Best-effort name up front; record_audio corrects it once the stream
        # is open and the real device is known.
        self.visualizer_manager.set_input_device(self._input_device_name())

        # Pre-load model in background to reduce latency when transcription starts.
        # The lock in WhisperClient prevents conflicts if model is already loaded
        # or being unloaded by the transcription thread.
        threading.Thread(target=self._preload_model, daemon=True).start()

        # Start recording immediately
        self.record_thread = threading.Thread(target=self.record_audio, daemon=True)
        self.record_thread.start()

    def stop_recording(self):
        self.is_recording = False
        self._ui(lambda: self.record_button.config(bg="white"))
        # Bounded join: a wedged PortAudio stream close (seen after suspend /
        # default-device changes) must not hold the toggle lock forever —
        # that would make every future hotkey press a silent no-op.
        self.record_thread.join(timeout=5.0)
        if self.record_thread.is_alive():
            log.error("Record thread did not stop within 5s — continuing "
                      "without it (audio stream may be wedged)")
        log.info("Recording stopped")

        # Stop recording in visualizer (it will transition to transcription state)
        self.visualizer_manager.stop_recording()

        if self.recordings:
            audio_data = np.concatenate(self.recordings)
            audio_data = (audio_data * 32767).astype(np.int16)
            os.makedirs(self.output_folder, exist_ok=True)
            filename = f"{self.output_folder}/audio_{int(time.time())}.wav"
            write(filename, self._recorded_samplerate, audio_data)
            self.recordings = []
            log.info("Audio saved: %s", filename)
            self.transcription_queue.put(filename)
        else:
            log.warning("No audio data recorded. Check your audio input device.")

    def play_notification_sound(self):
        sound_file = './assets/saved-on-clipboard-sound.wav'

        if self.system_platform == 'Windows':
            import winsound
            winsound.PlaySound(sound_file, winsound.SND_FILENAME)
        elif self.system_platform == 'Darwin':  # MacOS
            os.system(f'afplay {sound_file}')
        else:
            log.warning("Unsupported platform for notification sound: %s", self.system_platform)

    def process_transcriptions(self):
        while self.keep_transcribing:
            try:
                filename = self.transcription_queue.get(timeout=1)

                try:
                    # Show transcription progress
                    self.visualizer_manager.start_transcription()

                    # Transcribe (loads model internally if not already loaded)
                    log.info("Transcribing: %s", filename)
                    transcription = self.transcriber.transcribe(filename)
                    self.transcription_queue.task_done()
                    log.info("Transcription complete (%d chars)", len(transcription))

                    # Show success animation first
                    self.visualizer_manager.stop_transcription()

                    if self._save_to_clipboard_flag:
                        if self._llm_prefix_flag:
                            transcription = "[Transcribed via speech-to-text (Whisper). Some words may be inaccurate \u2014 please interpret based on context.]\n\n" + transcription
                        pyperclip.copy(transcription)
                        log.debug("Transcription copied to clipboard")
                        if self.notify_clipboard_saving:
                            # Delay audio notification to sync with visual feedback
                            threading.Timer(0.3, self.play_notification_sound).start()
                except Exception as e:
                    log.error("Transcription error: %s", e, exc_info=True)
                    self.visualizer_manager.stop_transcription()
                finally:
                    # Unload model after transcription to free GPU memory
                    self.transcriber.unload_model()

            except queue.Empty:
                continue
            except Exception as e:
                # Catch-all so the transcription thread never dies silently
                log.critical("Unexpected error in transcription thread: %s", e, exc_info=True)
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
        log.debug("Window closed (hidden to tray)")
        self.master.withdraw()  # Hide the window

    def record_audio(self):
        # Transition visualizer from loading to recording state
        self.visualizer_manager.start_recording()

        try:
            with sd.InputStream(callback=self.audio_callback) as stream:
                # The WAV must be written at the device's actual rate —
                # hardcoding 44100 on a 48 kHz device pitch-shifts the audio
                # and wrecks the transcription.
                self._recorded_samplerate = int(stream.samplerate)

                device_name = self._input_device_name(stream.device)
                log.info("Recording from input device: %s (%d Hz)",
                         device_name or "unknown", self._recorded_samplerate)
                self.visualizer_manager.set_input_device(device_name)

                while self.is_recording:
                    sd.sleep(100)
        except Exception as e:
            log.error("Audio input stream failed: %s", e, exc_info=True)
            self.is_recording = False
            self._ui(lambda: self.record_button.config(bg="white"))
            self.visualizer_manager.stop_recording()

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
        """Install the global hotkey. Delegates to HotkeyListener on Windows;
        falls back to the keyboard library on other platforms."""
        if self.system_platform == 'Windows':
            self.hotkey_listener = HotkeyListener(
                shortcut=self.shortcut,
                on_trigger=self.toggle_recording,
                log_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
            )
            self.hotkey_listener.start()
        else:
            self.hotkey_listener = None
            try:
                import keyboard
                keyboard.add_hotkey(self.shortcut, self.toggle_recording)
                log.info("Global hotkey registered (non-Windows path): %s", self.shortcut)
            except Exception as e:
                log.error("Failed to register global hotkey '%s': %s",
                          self.shortcut, e, exc_info=True)

    def diagnose_hotkey(self):
        """Show a diagnostic dialog explaining the hotkey state. Invoked
        from the tray menu. Helps the user figure out which app is
        blocking RegisterHotKey when we're stuck in fallback mode."""
        if self.hotkey_listener is None:
            self._ui(lambda: messagebox.showinfo(
                "Hotkey Diagnostic",
                f"Platform: {self.system_platform}\n"
                f"Shortcut: {self.shortcut}\n\n"
                "Hotkey diagnostics are only available on Windows."
            ))
            return

        report = self.hotkey_listener.diagnose()
        log.info("Hotkey diagnostic report: %s", report)

        lines = [
            f"Shortcut:       {report['shortcut']}",
            f"Current mode:   {report['current_mode']}",
            f"Win32 probe:    {report['win32_probe']}",
            "",
            "Suggestions:",
        ]
        if report["suggestions"]:
            lines.extend(f"  • {s}" for s in report["suggestions"])
        else:
            lines.append("  (none)")

        # This runs on the tray thread; dialogs must be created on Tk's
        # main thread or Tcl state can corrupt.
        self._ui(lambda: messagebox.showinfo("Hotkey Diagnostic", "\n".join(lines)))

    def setup_system_tray(self):
        # Load the icon image from a file
        icon_image = Image.open('./assets/whisper_clip-centralized.png')

        # Define the menu items
        menu = Menu(
            MenuItem('Toggle Recording (' + self.shortcut + ')', self.toggle_recording),
            MenuItem('Diagnose Hotkey', self.diagnose_hotkey),
            MenuItem('Show Window', self.show_window, default=True, visible=False),
            MenuItem('Exit', self.exit_application)
        )

        # Create and run the system tray icon
        self.icon = Icon('WhisperClip', icon_image, 'WhisperClip', menu)
        self.icon.run_detached()

    def show_window(self):
        # pystray callbacks run on the tray thread — marshal to Tk.
        self._ui(self._show_window_on_main)

    def _show_window_on_main(self):
        """Show the window on the CURRENT virtual desktop instead of letting
        Windows yank the user to whichever desktop the window lived on."""
        if self.system_platform == 'Windows':
            from virtual_desktop import bring_window_to_current_desktop
            bring_window_to_current_desktop(self.master, self._vdh_holder)
        else:
            self.master.deiconify()
            self.master.lift()

    def exit_application(self):
        log.info("Application exiting")
        self.is_recording = False  # release a live record loop
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception as e:
                log.error("Error stopping hotkey listener: %s", e, exc_info=True)
        self.keep_transcribing = False
        self.transcription_thread.join(timeout=10.0)
        if self.transcription_thread.is_alive():
            log.warning("Transcription thread still busy at exit — not waiting")
        self.visualizer_manager.stop()
        self.icon.stop()
        # quit() must run on the main thread; this is called from the tray.
        self._ui(self.master.quit)

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
                except Exception:
                    pass  # If parsing fails, just continue without timestamp info

            # Show confirmation
            result = messagebox.askyesno(
                "Transcribe Audio",
                f"Do you want to transcribe this file?\n\n{filename}{timestamp_info}\n\n" +
                "You can preview the audio using your system's media player before confirming."
            )

            if result:
                log.info("File selected for transcription: %s", file_path)

                # Pre-load model
                threading.Thread(target=self._preload_model, daemon=True).start()

                # Show visualizer in transcription state (no mic involved)
                self.visualizer_manager.set_input_device(None)
                self.visualizer_manager.start_loading()
                threading.Timer(1.0, self.visualizer_manager.start_transcription).start()

                # Add to transcription queue
                self.transcription_queue.put(file_path)

                # Show success message
                messagebox.showinfo(
                    "Processing",
                    f"The file has been queued for transcription.\n" +
                    "The transcription will be copied to your clipboard when complete."
                )
