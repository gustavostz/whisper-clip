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
        self.is_transcribing = False
        self.show_success = False
        self.opacity = 0.0
        self.target_opacity = 0.0
        self.loading_animation_value = 0.0  # For pulsing animation
        self.transcription_animation_value = 0.0  # For transcription animation
        self.success_animation_value = 0.0  # For success checkmark animation
        
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
        # Don't hide yet - will transition to transcription state
        
    def start_transcription(self):
        """Called when transcription starts"""
        self.is_transcribing = True
        self.transcription_animation_value = 0.0
        self.target_opacity = 1.0
        
        if not self.isVisible():
            self.show()
        self.update()
        
    def stop_transcription(self):
        """Called when transcription completes"""
        self.is_transcribing = False
        self.show_success = True
        self.success_animation_value = 0.0
        
        # Show success for 1.5 seconds
        QTimer.singleShot(1500, self.hide_success)
        
    def hide_success(self):
        """Hide success state and close if not recording"""
        self.show_success = False
        
        # If we're still recording, stay visible
        if self.is_recording:
            self.target_opacity = 1.0
        else:
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
        if self.opacity <= 0.0 and not self.is_recording and not self.is_loading and not self.is_transcribing:
            self.hide()
            self.close_signal.emit()
            
        # Animate loading pulse
        if self.is_loading:
            self.loading_animation_value += 0.05
            if self.loading_animation_value > 2 * 3.14159:  # Full cycle
                self.loading_animation_value = 0.0
                
        # Animate transcription
        if self.is_transcribing:
            self.transcription_animation_value += 0.03
            if self.transcription_animation_value > 2 * 3.14159:  # Full cycle
                self.transcription_animation_value = 0.0
                
        # Animate success
        if self.show_success and self.success_animation_value < 1.0:
            self.success_animation_value = min(1.0, self.success_animation_value + 0.1)
            
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
        elif self.is_recording and self.is_transcribing:
            # Show split view for concurrent operations
            self.draw_concurrent(painter)
        elif self.is_transcribing:
            self.draw_transcription(painter)
        elif self.is_recording:
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
        
    def draw_transcription(self, painter):
        """Draw transcription animation"""
        rect = self.rect()
        center = rect.center()
        
        # Purple color scheme
        base_r = 147
        base_g = 51
        base_b = 234
        
        if self.show_success:
            # Draw success checkmark
            self.draw_success_check(painter, center.x(), center.y(), base_r, base_g, base_b)
        else:
            # Draw animated dots in a wave pattern
            dot_count = 3  # Reduced from 5
            dot_radius = 5  # Slightly larger
            dot_spacing = 35  # Much more spacing
            base_y = center.y()
            
            for i in range(dot_count):
                # Calculate position and animation offset
                x = center.x() - (dot_count - 1) * dot_spacing / 2 + i * dot_spacing
                
                # Create wave effect
                wave_offset = math.sin(self.transcription_animation_value + i * 0.8) * 12
                y = base_y + wave_offset
                
                # Vary opacity for fade effect
                opacity = int(180 + 75 * math.sin(self.transcription_animation_value + i * 0.8))
                
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(base_r, base_g, base_b, opacity)))
                painter.drawEllipse(QPointF(x, y), dot_radius, dot_radius)
        
        # Draw transcription text
        painter.setPen(QPen(QColor(base_r, base_g, base_b, 255), 1))
        font = QFont("Arial", 12)
        painter.setFont(font)
        text = "Transcription complete!" if self.show_success else "Transcribing audio..."
        text_rect = painter.fontMetrics().boundingRect(text)
        painter.drawText(center.x() - text_rect.width() // 2, 
                        center.y() - 40, text)
        
        # Draw processing indicator (only when not showing success)
        if not self.show_success:
            painter.setPen(QPen(QColor(base_r, base_g, base_b, 150), 1))
            font = QFont("Arial", 10)
            painter.setFont(font)
            processing_text = "Processing with Whisper AI"
            processing_rect = painter.fontMetrics().boundingRect(processing_text)
            painter.drawText(center.x() - processing_rect.width() // 2, 
                            center.y() + 45, processing_text)
        
    def draw_concurrent(self, painter):
        """Draw view for concurrent recording and transcription"""
        rect = self.rect()
        center = rect.center()
        
        # Purple color scheme
        base_r = 147
        base_g = 51
        base_b = 234
        
        # Left side: Recording indicator
        left_center_x = rect.width() // 4
        
        # Draw recording waveform (keep better proportions)
        waveform_width = rect.width() // 2 - 40  # Half the window minus margins
        waveform_height = rect.height() - 40
        waveform_rect = QRect(20, center.y() - waveform_height // 2, waveform_width, waveform_height)
        
        # Draw mini waveform with proper scaling
        bar_count = min(30, len(self.smoothed_levels) // 2)  # Use fewer bars but maintain quality
        total_bar_width = waveform_rect.width() / bar_count
        bar_width = max(3, int(total_bar_width * 0.7))  # Similar to full waveform
        bar_spacing = total_bar_width - bar_width
        
        for i in range(bar_count):
            # Sample from smoothed levels
            index = i * len(self.smoothed_levels) // bar_count
            level = self.smoothed_levels[index] if index < len(self.smoothed_levels) else 0
            if level < 0.02:
                continue
                
            x = waveform_rect.left() + i * total_bar_width
            amplitude = level * waveform_rect.height() * 0.45  # Same as full waveform
            
            # Intensity variation based on level
            intensity = 0.5 + level * 0.5
            r = int(base_r * intensity)
            g = int(base_g * intensity)
            b = int(base_b * intensity)
            
            # Draw symmetrical bar with gradient
            bar_gradient = QLinearGradient(x + bar_width/2, center.y() - amplitude, 
                                         x + bar_width/2, center.y() + amplitude)
            bar_gradient.setColorAt(0, QColor(r, g, b, 180))
            bar_gradient.setColorAt(0.5, QColor(r, g, b, 255))
            bar_gradient.setColorAt(1, QColor(r, g, b, 180))
            
            painter.setBrush(QBrush(bar_gradient))
            painter.setPen(Qt.NoPen)
            
            bar_rect = QRect(int(x), int(center.y() - amplitude), 
                           int(bar_width), int(amplitude * 2))
            painter.drawRoundedRect(bar_rect, bar_width//4, bar_width//4)
        
        # Draw recording label
        painter.setPen(QPen(QColor(base_r, base_g, base_b, 255), 1))
        font = QFont("Arial", 9)
        painter.setFont(font)
        painter.drawText(left_center_x - 30, rect.bottom() - 10, "Recording")
        
        # Draw separator
        painter.setPen(QPen(QColor(base_r, base_g, base_b, 100), 1))
        painter.drawLine(rect.width() // 2, 20, rect.width() // 2, rect.height() - 20)
        
        # Right side: Transcription animation
        right_center_x = rect.width() * 3 // 4
        
        if self.show_success:
            # Draw smaller success checkmark
            self.draw_success_check(painter, right_center_x, center.y(), base_r, base_g, base_b, scale=0.7)
            label_text = "Complete!"
        else:
            # Draw transcription dots
            dot_count = 3
            dot_radius = 3
            dot_spacing = 12
            
            for i in range(dot_count):
                x = right_center_x - (dot_count - 1) * dot_spacing / 2 + i * dot_spacing
                wave_offset = math.sin(self.transcription_animation_value + i * 0.5) * 8
                y = center.y() + wave_offset
                
                opacity = int(150 + 105 * math.sin(self.transcription_animation_value + i * 0.5))
                
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(base_r, base_g, base_b, opacity)))
                painter.drawEllipse(QPointF(x, y), dot_radius, dot_radius)
            label_text = "Transcribing"
        
        # Draw transcription label
        painter.setPen(QPen(QColor(base_r, base_g, base_b, 255), 1))
        font = QFont("Arial", 9)
        painter.setFont(font)
        label_rect = painter.fontMetrics().boundingRect(label_text)
        painter.drawText(right_center_x - label_rect.width() // 2, rect.bottom() - 10, label_text)
        
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
                
    def draw_success_check(self, painter, center_x, center_y, r, g, b, scale=1.0):
        """Draw animated success checkmark"""
        # Scale the checkmark
        size = 30 * scale
        
        # Calculate animation progress
        progress = self.success_animation_value
        
        # Draw circle background
        circle_radius = size
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(r, g, b, int(50 * progress))))
        painter.drawEllipse(QPointF(center_x, center_y), circle_radius, circle_radius)
        
        # Draw checkmark path
        if progress > 0.2:
            check_progress = min(1.0, (progress - 0.2) / 0.8)
            
            painter.setPen(QPen(QColor(r, g, b, 255), 3 * scale, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            
            # Checkmark points
            start_x = center_x - size * 0.3
            start_y = center_y + size * 0.1
            
            mid_x = center_x - size * 0.05
            mid_y = center_y + size * 0.35
            
            end_x = center_x + size * 0.35
            end_y = center_y - size * 0.25
            
            # Draw first part of checkmark
            if check_progress > 0:
                first_progress = min(1.0, check_progress * 2)
                path = QPainterPath()
                path.moveTo(start_x, start_y)
                path.lineTo(
                    start_x + (mid_x - start_x) * first_progress,
                    start_y + (mid_y - start_y) * first_progress
                )
                painter.drawPath(path)
            
            # Draw second part of checkmark
            if check_progress > 0.5:
                second_progress = (check_progress - 0.5) * 2
                path = QPainterPath()
                path.moveTo(mid_x, mid_y)
                path.lineTo(
                    mid_x + (end_x - mid_x) * second_progress,
                    mid_y + (end_y - mid_y) * second_progress
                )
                painter.drawPath(path)
                
    def closeEvent(self, event):
        """Clean up timers on close"""
        self.animation_timer.stop()
        self.update_timer.stop()
        event.accept()


if __name__ == '__main__':
    # Test the visualizer
    app = QApplication(sys.argv)
    visualizer = AudioVisualizer()
    
    # Test sequence: loading -> recording -> transcribing -> concurrent
    visualizer.start_loading()
    
    def start_recording_test():
        visualizer.start_recording()
        
        # Simulate audio levels
        def update_test_levels():
            base_level = random.uniform(0.2, 0.6)
            for _ in range(3):
                variation = random.uniform(-0.2, 0.3)
                level = max(0.1, min(0.9, base_level + variation))
                visualizer.update_audio_level(level)
            
        test_timer = QTimer()
        test_timer.timeout.connect(update_test_levels)
        test_timer.start(50)
        
        # After 3 seconds, stop recording and start transcribing
        def stop_and_transcribe():
            visualizer.stop_recording()
            visualizer.start_transcription()
            
            # After 3 seconds, stop transcription (show success)
            def show_success():
                visualizer.stop_transcription()
                
                # After 2 seconds, start recording again
                def start_recording_again():
                    visualizer.start_recording()
                    
                    # After 2 seconds, start transcribing (concurrent mode)
                    def start_concurrent_transcription():
                        visualizer.start_transcription()
                        
                        # After 3 seconds, stop transcription (show success in concurrent mode)
                        def stop_concurrent_transcription():
                            visualizer.stop_transcription()
                        
                        QTimer.singleShot(3000, stop_concurrent_transcription)
                    
                    QTimer.singleShot(2000, start_concurrent_transcription)
                
                QTimer.singleShot(2000, start_recording_again)
            
            QTimer.singleShot(3000, show_success)
        
        QTimer.singleShot(3000, stop_and_transcribe)
    
    QTimer.singleShot(2000, start_recording_test)
    
    sys.exit(app.exec_())