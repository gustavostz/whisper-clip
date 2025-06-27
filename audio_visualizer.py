import sys
import numpy as np
from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QRect, QPointF
from PyQt5.QtGui import QPainter, QColor, QLinearGradient, QPen, QPainterPath, QBrush, QFont
import collections
import random
import math


class AudioVisualizer(QWidget):
    """Modern audio waveform visualizer with smooth animations"""
    
    close_signal = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self.initUI()
        self.audio_levels = collections.deque(maxlen=100)  # Store last 100 audio samples for denser waveform
        self.target_levels = collections.deque(maxlen=100)
        self.smoothed_levels = collections.deque(maxlen=100)
        
        # Initialize with zeros
        for _ in range(100):
            self.audio_levels.append(0.0)
            self.target_levels.append(0.0)
            self.smoothed_levels.append(0.0)
        
        # Animation timer
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self.animate_levels)
        self.animation_timer.start(16)  # ~60 FPS
        
        # Update timer for redrawing
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update)
        self.update_timer.start(16)  # ~60 FPS
        
        self.is_recording = False
        self.is_loading = False
        self.opacity = 0.0
        self.target_opacity = 0.0
        self.loading_animation_value = 0.0  # For pulsing animation
        
    def initUI(self):
        # Window setup
        self.setWindowTitle('Audio Visualizer')
        self.resize(600, 120)  # Wider window for better waveform display
        
        # Make window frameless and always on top
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | 
            Qt.FramelessWindowHint | 
            Qt.Tool |
            Qt.WindowTransparentForInput
        )
        
        # Enable transparency
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        
        # Position at bottom center of screen
        self.center_at_bottom()
        
    def center_at_bottom(self):
        """Position window at bottom center of primary screen"""
        screen = QApplication.primaryScreen()
        screen_geometry = screen.geometry()
        
        # Calculate position
        x = (screen_geometry.width() - self.width()) // 2
        y = screen_geometry.height() - self.height() - 50  # 50px from bottom
        
        self.move(x, y)
        
    def update_audio_level(self, level):
        """Update audio level data"""
        # Normalize level to 0-1 range
        normalized_level = min(1.0, max(0.0, level))
        
        # Add some natural variation to make waveform look more realistic
        if normalized_level > 0.05:  # Only add variation for non-silent audio
            # Add small random variation (±20% of the level)
            variation = random.uniform(-0.2, 0.2) * normalized_level
            normalized_level = max(0.0, min(1.0, normalized_level + variation))
        
        self.target_levels.append(normalized_level)
        
    def start_loading(self):
        """Called when user initiates recording (shows loading state)"""
        self.is_loading = True
        self.is_recording = False
        self.target_opacity = 1.0
        self.opacity = 0.0  # Start from transparent
        self.show()
        self.raise_()  # Bring to front
        self.activateWindow()  # Make sure it's active
        self.update()  # Force immediate update
        
    def start_recording(self):
        """Called when actual recording starts (transitions from loading to recording)"""
        self.is_loading = False
        self.is_recording = True
        self.target_opacity = 1.0
        if not self.isVisible():
            self.show()
        self.update()  # Force update
        
    def stop_recording(self):
        """Called when recording stops"""
        self.is_recording = False
        self.is_loading = False
        self.target_opacity = 0.0
        # Timer will hide window when opacity reaches 0
        
    def animate_levels(self):
        """Smooth animation of audio levels"""
        # Animate opacity
        opacity_speed = 0.1
        if self.opacity < self.target_opacity:
            self.opacity = min(self.target_opacity, self.opacity + opacity_speed)
        elif self.opacity > self.target_opacity:
            self.opacity = max(self.target_opacity, self.opacity - opacity_speed)
            
        # Hide window when fully transparent
        if self.opacity <= 0.0 and not self.is_recording and not self.is_loading:
            self.hide()
            self.close_signal.emit()
            
        # Animate loading pulse
        if self.is_loading:
            self.loading_animation_value += 0.05
            if self.loading_animation_value > 2 * 3.14159:  # Full cycle
                self.loading_animation_value = 0.0
            
        # Smooth audio level transitions (less aggressive for more responsive waveform)
        smoothing_factor = 0.5  # Higher value = more responsive
        for i in range(len(self.smoothed_levels)):
            current = self.smoothed_levels[i]
            target = self.target_levels[i]
            self.smoothed_levels[i] = current + (target - current) * smoothing_factor
            
        # Decay levels when not recording
        if not self.is_recording:
            for i in range(len(self.target_levels)):
                self.target_levels[i] *= 0.95
                
    def paintEvent(self, event):
        """Custom paint event for waveform visualization"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Set overall opacity
        painter.setOpacity(self.opacity)
        
        # Draw background with gradient
        self.draw_background(painter)
        
        # Draw content based on state
        if self.is_loading:
            self.draw_loading(painter)
        else:
            self.draw_waveform(painter)
        
    def draw_background(self, painter):
        """Draw gradient background with purple tint"""
        rect = self.rect()
        
        # Create gradient with purple tint
        gradient = QLinearGradient(0, 0, 0, rect.height())
        gradient.setColorAt(0, QColor(30, 20, 40, 180))  # Dark purple tint
        gradient.setColorAt(1, QColor(20, 10, 30, 200))  # Darker purple tint
        
        # Draw rounded rectangle
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(rect, 15, 15)
        
        # Draw subtle purple border
        painter.setPen(QPen(QColor(120, 80, 150, 100), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 15, 15)
        
    def draw_loading(self, painter):
        """Draw loading animation"""
        rect = self.rect()
        center = rect.center()
        
        # Calculate pulsing effect
        pulse = (math.sin(self.loading_animation_value) + 1) / 2  # 0 to 1
        
        # Purple color with pulsing intensity
        base_intensity = 0.6
        intensity = base_intensity + pulse * 0.4
        r = int(147 * intensity)
        g = int(51 * intensity)
        b = int(234 * intensity)
        
        # Draw microphone icon in center
        mic_size = 30
        mic_rect = QRect(center.x() - mic_size//2, center.y() - mic_size, mic_size, mic_size)
        
        # Draw microphone body
        painter.setPen(QPen(QColor(r, g, b, 255), 3))
        painter.setBrush(QBrush(QColor(r, g, b, 100)))
        
        # Microphone top (circle)
        painter.drawEllipse(center.x() - mic_size//4, center.y() - mic_size, mic_size//2, mic_size//2)
        
        # Microphone body (rectangle)
        painter.drawRoundedRect(center.x() - mic_size//4, center.y() - mic_size//2, 
                              mic_size//2, mic_size//2, 5, 5)
        
        # Microphone stand
        painter.drawLine(center.x(), center.y(), center.x(), center.y() + mic_size//3)
        painter.drawLine(center.x() - mic_size//4, center.y() + mic_size//3, 
                        center.x() + mic_size//4, center.y() + mic_size//3)
        
        # Draw loading dots below microphone
        dot_y = center.y() + mic_size//2 + 20
        dot_radius = 3
        dot_spacing = 15
        
        for i in range(3):
            dot_x = center.x() - dot_spacing + i * dot_spacing
            
            # Animate dots with wave effect
            dot_offset = math.sin(self.loading_animation_value + i * 0.5) * 3
            dot_alpha = int(100 + 155 * ((math.sin(self.loading_animation_value + i * 0.5) + 1) / 2))
            
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(r, g, b, dot_alpha)))
            painter.drawEllipse(QPointF(dot_x, dot_y + dot_offset), dot_radius, dot_radius)
        
        # Draw "Loading..." text
        painter.setPen(QPen(QColor(r, g, b, 200), 1))
        font = QFont("Arial", 10)
        painter.setFont(font)
        text = "Preparing to record..."
        text_rect = painter.fontMetrics().boundingRect(text)
        text_x = center.x() - text_rect.width() // 2
        text_y = rect.bottom() - 15
        painter.drawText(text_x, text_y, text)
        
    def draw_waveform(self, painter):
        """Draw the audio waveform as symmetrical bars"""
        rect = self.rect()
        padding = 20
        waveform_rect = rect.adjusted(padding, padding, -padding, -padding)
        
        # Calculate center line
        center_y = waveform_rect.center().y()
        
        # Draw center line (optional - very faint)
        painter.setPen(QPen(QColor(100, 50, 150, 30), 1))  # Very faint purple line
        painter.drawLine(waveform_rect.left(), center_y, waveform_rect.right(), center_y)
        
        # Calculate bar dimensions for dense waveform
        bar_count = len(self.smoothed_levels)
        total_width = waveform_rect.width()
        # Calculate exact spacing for bars to fit perfectly
        total_bar_width = total_width / bar_count
        bar_width = max(3, int(total_bar_width * 0.7))  # 70% bar, 30% gap
        bar_spacing = total_bar_width - bar_width
        
        # Purple color scheme
        base_purple_r = 147  # Base purple red component
        base_purple_g = 51   # Base purple green component  
        base_purple_b = 234  # Base purple blue component
        
        # Draw each bar
        for i, level in enumerate(self.smoothed_levels):
            if level < 0.02:  # Skip very quiet samples
                continue
                
            # Calculate bar position with floating point accuracy
            x = waveform_rect.left() + i * total_bar_width
            
            # Calculate bar height (amplitude)
            amplitude = level * waveform_rect.height() * 0.45  # 45% of rect height max
            
            # Intensity variation based on level
            intensity = 0.5 + level * 0.5
            r = int(base_purple_r * intensity)
            g = int(base_purple_g * intensity)
            b = int(base_purple_b * intensity)
            
            # Create gradient for each bar
            bar_gradient = QLinearGradient(x + bar_width/2, center_y - amplitude, 
                                         x + bar_width/2, center_y + amplitude)
            bar_gradient.setColorAt(0, QColor(r, g, b, 180))  # Top fade
            bar_gradient.setColorAt(0.45, QColor(r, g, b, 255))  # Near center more opaque
            bar_gradient.setColorAt(0.5, QColor(r, g, b, 255))  # Center fully opaque
            bar_gradient.setColorAt(0.55, QColor(r, g, b, 255))  # Near center more opaque
            bar_gradient.setColorAt(1, QColor(r, g, b, 180))  # Bottom fade
            
            # Draw the mirrored bar (extends both up and down from center)
            painter.setBrush(QBrush(bar_gradient))
            painter.setPen(Qt.NoPen)
            
            # Draw main bar
            bar_rect = QRect(int(x), int(center_y - amplitude), 
                           int(bar_width), int(amplitude * 2))
            painter.drawRoundedRect(bar_rect, bar_width//4, bar_width//4)
            
            # Add glow effect for louder sounds
            if level > 0.3:
                glow_alpha = int(30 * level)  # Stronger glow for louder sounds
                glow_pen = QPen(QColor(r, g, b, glow_alpha), 2)
                painter.setPen(glow_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(bar_rect.adjusted(-1, -1, 1, 1), bar_width//4, bar_width//4)
                
    def closeEvent(self, event):
        """Clean up timers on close"""
        self.animation_timer.stop()
        self.update_timer.stop()
        event.accept()


if __name__ == '__main__':
    # Test the visualizer
    app = QApplication(sys.argv)
    visualizer = AudioVisualizer()
    
    # First show loading state
    visualizer.start_loading()
    
    # After 3 seconds, transition to recording
    def start_recording_test():
        visualizer.start_recording()
        
        # Simulate audio levels with more realistic patterns
        def update_test_levels():
            # Simulate speech-like patterns
            base_level = random.uniform(0.2, 0.6)
            # Add variations to simulate natural speech
            for _ in range(3):  # Multiple updates per cycle
                variation = random.uniform(-0.2, 0.3)
                level = max(0.1, min(0.9, base_level + variation))
                visualizer.update_audio_level(level)
            
        test_timer = QTimer()
        test_timer.timeout.connect(update_test_levels)
        test_timer.start(50)
    
    QTimer.singleShot(3000, start_recording_test)  # Transition after 3 seconds
    
    sys.exit(app.exec_())