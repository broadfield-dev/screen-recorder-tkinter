#!/usr/bin/env python3

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import cv2
import numpy as np
import mss
import mss.tools
import os
from datetime import datetime
import sys
import subprocess
import platform

# Global hotkey libraries
try:
    import keyboard
except ImportError:
    keyboard = None
    print("keyboard module not installed - global hotkey Ctrl+Shift+R disabled. pip install keyboard")

try:
    from pynput import mouse as pynput_mouse
    from pynput.mouse import Listener as MouseListener, Controller as MouseController
except ImportError:
    pynput_mouse = None
    print("pynput not installed - mouse click effects disabled. pip install pynput")

# Global mouse controller for position retrieval
mouse_controller = MouseController() if pynput_mouse else None

# ---------------------- Mouse click tracking via pynput ---------------------
class GlobalMouseTracker:
    def __init__(self):
        self.listener = None
        self.click_events = []  # list of (x, y, time)
        self.dragging = False
        self.drag_start = None
        self._running = False

    def start(self):
        if not pynput_mouse:
            return
        if self._running:
            return
        self._running = True
        self.listener = MouseListener(on_click=self._on_click, on_move=self._on_move)
        self.listener.daemon = True
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()
        self._running = False

    def _on_click(self, x, y, button, pressed):
        now = time.time()
        if pressed:
            self.click_events.append((x, y, now))
            self.dragging = True
            self.drag_start = (x, y)
        else:
            self.dragging = False
            self.drag_start = None

    def _on_move(self, x, y):
        pass

    def get_click_events(self, since_time=None):
        if since_time is None:
            return self.click_events
        return [(x,y,t) for x,y,t in self.click_events if t > since_time]

    def clear_old_events(self, before_time):
        self.click_events = [(x,y,t) for x,y,t in self.click_events if t >= before_time]


# ---------------------- Main App Class ---------------------
class ScreenRecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Recorder v7")
        self.root.geometry("400x500")
        self.root.resizable(False, False)

        # Recording state
        self.recording = False
        self.paused = False
        self.writer = None
        self.recording_thread = None
        self.fps = 20

        # Region selection state
        self.selected_region = None  # (left, top, width, height) relative to monitor
        self.selected_monitor_index = 0  # index of monitor where region was selected

        # Mouse tracker
        self.mouse_tracker = GlobalMouseTracker()

        # Available monitors
        self.sct = mss.mss()
        self.monitors = self.sct.monitors
        # monitors[0] is combined all, monitors[1..n] are individual

        # Set recordings directory to ./captures relative to script location
        self.recordings_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
        if not os.path.exists(self.recordings_dir):
            os.makedirs(self.recordings_dir)

        self.click_ripple_active = False
        self.ripple_start_time = 0
        self.ripple_center = (0,0)

        # Build UI
        self._create_widgets()
        self._populate_recordings()

        # Register global hotkey
        if keyboard:
            keyboard.add_hotkey('ctrl+shift+r', self._stop_recording_hotkey)

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _create_widgets(self):
        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)

        # Tab 1: Record
        self.record_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.record_tab, text='Record')
        self._build_record_tab()

        # Tab 2: Saved Recordings
        self.saved_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.saved_tab, text='Saved Recordings')
        self._build_saved_tab()

    def _build_record_tab(self):
        # Monitor selection
        ttk.Label(self.record_tab, text="Monitor:").grid(row=0, column=0, sticky='w', padx=5, pady=5)
        self.monitor_var = tk.StringVar()
        self.monitor_combo = ttk.Combobox(self.record_tab, textvariable=self.monitor_var, state='readonly', width=30)
        self.monitor_combo.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
        monitor_names = []
        for i, mon in enumerate(self.monitors):
            if i == 0:
                monitor_names.append(f"All Monitors ({mon['width']}x{mon['height']})")
            else:
                monitor_names.append(f"Monitor {i} ({mon['width']}x{mon['height']}) - left:{mon['left']} top:{mon['top']})")
        self.monitor_combo['values'] = monitor_names
        if len(monitor_names) > 1:
            self.monitor_combo.current(1)  # first individual monitor
        else:
            self.monitor_combo.current(0)
        self.monitor_combo.bind('<<ComboboxSelected>>', self._on_monitor_change)

        # Region selection
        self.region_btn = ttk.Button(self.record_tab, text="Select Region", command=self._select_region)
        self.region_btn.grid(row=1, column=0, columnspan=2, pady=5, sticky='ew', padx=5)
        self.region_label = ttk.Label(self.record_tab, text="No region selected (full monitor)")
        self.region_label.grid(row=2, column=0, columnspan=2, pady=2, sticky='w', padx=5)

        # FPS slider
        ttk.Label(self.record_tab, text="FPS:").grid(row=3, column=0, sticky='w', padx=5, pady=5)
        self.fps_var = tk.IntVar(value=20)
        self.fps_slider = ttk.Scale(self.record_tab, from_=5, to=60, variable=self.fps_var, orient='horizontal',
                                    command=lambda v: self._fps_label.config(text=str(int(float(v)))))
        self.fps_slider.grid(row=3, column=1, sticky='ew', padx=5, pady=5)
        self._fps_label = ttk.Label(self.record_tab, text="20")
        self._fps_label.grid(row=3, column=2, padx=2)

        # Mouse visibility toggle
        self.mouse_var = tk.BooleanVar(value=False)
        self.mouse_check = ttk.Checkbutton(self.record_tab, text="Show mouse cursor highlight & click FX",
                                            variable=self.mouse_var)
        self.mouse_check.grid(row=4, column=0, columnspan=3, pady=5, sticky='w', padx=5)

        # Start/Stop buttons
        btn_frame = ttk.Frame(self.record_tab)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="Start Recording", command=self._start_recording)
        self.start_btn.pack(side='left', padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="Stop Recording", command=self._stop_recording, state='disabled')
        self.stop_btn.pack(side='left', padx=5)

        # Status
        self.status_label = ttk.Label(self.record_tab, text="Ready")
        self.status_label.grid(row=6, column=0, columnspan=3, pady=5, sticky='w', padx=5)

        # Configure grid weights
        self.record_tab.columnconfigure(1, weight=1)

    def _build_saved_tab(self):
        # Listbox + scrollbar
        list_frame = ttk.Frame(self.saved_tab)
        list_frame.pack(fill='both', expand=True, padx=5, pady=5)
        self.saved_listbox = tk.Listbox(list_frame, height=10)
        self.saved_listbox.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.saved_listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.saved_listbox.config(yscrollcommand=scrollbar.set)
        self.saved_listbox.bind('<Double-Button-1>', self._play_selected)

        # Buttons
        btn_frame = ttk.Frame(self.saved_tab)
        btn_frame.pack(fill='x', padx=5, pady=5)
        ttk.Button(btn_frame, text="Open Folder", command=self._open_folder).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Delete Selected", command=self._delete_selected).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Re-encode Selected", command=self._reencode_selected).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Refresh", command=self._populate_recordings).pack(side='left', padx=2)

    def _on_monitor_change(self, event):
        # Reset region selection only if the change was user-initiated (not programmatic)
        if getattr(self, '_programmatic_monitor_change', False):
            return
        self.selected_region = None
        self.region_label.config(text="No region selected (full monitor)")
        idx = self.monitor_combo.current()
        self.selected_monitor_index = idx

    def _select_region(self):
        # Get the selected monitor index
        idx = self.monitor_combo.current()
        if idx < 1 or idx >= len(self.monitors):
            messagebox.showerror("Error", "Please select a specific monitor (not 'All Monitors') for region selection.")
            return
        monitor = self.monitors[idx]
        mon_left = monitor['left']
        mon_top = monitor['top']
        mon_width = monitor['width']
        mon_height = monitor['height']

        # Show a brief instruction
        self.status_label.config(text="Click and drag to select region on monitor...")
        self.root.update()

        # Hide the recorder window to not interfere
        self.root.withdraw()
        time.sleep(0.2)

        # Use a simple fullscreen overlay on that monitor's area only
        region = self._overlay_region_on_monitor(mon_left, mon_top, mon_width, mon_height)

        self.root.deiconify()
        if region:
            # Store region as relative to selected monitor
            left, top, width, height = region
            self.selected_region = (left - mon_left, top - mon_top, width, height)
            self.selected_monitor_index = idx  # remember which monitor this region belongs to
            # Prevent _on_monitor_change from wiping the region during programmatic combobox update
            self._programmatic_monitor_change = True
            # Force the combobox to match the monitor used for region selection
            self.monitor_combo.current(idx)
            self.monitor_var.set(self.monitor_combo['values'][idx])
            self._programmatic_monitor_change = False
            self.region_label.config(text=f"Region: ({self.selected_region[0]}, {self.selected_region[1]}) {width}x{height}")
        else:
            self.selected_region = None
            self.region_label.config(text="Region selection cancelled.")
            self.status_label.config(text="Ready")

    def _overlay_region_on_monitor(self, mon_left, mon_top, mon_width, mon_height):
        """
        Creates a transparent fullscreen overlay window positioned exactly over the specified monitor.
        User clicks and drags to select a rectangle. Returns (left, top, width, height) in screen coordinates.
        """
        selector = tk.Toplevel(self.root)
        selector.title("Region Selector")
        selector.geometry(f"{mon_width}x{mon_height}+{mon_left}+{mon_top}")
        selector.attributes('-alpha', 0.3)
        selector.attributes('-topmost', True)
        selector.config(bg='gray')

        canvas = tk.Canvas(selector, cursor='crosshair', highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        start_x = start_y = end_x = end_y = None
        rect_id = None
        region = None

        def on_press(event):
            nonlocal start_x, start_y
            start_x = event.x
            start_y = event.y
            nonlocal rect_id
            if rect_id:
                canvas.delete(rect_id)
            rect_id = canvas.create_rectangle(start_x, start_y, start_x, start_y, outline='red', width=2)

        def on_drag(event):
            nonlocal end_x, end_y
            end_x = event.x
            end_y = event.y
            if rect_id:
                canvas.coords(rect_id, start_x, start_y, end_x, end_y)

        def on_release(event):
            nonlocal end_x, end_y, region
            end_x = event.x
            end_y = event.y
            x1 = min(start_x, end_x)
            y1 = min(start_y, end_y)
            x2 = max(start_x, end_x)
            y2 = max(start_y, end_y)
            if x2 - x1 < 10 or y2 - y1 < 10:
                region = None
            else:
                region = (mon_left + x1, mon_top + y1, x2 - x1, y2 - y1)
            selector.destroy()

        canvas.bind('<ButtonPress-1>', on_press)
        canvas.bind('<B1-Motion>', on_drag)
        canvas.bind('<ButtonRelease-1>', on_release)

        def cancel(event):
            nonlocal region
            region = None
            selector.destroy()
        selector.bind('<Escape>', cancel)

        self.root.wait_window(selector)
        return region

    def _start_recording(self):
        if self.recording:
            return

        # Determine FPS
        self.fps = self.fps_var.get()
        if self.fps < 1:
            self.fps = 20

        # Determine which monitor to use
        # If a region is selected, use the monitor index stored with that region
        if self.selected_region is not None:
            idx = self.selected_monitor_index
        else:
            idx = self.monitor_combo.current()

        if idx == 0:
            # All monitors (monitor 0 is combined)
            monitor = self.monitors[0]
            left = monitor['left']
            top = monitor['top']
            width = monitor['width']
            height = monitor['height']
        else:
            monitor = self.monitors[idx]
            if self.selected_region:
                left = monitor['left'] + self.selected_region[0]
                top = monitor['top'] + self.selected_region[1]
                width = self.selected_region[2]
                height = self.selected_region[3]
            else:
                left = monitor['left']
                top = monitor['top']
                width = monitor['width']
                height = monitor['height']

        # Validate region size
        if width <= 0 or height <= 0:
            messagebox.showerror("Error", "Invalid capture region dimensions.")
            return

        # Prepare filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.mp4"
        filepath = os.path.join(self.recordings_dir, filename)

        # Initialize VideoWriter with avc1 (H.264) if possible
        fourcc = cv2.VideoWriter_fourcc(*'avc1')
        self.writer = cv2.VideoWriter(filepath, fourcc, self.fps, (width, height))
        if not self.writer.isOpened():
            # Fallback to mp4v if avc1 not supported
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            self.writer = cv2.VideoWriter(filepath, fourcc, self.fps, (width, height))
            if not self.writer.isOpened():
                messagebox.showerror("Error", "Could not open video writer. Check codec support.")
                return

        # Update UI
        self.recording = True
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.status_label.config(text=f"Recording... Press Ctrl+Shift+R to stop (window hidden).")

        # Start mouse tracker
        if self.mouse_var.get():
            self.mouse_tracker.start()

        # Hide window
        self.root.withdraw()
        time.sleep(0.3)

        # Start recording thread
        self.recording_thread = threading.Thread(target=self._record_loop, args=(left, top, width, height, filepath, idx))
        self.recording_thread.daemon = True
        self.recording_thread.start()

    def _record_loop(self, left, top, width, height, filepath, monitor_idx):
        sct = mss.mss()
        monitor = {'left': left, 'top': top, 'width': width, 'height': height}
        frame_time = 1.0 / self.fps

        # Get monitor offset for mouse coordinate mapping
        monitor_info = self.monitors[monitor_idx] if monitor_idx < len(self.monitors) else None
        mon_left = monitor_info['left'] if monitor_info else 0
        mon_top = monitor_info['top'] if monitor_info else 0

        last_click_clear = time.time()

        while self.recording:
            start_frame = time.time()

            # Capture screen
            img = sct.grab(monitor)
            frame = np.array(img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            # Draw mouse effects if enabled
            if self.mouse_var.get():
                # Get current global mouse position using pynput
                mx, my = mouse_controller.position
                # Convert to frame coordinates
                fx = mx - left
                fy = my - top

                # Draw main green circle (always)
                if 0 <= fx < width and 0 <= fy < height:
                    cv2.circle(frame, (int(fx), int(fy)), 15, (0, 255, 0), 2)

                # Process click events from pynput
                now = time.time()
                events = self.mouse_tracker.get_click_events(since_time=last_click_clear)
                for (cx, cy, click_time) in events:
                    # Convert to frame coordinates
                    cfx = cx - left
                    cfy = cy - top
                    if 0 <= cfx < width and 0 <= cfy < height:
                        # Ripple effect: expanding outer circle
                        elapsed = now - click_time
                        if elapsed < 0.4:  # animate for 0.4 sec
                            radius = int(10 + elapsed * 150)  # expand
                            alpha = max(0, 1 - elapsed / 0.4)
                            # Draw as a fading yellow/orange circle
                            color = (0, int(255 * alpha), int(255 * alpha * 0.7))  # BGR: orange-yellow
                            cv2.circle(frame, (int(cfx), int(cfy)), min(radius, max(width, height)), color, 2)

                # Clear old click events
                if last_click_clear < time.time() - 1.0:
                    self.mouse_tracker.clear_old_events(time.time() - 1.0)
                    last_click_clear = time.time()

                # Draw drag indicator (if mouse button held)
                if self.mouse_tracker.dragging and self.mouse_tracker.drag_start:
                    dx, dy = self.mouse_tracker.drag_start
                    dfx = dx - left
                    dfy = dy - top
                    if 0 <= dfx < width and 0 <= dfy < height:
                        # Red dot with outer ring
                        cv2.circle(frame, (int(dfx), int(dfy)), 10, (0, 0, 255), -1)  # filled red
                        cv2.circle(frame, (int(dfx), int(dfy)), 20, (0, 255, 255), 2)  # yellow ring

            # Write frame
            self.writer.write(frame)

            # Maintain FPS
            elapsed = time.time() - start_frame
            sleep_time = frame_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Cleanup
        self.writer.release()
        sct.close()
        self.mouse_tracker.stop()
        self.root.after(0, self._recording_finished, filepath)

    def _reencode_with_ffmpeg(self, input_path):
        """
        Re-encode the video to H.264 with silent AAC audio using FFmpeg.
        Returns the path to the re-encoded file, or None if FFmpeg is not available.
        """
        try:
            # Check if ffmpeg is available
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("FFmpeg not found - skipping re-encode. Install ffmpeg for optimal upload compatibility.")
            return None

        # Generate output filename
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_h264{ext}"

        # Re-encode with libx264 and add silent AAC audio track
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-f', 'lavfi',
            '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '18',
            '-c:a', 'aac',
            '-shortest',
            '-y',
            output_path
        ]

        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
            # Remove the original file and use the re-encoded one
            os.remove(input_path)
            os.rename(output_path, input_path)
            return input_path
        except Exception as e:
            print(f"FFmpeg re-encode failed: {e}")
            # Cleanup partial output if it exists
            if os.path.exists(output_path):
                os.remove(output_path)
            return None

    def _recording_finished(self, filepath):
        self.root.deiconify()
        # Attempt to re-encode with FFmpeg for H.264 + silent audio compatibility
        reencoded_path = self._reencode_with_ffmpeg(filepath)
        if reencoded_path:
            filepath = reencoded_path
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        self.status_label.config(text=f"Recording saved: {os.path.basename(filepath)}")
        self._populate_recordings()
        messagebox.showinfo("Recording Saved", f"Video saved to:\n{filepath}")

    def _stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.status_label.config(text="Stopping...")

    def _stop_recording_hotkey(self):
        if self.recording:
            self.recording = False
            self.root.after(0, lambda: self.status_label.config(text="Stopping via hotkey..."))
            self.root.after(500, lambda: self.root.deiconify())

    def _populate_recordings(self):
        if not hasattr(self, 'saved_listbox'):
            return
        self.saved_listbox.delete(0, tk.END)
        if not os.path.exists(self.recordings_dir):
            return
        files = [f for f in os.listdir(self.recordings_dir) if f.endswith('.mp4')]
        files.sort(reverse=True)
        for f in files:
            filepath = os.path.join(self.recordings_dir, f)
            size = os.path.getsize(filepath)
            size_str = f"{size/1024/1024:.1f} MB" if size > 1024*1024 else f"{size/1024:.0f} KB"
            display = f"{f}  ({size_str})"
            self.saved_listbox.insert(tk.END, display)

    def _play_selected(self, event):
        selection = self.saved_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        filename = self.saved_listbox.get(idx).split("  (")[0]
        filepath = os.path.join(self.recordings_dir, filename)
        if os.path.exists(filepath):
            try:
                if platform.system() == 'Windows':
                    os.startfile(filepath)
                elif platform.system() == 'Darwin':
                    subprocess.run(['open', filepath])
                else:
                    subprocess.run(['xdg-open', filepath])
            except Exception as e:
                messagebox.showerror("Error", f"Could not open file: {e}")

    def _open_folder(self):
        if os.path.exists(self.recordings_dir):
            try:
                if platform.system() == 'Windows':
                    os.startfile(self.recordings_dir)
                elif platform.system() == 'Darwin':
                    subprocess.run(['open', self.recordings_dir])
                else:
                    subprocess.run(['xdg-open', self.recordings_dir])
            except Exception as e:
                messagebox.showerror("Error", f"Could not open folder: {e}")

    def _delete_selected(self):
        selection = self.saved_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        filename = self.saved_listbox.get(idx).split("  (")[0]
        filepath = os.path.join(self.recordings_dir, filename)
        if os.path.exists(filepath):
            if messagebox.askyesno("Delete", f"Delete {filename}?"):
                os.remove(filepath)
                self._populate_recordings()

    def _reencode_selected(self):
        """Re-encode the selected recording to H.264 with silent audio."""
        selection = self.saved_listbox.curselection()
        if not selection:
            messagebox.showinfo("Re-encode", "Please select a recording from the list first.")
            return
        idx = selection[0]
        filename = self.saved_listbox.get(idx).split("  (")[0]
        filepath = os.path.join(self.recordings_dir, filename)
        if not os.path.exists(filepath):
            messagebox.showerror("Error", "File not found.")
            return

        # Ask for confirmation
        if not messagebox.askyesno("Re-encode", f"Re-encode '{filename}' to H.264 with silent audio? (Original will be replaced)"):
            return

        self.status_label.config(text=f"Re-encoding {filename}...")
        self.root.update()

        result = self._reencode_with_ffmpeg(filepath)
        if result:
            messagebox.showinfo("Success", f"Re-encoding complete.\n{os.path.basename(result)}")
            self.status_label.config(text=f"Re-encoded: {os.path.basename(result)}")
        else:
            messagebox.showerror("Error", "Re-encoding failed. FFmpeg may not be installed or an error occurred.")
            self.status_label.config(text="Ready")
        self._populate_recordings()

    def _on_closing(self):
        if self.recording:
            if messagebox.askyesno("Recording Active", "A recording is in progress. Stop and exit?"):
                self.recording = False
                time.sleep(0.5)
        if keyboard:
            keyboard.unhook_all_hotkeys()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ScreenRecorderApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
