#!/usr/bin/env python3
"""
AI Gesture Controller - Fluid Mouse, Volume, Scroll, and Drag Control
"""
import sys
import cv2
import mediapipe as mp
import math
import pyautogui
import time
import json
import numpy as np

# --- Disable PyAutoGUI Fail-Safe ---
pyautogui.FAILSAFE = False

# --- Volume control imports (Windows specific) ---
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _volume_control_available = True
except ImportError:
    _volume_control_available = False
    print("Warning: pycaw or comtypes not found. Volume control will be disabled.")

# --- Configuration Loading ---
def load_config(config_file="config.json"):
    """Loads configuration from a JSON file."""
    default_config = {
        "general": {"gesture_cooldown": 2.5, "feedback_duration": 1.5},
        "mouse": {"smoothing": 0.7, "pinch_threshold_click": 35, "drag_hold_duration": 0.2, "scroll_speed_multiplier": 30},
        "volume": {"pinch_threshold_min": 40, "pinch_threshold_max": 150},
        "gestures": {"quit_open_palm": {"enabled": True, "key": "quit"}, "toggle_control_mode": {"enabled": True, "key": "toggle_control"}},
        "actions": {"quit": {"type": "system", "command": "quit"}, "toggle_control": {"type": "system", "command": "toggle_control_mode"}}
    }
    try:
        with open(config_file, 'r') as f: config = json.load(f)
        print(f"Configuration loaded from {config_file}")
        return config
    except Exception as e:
        print(f"Warning: {e}. Using default settings."); return default_config

# --- Main Application Class ---
class GestureControllerApp:
    def __init__(self, window_title="AI Gesture Controller"):
        self.window_title = window_title
        
        # --- Load Config & Constants ---
        self.config = load_config()
        self.gesture_cooldown = self.config.get("general", {}).get("gesture_cooldown", 2.0)
        self.feedback_duration = self.config.get("general", {}).get("feedback_duration", 1.5)
        self.smoothing = self.config.get("mouse", {}).get("smoothing", 0.7)
        self.pinch_threshold = self.config.get("mouse", {}).get("pinch_threshold_click", 35)
        self.drag_hold_duration = self.config.get("mouse", {}).get("drag_hold_duration", 0.2)
        self.scroll_speed = self.config.get("mouse", {}).get("scroll_speed_multiplier", 30)
        self.vol_pinch_min = self.config.get("volume", {}).get("pinch_threshold_min", 40)
        self.vol_pinch_max = self.config.get("volume", {}).get("pinch_threshold_max", 150)

        # --- Initialize System Controls ---
        self.volume_interface, self.minVol, self.maxVol = None, None, None
        self.volume_enabled = False
        if _volume_control_available:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self.volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
                volRange = self.volume_interface.GetVolumeRange()
                self.minVol, self.maxVol = volRange[0], volRange[1]
                self.volume_enabled = True
                print("Volume control initialized.")
            except Exception as e: print(f"Warning: Volume control initialization failed: {e}")

        # --- State Variables ---
        self.current_mode = 'normal'
        self.last_gesture_time = 0
        self.feedback_text = ""; self.feedback_text_end_time = 0
        self.is_quitting = False; self.quit_trigger_time = 0
        self.is_pinching = False; self.pinch_start_time = 0; self.is_dragging = False
        self.is_scrolling = False; self.last_y = 0

        # --- Mouse Control Variables ---
        self.screen_width, self.screen_height = pyautogui.size()
        self.prev_x, self.prev_y = 0, 0

        # --- MediaPipe Initialization ---
        self.hands_detector = mp.solutions.hands.Hands(max_num_hands=2, min_detection_confidence=0.7, min_tracking_confidence=0.7)

        # --- Webcam Initialization ---
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened(): print("Error: Could not open webcam."); sys.exit()
        self.cam_w, self.cam_h = 1280, 720
        self.cap.set(3, self.cam_w); self.cap.set(4, self.cam_h)
        
    def is_finger_up(self, hand_landmarks, tip_idx, pip_idx):
        try: return hand_landmarks.landmark[tip_idx].y < hand_landmarks.landmark[pip_idx].y
        except: return False

    def are_all_fingers_up(self, hand_landmarks):
        return all([self.is_finger_up(hand_landmarks, *f) for f in [(4,2),(8,6),(12,10),(16,14),(20,18)]])

    def set_feedback_text(self, text):
        self.feedback_text = text; self.feedback_text_end_time = time.time() + self.feedback_duration

    def draw_centered_text(self, image, text, color=(255, 255, 255)):
        h, w, _ = image.shape; font_scale = 2; font_thickness = 3
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, font_thickness)
        tx = (w - tw) // 2; ty = (h + th) // 2
        overlay = image.copy(); cv2.rectangle(overlay, (tx - 20, ty - th - 20), (tx + tw + 20, ty + 20), (0, 0, 0), -1)
        alpha = 0.6; cv2.addWeighted(overlay, alpha, image, 1 - alpha, 0, image)
        cv2.putText(image, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, font_thickness)

    def execute_action(self, action_key):
        action_config = self.config.get("actions", {}).get(action_key, {}); action_type = action_config.get("type")
        if action_type == "system":
            command = action_config.get("command")
            if command == "quit" and not self.is_quitting: self.is_quitting = True; self.quit_trigger_time = time.time()
            elif command == "toggle_control_mode":
                self.current_mode = 'control' if self.current_mode == 'normal' else 'normal'
                feedback = f"Control Mode {'ON' if self.current_mode == 'control' else 'OFF'}"
                self.set_feedback_text(feedback); print(feedback)

    def run(self):
        """The main application loop."""
        print("Starting AI Gesture Controller. Raise both index fingers to toggle Control Mode.")
        is_running = True
        while self.cap.isOpened() and is_running:
            success, image = self.cap.read()
            if not success: print("Failed to grab frame."); break
            
            image = cv2.flip(image, 1)
            
            if self.is_quitting:
                time_elapsed = time.time() - self.quit_trigger_time
                if time_elapsed < 2: self.draw_centered_text(image, f"Quitting in {math.ceil(2 - time_elapsed)}...", (0, 0, 255))
                elif time_elapsed < 2.7: self.draw_centered_text(image, "Thank You!", (0, 255, 0))
                else: is_running = False; continue
            else:
                rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB); results = self.hands_detector.process(rgb_image)
                
                if results.multi_hand_landmarks:
                    for hand_landmarks in results.multi_hand_landmarks:
                        mp.solutions.drawing_utils.draw_landmarks(image, hand_landmarks, mp.solutions.hands.HAND_CONNECTIONS)
                    
                    if len(results.multi_hand_landmarks) == 2 and (time.time() - self.last_gesture_time) > self.gesture_cooldown:
                        h1, h2 = results.multi_hand_landmarks
                        h1_index_up, h1_others_down = self.is_finger_up(h1, 8, 6), not any([self.is_finger_up(h1, *f) for f in [(12,10),(16,14),(20,18)]])
                        h2_index_up, h2_others_down = self.is_finger_up(h2, 8, 6), not any([self.is_finger_up(h2, *f) for f in [(12,10),(16,14),(20,18)]])
                        if self.config.get("gestures",{}).get("toggle_control_mode",{}).get("enabled") and h1_index_up and h1_others_down and h2_index_up and h2_others_down:
                            self.execute_action("toggle_control"); self.last_gesture_time = time.time()
                    
                    hand_landmarks = results.multi_hand_landmarks[0]
                    
                    if self.current_mode == 'normal':
                        cv2.putText(image, "Normal Mode", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                        if (time.time() - self.last_gesture_time) > self.gesture_cooldown:
                            if self.config.get("gestures",{}).get("quit_open_palm",{}).get("enabled") and self.are_all_fingers_up(hand_landmarks):
                                self.execute_action("quit"); self.last_gesture_time = time.time()

                    elif self.current_mode == 'control':
                        index_tip = hand_landmarks.landmark[8]; thumb_tip = hand_landmarks.landmark[4]
                        index_up = self.is_finger_up(hand_landmarks, 8, 6); middle_up = self.is_finger_up(hand_landmarks, 12, 10); thumb_up = self.is_finger_up(hand_landmarks, 4, 2)
                        
                        # --- FLUID CONTROL LOGIC ---
                        if index_up and middle_up: # SCROLL (using index + middle fingers)
                            cv2.putText(image, "Scroll Mode", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                            if not self.is_scrolling: 
                                self.is_scrolling = True
                                self.last_y = index_tip.y
                            delta_y = index_tip.y - self.last_y
                            scroll_amount = int(-delta_y * self.scroll_speed * 5)  # Much higher sensitivity
                            if abs(delta_y) > 0.002:  # Very low threshold for ultra-responsive scrolling
                                pyautogui.scroll(scroll_amount)
                            self.last_y = index_tip.y
                        
                        elif middle_up and thumb_up and not index_up: # SWITCH TABS (pinky + middle up)
                            cv2.putText(image, "Tab Switch Mode", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2)
                            if not hasattr(self, 'last_tab_time') or time.time() - getattr(self, 'last_tab_time', 0) > 0.5:
                                # Determine direction based on hand movement
                                pinky_tip = hand_landmarks.landmark[20]
                                if not hasattr(self, 'last_pinky_x'):
                                    self.last_pinky_x = pinky_tip.x
                                
                                delta_x = pinky_tip.x - self.last_pinky_x
                                if abs(delta_x) > 0.02:  # Horizontal movement threshold
                                    if delta_x > 0:
                                        pyautogui.hotkey('ctrl', 'tab')  # Next tab
                                        print("Next Tab")
                                        self.set_feedback_text("Next Tab")
                                    else:
                                        pyautogui.hotkey('ctrl', 'shift', 'tab')  # Previous tab
                                        print("Previous Tab")
                                        self.set_feedback_text("Previous Tab")
                                    self.last_tab_time = time.time()
                                self.last_pinky_x = pinky_tip.x
                        
                        elif index_up and thumb_up: # VOLUME
                            self.is_scrolling = False
                            cv2.putText(image, "Volume Control", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                            if self.volume_enabled and self.minVol is not None:
                                distance = math.hypot((thumb_tip.x - index_tip.x) * self.cam_w, (thumb_tip.y - index_tip.y) * self.cam_h)
                                clamped_dist = max(self.vol_pinch_min, min(distance, self.vol_pinch_max))
                                vol_val = np.interp(clamped_dist, [self.vol_pinch_min, self.vol_pinch_max], [self.minVol, self.maxVol])
                                self.volume_interface.SetMasterVolumeLevel(vol_val, None)
                                vol_percent = int(np.interp(vol_val, [self.minVol, self.maxVol], [0, 100]))
                                cv2.rectangle(image, (30, 100), (65, 300), (255, 0, 0), 3)
                                bar_height = np.interp(vol_percent, [0, 100], [300, 100])
                                cv2.rectangle(image, (30, int(bar_height)), (65, 300), (255, 0, 0), cv2.FILLED)

                        elif index_up: # MOUSE POINTER
                            self.is_scrolling = False
                            cv2.putText(image, "Mouse Pointer", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
                            x = np.interp(index_tip.x, [0.1, 0.9], [0, self.screen_width])
                            y = np.interp(index_tip.y, [0.1, 0.9], [0, self.screen_height])
                            curr_x = self.prev_x + (x - self.prev_x) * self.smoothing; curr_y = self.prev_y + (y - self.prev_y) * self.smoothing
                            pyautogui.moveTo(curr_x, curr_y)
                            cv2.circle(image, (int(index_tip.x * self.cam_w), int(index_tip.y * self.cam_h)), 15, (255, 0, 255), cv2.FILLED)
                            self.prev_x, self.prev_y = curr_x, curr_y
                        
                        else: self.is_scrolling = False
                        
                        # --- CLICK AND DRAG LOGIC (Works in any Control sub-mode) ---
                        distance = math.hypot((thumb_tip.x - index_tip.x) * self.cam_w, (thumb_tip.y - index_tip.y) * self.cam_h)
                        if distance < self.pinch_threshold:
                            if not self.is_pinching: self.is_pinching = True; self.pinch_start_time = time.time()
                            elif (time.time() - self.pinch_start_time) > self.drag_hold_duration and not self.is_dragging:
                                self.is_dragging = True; pyautogui.mouseDown(); print("Drag Started"); self.set_feedback_text("Dragging...")
                        else:
                            if self.is_dragging: pyautogui.mouseUp(); print("Drag Released"); self.set_feedback_text("Dropped!")
                            elif self.is_pinching: pyautogui.click(); print("Click!"); self.set_feedback_text("Click!")
                            self.is_pinching = False; self.is_dragging = False

            if time.time() < self.feedback_text_end_time: self.draw_centered_text(image, self.feedback_text)
            cv2.imshow(self.window_title, image)
            if cv2.waitKey(1) & 0xFF == ord('q'): 
                print("Quitting by key press...")
                break

        self.cap.release()
        cv2.destroyAllWindows()
        print("AI Gesture Controller stopped.")

# --- Entry Point ---
if __name__ == "__main__":
    try:
        app = GestureControllerApp()
        app.run()
    except KeyboardInterrupt:
        print("\nApplication interrupted by user.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
