import sys
import threading
import queue
import subprocess
import os
import json
import time
from multiprocessing import Process, Queue as MPQueue

# Try to import PyQt5, but make it optional
PYQT5_AVAILABLE = False
try:
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer
    PYQT5_AVAILABLE = True
except ImportError:
    pass  # Audio visualization will be disabled if PyQt5 is not installed


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
        except:
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
        
    def _run_visualizer(self):
        """Run the visualizer in a separate process"""
        if not PYQT5_AVAILABLE:
            return
            
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
                    elif command == 'quit':
                        app.quit()
            except:
                pass
                
        # Check for commands periodically
        timer = QTimer()
        timer.timeout.connect(check_commands)
        timer.start(10)  # Check every 10ms
        
        # Start hidden - will show when needed
        visualizer.hide()
        app.exec_()