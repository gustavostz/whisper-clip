"""Custom hotkey listener
"""
import os

from pynput.keyboard import Key, KeyCode

DEBUG_MODE = os.getenv("DEBUG_MODE", 'false').lower() == 'true'

class HotkeyListener:
    def __init__(self, shortcut, callback):
        self.shortcut = shortcut
        self.callback = callback
        self.current_keys = set()
        
        # Parse the shortcut into individual keys
        self.shortcut_keys = self.parse_shortcut(shortcut)
        
    def parse_shortcut(self, shortcut):
        # Implement this based on your shortcut format
        # add more conditions if other keys are used
        str_shortcut_list = shortcut.split('+')
        shortcut_list = []
        for str_shortcut in str_shortcut_list:
            if str_shortcut.lower() == "space":
                value = Key.space
            elif str_shortcut.lower() == "ctrl":
                value = Key.ctrl
            elif str_shortcut.lower() == "alt":
                value = Key.alt
            elif str_shortcut.lower() == "shift":
                value = Key.shift
            else:
                value = KeyCode.from_char(str_shortcut)
            
            shortcut_list.append(value)
        print(shortcut_list)
        return shortcut_list
        
    def on_press(self, key):
        try:
            self.current_keys.add(key)
            if self.check_shortcut():
                self.callback()
        except AttributeError:
            pass
            
    def on_release(self, key):
        try:
            self.current_keys.remove(key)
        except (KeyError, AttributeError):
            pass
            
    def check_shortcut(self):
        # Check if all keys in shortcut are currently pressed

        if DEBUG_MODE:
            print(f"checking shortcut: {all(k in self.current_keys for k in self.shortcut_keys)}")
            print(f"current keys: {self.current_keys}")
            print(f"shortcut keys: {self.shortcut_keys}")
            for k in self.shortcut_keys:
                print(f"shortcut key {k} of type {type(k)} pressed: {k in self.current_keys}")

            for k in self.current_keys:
                print(f"current key {k} of type {type(k)} is part of shorcut: {k in self.shortcut_keys}")
        
        return all(k in self.current_keys for k in self.shortcut_keys)