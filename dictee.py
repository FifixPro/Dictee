"""
Dictee - Push-to-talk dictation on an AMD XDNA2 NPU (Windows 11).

Hold Ctrl+Alt+Space, speak, release Space: the audio is transcribed by
Whisper V3 Turbo running on the NPU through FastFlowLM, and the text is
pasted at the cursor. A small floating pill shows what Dictee is doing and
a notification-area icon gives access to the language and other options.

Usage:
    python dictee.py                      run from this console (Ctrl+C to quit)
    python dictee.py --background         run with no console window (FastFlowLM hidden too)
    python dictee.py --stop               stop the background instance
    python dictee.py --install-startup    start in background at every Windows logon
    python dictee.py --uninstall-startup  remove the logon entry

Options:
    --lang auto|en|fr|de                  dictation language (remembered for next time)
    --no-gui                              no pill and no tray icon, console messages only

See README.md for the full installation guide.
"""

import argparse
import ctypes
import io
import json
import logging
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import wave
import winreg
import winsound
from collections import deque
from ctypes import wintypes
from pathlib import Path

import keyboard
import numpy as np
import pyperclip
import requests
import sounddevice as sd

try:                                          # optional: only needed for the graphical interface
    import tkinter as tk
    import pystray
    from PIL import Image, ImageDraw
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
FLM_BASE_URL = "http://127.0.0.1:52625/v1"   # FastFlowLM default port
FLM_COMMAND = ["flm", "serve", "--asr", "1"]  # add an LLM tag after "serve" if your FLM version needs one
MODEL = "whisper-v3"                          # name expected by the FLM ASR endpoint
LANGUAGE = "auto"                             # default language: "auto", "en", "fr" or "de"
TRIGGER_KEY = "space"                         # pressed together with Ctrl+Alt
SAMPLE_RATE = 16000                           # Whisper's native sample rate
MIN_SECONDS = 0.4                             # shorter presses are treated as accidental
MAX_SECONDS = 300                             # safety stop if the key-up event is missed
AUTO_START_SERVER = True                      # launch FastFlowLM if it is not running
SERVER_START_TIMEOUT = 180                    # seconds to wait for the server to come up
RESTORE_CLIPBOARD = True                      # put the previous clipboard text back after pasting
BEEP = True                                   # short beeps when recording starts and stops

# Languages offered in the tray menu and accepted by --lang.
# "auto" sends no language to the server: Whisper detects it on each recording.
LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
}

APP_NAME = "Dictee"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Dictee"
LOG_FILE = STATE_DIR / "dictee.log"
FLM_LOG_FILE = STATE_DIR / "flm_server.log"
PID_FILE = STATE_DIR / "dictee.pid"
CONFIG_FILE = STATE_DIR / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME = "Local\\Dictee_single_instance"

VK_CONTROL, VK_MENU = 0x11, 0x12
_user32 = ctypes.windll.user32
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

log = logging.getLogger(APP_NAME)
start_event = threading.Event()   # set by the keyboard hook: start recording
stop_event = threading.Event()    # set by the keyboard hook: stop recording
quit_event = threading.Event()    # set when the application is closing
ui_queue = queue.Queue()          # (state, text) messages for the overlay, read by the Tk thread
recording = False                 # True between Ctrl+Alt+Space down and Space up
busy = False                      # True while transcribing/pasting
hidden = False                    # True when running without a console
gui_active = False                # True when the pill and tray icon are in use
language = LANGUAGE               # effective dictation language
audio_level = 0.0                 # 0..1 microphone level, read by the overlay
flm_process = None                # FastFlowLM server process, if this script started it
tray_icon = None                  # pystray icon, created in its own thread
tray_images = {}                  # "idle" / "recording" icon images
_tray_state = "idle"
_mutex_handle = None              # kept alive for the lifetime of the process


def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    log.addHandler(file_handler)
    if sys.stdout is not None:                # None under pythonw.exe
        log.addHandler(logging.StreamHandler(sys.stdout))


def notify(state, text=""):
    """Tell the graphical interface what is happening (no-op without GUI)."""
    global _tray_state
    if not gui_active:
        return
    ui_queue.put((state, text))
    wanted = "recording" if state == "listening" else "idle"
    if tray_icon is not None and wanted != _tray_state:
        try:
            tray_icon.icon = tray_images[wanted]
            _tray_state = wanted
        except Exception:
            pass


def fatal(message):
    log.error(message)
    notify("fatal", message)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Saved settings (language chosen from the tray menu or --lang)
# ----------------------------------------------------------------------------
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_language(code):
    global language
    language = code
    try:
        CONFIG_FILE.write_text(json.dumps({"language": code}), encoding="utf-8")
    except OSError as error:
        log.error("Could not save settings: %s", error)
    log.info("Dictation language: %s", LANGUAGES.get(code, code))


# ----------------------------------------------------------------------------
# Background mode, single instance, logon startup
# ----------------------------------------------------------------------------
def pythonw_path():
    path = Path(sys.executable).with_name("pythonw.exe")
    if not path.exists():
        log.error("pythonw.exe not found next to %s", sys.executable)
        return None
    return path


def is_running():
    handle = _kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)   # SYNCHRONIZE
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def acquire_single_instance():
    global _mutex_handle
    _mutex_handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
        fatal("Another instance is already running (use --stop or the tray icon to end it).")


def launch_background(extra_args):
    """Relaunch this script with pythonw.exe (no console window) and return."""
    if is_running():
        fatal("Already running in the background (use --stop or the tray icon to end it).")
    pythonw = pythonw_path()
    if pythonw is None:
        sys.exit(1)
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve()), "--background", *extra_args],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    log.info("Started in the background. Log file: %s", LOG_FILE)
    log.info("Stop it from the tray icon, or with: python dictee.py --stop")


def stop_background():
    if not PID_FILE.exists():
        fatal("No running instance found.")
    pid = PID_FILE.read_text().strip()
    # /T also ends the FastFlowLM server if this script started it
    result = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, text=True)
    PID_FILE.unlink(missing_ok=True)
    log.info("Stopped." if result.returncode == 0 else f"Nothing to stop (process {pid} not found).")


def startup_installed():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def install_startup():
    pythonw = pythonw_path()
    if pythonw is None:
        return
    command = f'"{pythonw}" "{Path(__file__).resolve()}" --background'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    log.info("Installed: dictation will start in the background at every logon.")


def uninstall_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        log.info("Logon entry removed.")
    except FileNotFoundError:
        log.info("No logon entry to remove.")


# ----------------------------------------------------------------------------
# Keyboard hook
# ----------------------------------------------------------------------------
def _key_down(vk):
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def on_trigger_key(event):
    """Called for every Space key event. Return False to swallow it, True to let it through."""
    global recording
    if event.event_type == keyboard.KEY_DOWN:
        if recording:
            return False                      # keyboard auto-repeat while holding
        if _key_down(VK_CONTROL) and _key_down(VK_MENU):
            if not busy:
                recording = True
                stop_event.clear()
                start_event.set()
            return False                      # do not type a space in the application
        return True                           # plain Space: normal typing
    if recording:                             # key up: end of push-to-talk
        recording = False
        stop_event.set()
        return False
    return True


# ----------------------------------------------------------------------------
# FastFlowLM server
# ----------------------------------------------------------------------------
def server_is_up():
    try:
        return requests.get(f"{FLM_BASE_URL}/models", timeout=2).ok
    except requests.RequestException:
        return False


def ensure_server():
    global flm_process
    if server_is_up():
        log.info("FastFlowLM server detected.")
        return
    if not AUTO_START_SERVER:
        fatal("FastFlowLM server unreachable. Start it first: flm serve --asr 1")
    log.info("Starting FastFlowLM (%s)...", " ".join(FLM_COMMAND))
    notify("starting")
    try:
        if hidden:
            flm_log = open(FLM_LOG_FILE, "w", encoding="utf-8", errors="replace")
            flm_process = subprocess.Popen(
                FLM_COMMAND, stdin=subprocess.DEVNULL, stdout=flm_log,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            flm_process = subprocess.Popen(FLM_COMMAND, creationflags=subprocess.CREATE_NEW_CONSOLE)
    except FileNotFoundError:
        fatal("'flm' command not found: install FastFlowLM (https://fastflowlm.com).")
    deadline = time.time() + SERVER_START_TIMEOUT
    while time.time() < deadline and not quit_event.is_set():
        if server_is_up():
            log.info("FastFlowLM server ready.")
            return
        time.sleep(1)
    if quit_event.is_set():
        sys.exit(0)
    fatal("FastFlowLM did not start. Check its output "
          f"({FLM_LOG_FILE if hidden else 'its console window'}); "
          "is the model downloaded? -> flm pull whisper-v3:turbo")


def stop_server():
    """Stop the FastFlowLM server, only if this script started it."""
    if flm_process is not None and flm_process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(flm_process.pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


# ----------------------------------------------------------------------------
# Record -> transcribe -> paste
# ----------------------------------------------------------------------------
def beep(frequency):
    if BEEP:
        try:
            winsound.Beep(frequency, 80)
        except RuntimeError:
            pass


def record_while_held():
    """Record the microphone until the trigger key is released. Returns int16 samples."""
    global audio_level
    chunks = []

    def callback(indata, frames, time_info, status):
        global audio_level
        chunks.append(indata.copy())
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2))) / 32768.0
        audio_level = min(1.0, math.sqrt(rms) * 2.5)   # rough perceptual scaling for the display

    audio_level = 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback):
        stop_event.wait(timeout=MAX_SECONDS)
    audio_level = 0.0
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks, axis=0).reshape(-1)


def to_wav_bytes(samples):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)                   # 16-bit PCM
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def transcribe(wav_bytes):
    data = {"model": MODEL}
    if language != "auto":                    # no language field: Whisper detects it
        data["language"] = language
    response = requests.post(
        f"{FLM_BASE_URL}/audio/transcriptions",
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=300,
    )
    response.raise_for_status()
    try:                                      # OpenAI-style JSON: {"text": "..."}
        return response.json().get("text", "").strip()
    except ValueError:                        # some versions answer in plain text
        return response.text.strip()


def paste_at_cursor(text):
    # Wait for Ctrl/Alt to be released, otherwise Ctrl+V would become Ctrl+Alt+V
    deadline = time.time() + 3
    while (_key_down(VK_CONTROL) or _key_down(VK_MENU)) and time.time() < deadline:
        time.sleep(0.05)
    previous = None
    if RESTORE_CLIPBOARD:
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = None
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    if previous is not None:
        time.sleep(0.4)                       # let the target application read the clipboard
        pyperclip.copy(previous)


def run():
    """Main dictation loop. Runs in the main thread without GUI, in a worker thread with GUI."""
    global busy, recording
    ensure_server()
    keyboard.hook_key(TRIGGER_KEY, on_trigger_key, suppress=True)
    log.info("Ready! Hold [Ctrl+Alt+Space] to speak, release to transcribe. (language: %s)",
             LANGUAGES.get(language, language))
    notify("ready", "Ready · hold Ctrl+Alt+Space to speak")

    while not quit_event.is_set():
        # Short timeout so that Ctrl+C and Quit stay responsive
        if not start_event.wait(timeout=0.5):
            continue
        start_event.clear()
        busy = True
        try:
            beep(880)
            log.info("Listening...")
            notify("listening")
            samples = record_while_held()
            recording = False                 # covers the MAX_SECONDS safety stop
            beep(660)
            duration = len(samples) / SAMPLE_RATE
            if duration < MIN_SECONDS:
                log.info("Press too short, ignored.")
                notify("hidden")
                continue
            log.info("Transcribing on the NPU (%.1f s of audio)...", duration)
            notify("transcribing")
            started = time.time()
            text = transcribe(to_wav_bytes(samples))
            if text:
                log.info("Transcribed in %.1f s: %s", time.time() - started, text)
                paste_at_cursor(text)
                notify("done", text)
            else:
                log.info("No text recognised.")
                notify("error", "No text recognised")
        except requests.RequestException as error:
            log.error("FastFlowLM connection error: %s", error)
            notify("error", "FastFlowLM connection error")
        except Exception as error:
            log.error("Error: %s", error)
            notify("error", str(error))
        finally:
            busy = False


def worker():
    """Thread wrapper around run() used with the graphical interface."""
    try:
        run()
    except SystemExit:
        pass                                  # fatal() already informed the overlay
    except Exception as error:
        log.exception("Unexpected error")
        notify("fatal", str(error))


# ----------------------------------------------------------------------------
# Graphical interface: floating pill (tkinter) and notification-area icon (pystray)
# ----------------------------------------------------------------------------
def ellipsize(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Overlay:
    """Small always-on-top pill at the bottom of the screen. Never takes keyboard focus."""

    KEY = "#010203"                           # colour made transparent around the pill
    BG = "#1b1b1f"
    FG = "#f5f5f5"
    DIM = "#9a9aa2"
    RED = "#ff4d4f"
    GREEN = "#3ddc84"
    ACCENT = "#ff6a13"
    BARS = 26

    def __init__(self):
        try:                                  # crisp rendering on high-DPI screens
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)      # no title bar, no taskbar button
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.KEY)
        self.root.configure(bg=self.KEY)
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.w, self.h = self.px(380), self.px(52)
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h,
                                bg=self.KEY, highlightthickness=0)
        self.canvas.pack()
        x = (self.root.winfo_screenwidth() - self.w) // 2
        y = self.root.winfo_screenheight() - self.h - self.px(96)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")

        self.state = "hidden"
        self.text = ""
        self.tick = 0
        self.hide_at = None
        self.quit_after = False
        self.visible = False
        self.styled = False
        self.levels = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.root.after(33, self.poll)

    def px(self, value):
        return int(round(value * self.scale))

    def make_non_activating(self):
        """Ask Windows never to give this window the keyboard focus."""
        try:
            user32 = ctypes.WinDLL("user32")
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)          # GA_ROOT
            style = user32.GetWindowLongW(hwnd, -20)                     # GWL_EXSTYLE
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080 | 0x00000008)
        except Exception as error:
            log.error("Could not set overlay window style: %s", error)

    def set_state(self, state, text):
        if state == "quit":
            self.root.quit()
            return
        self.quit_after = state == "fatal"
        if state == "fatal":
            state = "error"
        self.state, self.text, self.tick = state, text, 0
        durations = {"ready": 3.0, "done": 1.6, "error": 6.0 if self.quit_after else 3.5}
        self.hide_at = time.time() + durations[state] if state in durations else None
        if state == "listening":
            self.levels.extend([0.0] * self.BARS)
        if state == "hidden":
            if self.visible:
                self.root.withdraw()
                self.visible = False
        elif not self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            if not self.styled:
                self.root.update_idletasks()
                self.make_non_activating()
                self.styled = True
            self.visible = True

    def poll(self):
        """Runs ~30 times per second in the Tk thread: read messages, animate."""
        try:
            while True:
                state, text = ui_queue.get_nowait()
                self.set_state(state, text)
        except queue.Empty:
            pass
        if self.state != "hidden":
            if self.hide_at is not None and time.time() >= self.hide_at:
                if self.quit_after:
                    self.root.quit()
                    return
                self.set_state("hidden", "")
            else:
                self.draw()
        self.root.after(33, self.poll)

    def draw(self):
        c, w, h, px = self.canvas, self.w, self.h, self.px
        r, mid = h // 2, h // 2
        self.tick += 1
        c.delete("all")
        # Pill background: two circles and a rectangle
        c.create_oval(0, 0, h, h, fill=self.BG, outline=self.BG)
        c.create_oval(w - h, 0, w, h, fill=self.BG, outline=self.BG)
        c.create_rectangle(r, 0, w - r, h, fill=self.BG, outline=self.BG)
        left, right = r + px(2), w - r - px(2)
        font = ("Segoe UI", 11)

        if self.state == "listening":
            # Pulsing red dot, live microphone level bars, language tag
            radius = px(6) + px(3) * (0.5 + 0.5 * math.sin(self.tick / 5))
            cx = left + px(4)
            c.create_oval(cx - radius, mid - radius, cx + radius, mid + radius,
                          fill=self.RED, outline="")
            self.levels.append(audio_level)
            x0, x1 = cx + px(22), right - px(46)
            step = (x1 - x0) / self.BARS
            for i, level in enumerate(self.levels):
                bar = max(px(2), level * h * 0.62)
                x = x0 + i * step
                c.create_rectangle(x, mid - bar / 2, x + step * 0.55, mid + bar / 2,
                                   fill=self.FG, outline="")
            c.create_text(right, mid, text=language.upper(), anchor="e",
                          fill=self.DIM, font=("Segoe UI", 9, "bold"))
        elif self.state in ("transcribing", "starting"):
            label = "Transcribing on the NPU" if self.state == "transcribing" else "Starting FastFlowLM"
            c.create_text(left, mid, text=label, anchor="w", fill=self.FG, font=font)
            for i in range(3):                # three bouncing dots
                lift = px(6) * max(0.0, math.sin(self.tick / 4 - i * 0.9))
                x, rad = right - px(34) + i * px(14), px(3.5)
                c.create_oval(x - rad, mid - rad - lift, x + rad, mid + rad - lift,
                              fill=self.ACCENT, outline="")
        elif self.state in ("ready", "done"):
            c.create_text(left, mid, text="✓", anchor="w", fill=self.GREEN,
                          font=("Segoe UI", 13, "bold"))
            c.create_text(left + px(24), mid, text=ellipsize(self.text, 40), anchor="w",
                          fill=self.FG, font=font)
        elif self.state == "error":
            c.create_text(left, mid, text="!", anchor="w", fill=self.RED,
                          font=("Segoe UI", 13, "bold"))
            c.create_text(left + px(18), mid, text=ellipsize(self.text, 42), anchor="w",
                          fill=self.FG, font=font)


def make_tray_image(colour):
    """Draw a microphone icon on a dark disc."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 62, 62), fill=(27, 27, 31, 255))
    draw.rounded_rectangle((25, 12, 39, 38), radius=7, fill=colour)
    draw.arc((18, 20, 46, 46), start=0, end=180, fill=colour, width=4)
    draw.line((32, 46, 32, 53), fill=colour, width=4)
    draw.line((24, 53, 40, 53), fill=colour, width=4)
    return image


def toggle_startup():
    if startup_installed():
        uninstall_startup()
    else:
        install_startup()


def build_tray_menu():
    def language_item(code, label):
        return pystray.MenuItem(label, lambda icon, item: set_language(code),
                                checked=lambda item: language == code, radio=True)

    return pystray.Menu(
        pystray.MenuItem("Dictee · hold Ctrl+Alt+Space to speak", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Language", pystray.Menu(
            *[language_item(code, label) for code, label in LANGUAGES.items()])),
        pystray.MenuItem("Start at logon", lambda icon, item: toggle_startup(),
                         checked=lambda item: startup_installed()),
        pystray.MenuItem("Open log", lambda icon, item: os.startfile(LOG_FILE)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: ui_queue.put(("quit", ""))),
    )


def tray_thread():
    """The icon is created and run in the same thread, as its Windows message loop requires."""
    global tray_icon
    try:
        tray_icon = pystray.Icon(APP_NAME, tray_images["idle"],
                                 "Dictee · hold Ctrl+Alt+Space to speak", build_tray_menu())
        tray_icon.run()
    except Exception:
        log.exception("Tray icon failed")


def run_with_gui():
    global gui_active
    tray_images["idle"] = make_tray_image((245, 245, 245, 255))
    tray_images["recording"] = make_tray_image((255, 77, 79, 255))
    overlay = Overlay()
    gui_active = True
    # Ctrl+C in the console: ask the Tk loop to quit
    signal.signal(signal.SIGINT, lambda signum, frame: ui_queue.put(("quit", "")))
    threading.Thread(target=tray_thread, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    overlay.root.mainloop()


def cleanup():
    quit_event.set()
    stop_event.set()
    try:
        keyboard.unhook_all()
    except Exception:
        pass
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass
    stop_server()
    PID_FILE.unlink(missing_ok=True)
    log.info("Dictation stopped.")


def main():
    global hidden
    parser = argparse.ArgumentParser(description="Dictee: push-to-talk dictation on an AMD XDNA2 NPU.")
    parser.add_argument("--lang", choices=list(LANGUAGES),
                        help="dictation language, remembered for next time (default: %s)" % LANGUAGE)
    parser.add_argument("--no-gui", action="store_true",
                        help="no floating pill and no tray icon")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--background", action="store_true", help="run without a console window")
    group.add_argument("--stop", action="store_true", help="stop the running instance")
    group.add_argument("--install-startup", action="store_true",
                       help="start in the background at every Windows logon")
    group.add_argument("--uninstall-startup", action="store_true", help="remove the logon entry")
    args = parser.parse_args()
    setup_logging()

    global language
    saved = load_config().get("language")
    language = saved if saved in LANGUAGES else LANGUAGE
    if args.lang:
        set_language(args.lang)               # also saved, so background/logon starts reuse it

    if args.stop:
        stop_background()
    elif args.install_startup:
        install_startup()
    elif args.uninstall_startup:
        uninstall_startup()
    elif args.background and sys.stdout is not None:
        # We have a console: relaunch without one
        launch_background(["--no-gui"] if args.no_gui else [])
    else:
        hidden = args.background
        acquire_single_instance()             # exits here if another instance is running
        PID_FILE.write_text(str(os.getpid()))
        use_gui = not args.no_gui
        if use_gui and not GUI_AVAILABLE:
            log.info("Graphical interface unavailable (pip install pystray Pillow); console mode.")
            use_gui = False
        try:
            if use_gui:
                run_with_gui()
            else:
                run()
        except KeyboardInterrupt:
            pass
        finally:
            cleanup()


if __name__ == "__main__":
    main()
