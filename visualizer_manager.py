import sys
import time
import logging
import importlib.util
from multiprocessing import Process, Queue as MPQueue

log = logging.getLogger("whisperclip")

# Check if PyQt5 is installed WITHOUT importing it (importing PyQt5 + tkinter
# in the same process causes a segfault when CTranslate2 initializes CUDA)
PYQT5_AVAILABLE = importlib.util.find_spec("PyQt5") is not None


class VisualizerManager:
    """Manages the audio visualizer as a separate process"""

    def __init__(self):
        self.process = None
        self.communication_queue = MPQueue() if PYQT5_AVAILABLE else None
        self.is_running = False
        self.enabled = PYQT5_AVAILABLE

    def start(self):
        """Start the visualizer process"""
        if not self.enabled:
            return

        if self.process is None or not self.process.is_alive():
            log.debug("Starting visualizer process")
            self.process = Process(target=self._run_visualizer)
            self.process.daemon = True
            self.process.start()
            self.is_running = True
            time.sleep(1.0)  # Give the visualizer time to fully initialize

    def stop(self):
        """Stop the visualizer process"""
        if not self.enabled:
            return

        if self.process and self.process.is_alive():
            log.debug("Stopping visualizer process")
            self.send_command('quit')
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
            self.is_running = False

    def send_command(self, command, data=None):
        """Send command to visualizer process"""
        if not self.enabled or not self.communication_queue:
            return

        try:
            self.communication_queue.put_nowait({'command': command, 'data': data})
        except Exception:
            pass

    def update_audio_level(self, level):
        """Update audio level in visualizer"""
        self.send_command('update_level', level)

    def start_loading(self):
        """Notify visualizer to show loading state"""
        if not self.enabled:
            return

        if not self.is_running:
            self.start()
        self.send_command('start_loading')

    def start_recording(self):
        """Notify visualizer that recording started"""
        if not self.enabled:
            return

        if not self.is_running:
            self.start()
        self.send_command('start_recording')

    def stop_recording(self):
        """Notify visualizer that recording stopped"""
        self.send_command('stop_recording')

    def start_transcription(self):
        """Notify visualizer that transcription started"""
        self.send_command('start_transcription')


    def stop_transcription(self):
        """Notify visualizer that transcription completed"""
        self.send_command('stop_transcription')

    def _run_visualizer(self):
        """Run the visualizer in a separate process (PyQt5 is only imported here)"""
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        from audio_visualizer import AudioVisualizer

        app = QApplication(sys.argv)
        visualizer = AudioVisualizer()

        # Set up communication
        def check_commands():
            try:
                while not self.communication_queue.empty():
                    msg = self.communication_queue.get_nowait()
                    command = msg.get('command')
                    data = msg.get('data')

                    if command == 'update_level':
                        visualizer.update_audio_level(data)
                    elif command == 'start_loading':
                        visualizer.start_loading()
                    elif command == 'start_recording':
                        visualizer.start_recording()
                    elif command == 'stop_recording':
                        visualizer.stop_recording()
                    elif command == 'start_transcription':
                        visualizer.start_transcription()
                    elif command == 'stop_transcription':
                        visualizer.stop_transcription()
                    elif command == 'quit':
                        app.quit()
            except Exception:
                pass

        # Check for commands periodically
        timer = QTimer()
        timer.timeout.connect(check_commands)
        timer.start(10)  # Check every 10ms

        # Start hidden - will show when needed
        visualizer.hide()
        app.exec_()
