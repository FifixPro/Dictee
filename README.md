# Dictee — push-to-talk dictation on an AMD XDNA2 NPU

Hold **Ctrl + Alt + Space**, speak, release. Your speech is transcribed locally by
**Whisper V3 Turbo** running on the Ryzen AI NPU through
[FastFlowLM](https://fastflowlm.com) (FLM), and the text is pasted where your cursor is —
in Word, a browser, Teams, or any other application. Nothing leaves the machine.

A small floating pill shows when Dictee is listening and transcribing, and a tray icon
next to the clock gives access to the language (auto-detect, English, French, German) and
the other options. Dictee can run from a console, or in the background together with
FastFlowLM, and can start automatically when you log on to Windows.

## Requirements

| Item | Requirement |
|---|---|
| Processor | AMD Ryzen AI with an **XDNA2** NPU (Ryzen AI 300 series or newer). First-generation XDNA NPUs (Ryzen 7040 / 8040) are not supported by FastFlowLM. |
| OS | Windows 11 |
| NPU driver | Up to date (checked in step 1 with `flm validate`) |
| Python | 3.10 or newer, with "Add python.exe to PATH" ticked at install |
| Microphone | Any input device set as the Windows default |

## Installation

Run all commands in PowerShell.

### 1. Install FastFlowLM and check the NPU

Download and run the installer from <https://fastflowlm.com>, open a **new** PowerShell
window, then:

```powershell
flm version
flm validate
```

`flm validate` must report a working NPU stack. If it complains about the driver, update
the AMD NPU driver and run it again.

### 2. Download the Whisper model

```powershell
flm pull whisper-v3:turbo
```

### 3. Copy the script

Copy `dictee.py` and `requirements.txt` into a folder of your choice, for example
`C:\Tools\Dictee`, and move into it:

```powershell
cd C:\Tools\Dictee
```

### 4. Install the Python dependencies

```powershell
pip install -r requirements.txt
```

### 5. First run (in a console)

```powershell
python dictee.py
```

The script starts FastFlowLM in a second window if it is not already running, waits for
it, then prints `Ready!`. Click in any text field, hold **Ctrl + Alt + Space**, say a
sentence, and release Space. You hear a short beep when recording starts and another when
it stops; the text appears a few seconds later.

Do this first run before using background mode: it is where installation problems are
visible. Press **Ctrl + C** in the console, or choose **Quit** in the tray icon menu, to
stop Dictee. Quitting also stops the FastFlowLM server if Dictee started it.

If the first run fails, try `python dictee.py --no-gui`: it runs the dictation with
console messages only, which tells you whether the problem is in the dictation itself or
in the graphical interface.

## The interface

Dictee has no main window. It shows two discreet elements.

**The pill.** A small dark pill appears at the bottom centre of the screen only while
something is happening. It never takes the keyboard focus, so your cursor stays where it
is.

| Pill | Meaning |
|---|---|
| "Starting FastFlowLM" with moving dots | The server and the Whisper model are loading (first start only). |
| Green tick, "Ready" | Dictee is ready; the pill then disappears. |
| Pulsing red dot and moving bars | Recording. The bars follow your microphone level; the tag on the right is the language (`AUTO`, `EN`, `FR`, `DE`). |
| "Transcribing on the NPU" with moving dots | The recording is being transcribed. |
| Green tick and the first words of the text | The text has been pasted; the pill then disappears. |
| Red "!" and a message | Something went wrong; details are in the log. |

**The tray icon.** A microphone icon sits in the notification area next to the clock; it
turns red while recording. Windows 11 hides new icons by default: click the **^** arrow
next to the clock to find it, and drag it onto the taskbar to keep it visible.
Right-click the icon for the menu:

| Menu entry | Effect |
|---|---|
| Language | Auto-detect, English, Français or Deutsch. Applies immediately and is remembered. |
| Start at logon | Tick to start Dictee in the background at every Windows logon. |
| Open log | Open the log file. |
| Quit | Stop Dictee, and the FastFlowLM server if Dictee started it. |

## Running in the background

Once the first run works, Dictee and FastFlowLM can run with no console window at all;
only the pill and the tray icon remain.

| Command | Effect |
|---|---|
| `python dictee.py --background` | Start without a console. FastFlowLM is started hidden too if it is not already running. |
| `python dictee.py --stop` | Stop the running instance, and the FastFlowLM server it started (same as **Quit** in the tray menu). |
| `python dictee.py --install-startup` | Start in the background automatically at every Windows logon (same as **Start at logon** in the tray menu). |
| `python dictee.py --uninstall-startup` | Remove the automatic start. |

`--install-startup` only registers the automatic start for your Windows account; it does
not start anything immediately. Run `--background` once, or log off and on again.

The automatic start records the full path of `dictee.py`. If you move or rename the
folder afterwards, run `python dictee.py --install-startup` again from the new location.

Messages go to log files:

| File | Content |
|---|---|
| `%LOCALAPPDATA%\Dictee\dictee.log` | Script activity, transcribed text and errors |
| `%LOCALAPPDATA%\Dictee\flm_server.log` | Output of the hidden FastFlowLM server |

To watch the log live:

```powershell
Get-Content $env:LOCALAPPDATA\Dictee\dictee.log -Wait -Tail 20
```

## Dictation language

Dictee offers four choices: **Auto-detect**, **English**, **Français** and **Deutsch**.

With Auto-detect (the default), Whisper identifies the spoken language on each press, so
you can switch between English, French and German from one sentence to the next without
changing anything.

To force one language, pick it in the tray icon menu under **Language**, or pass `--lang`
when starting:

```powershell
python dictee.py --lang de
python dictee.py --background --lang en
```

`--lang` accepts `auto`, `en`, `fr` and `de`. The choice is saved and reused at the next
start, including automatic starts at logon.

Forcing a language helps when very short phrases are detected in the wrong language.
Auto-detect cannot be restricted to these three languages: Whisper chooses among all the
languages it knows, so a short or unclear recording can occasionally be transcribed in
another language.

## Settings

The settings are constants at the top of `dictee.py`. Restart the script after
changing one.

| Setting | Default | Purpose |
|---|---|---|
| `LANGUAGE` | `"auto"` | Dictation language used until one is chosen with the tray menu or `--lang`. |
| `LANGUAGES` | auto, en, fr, de | Languages offered in the tray menu and by `--lang`. Add a line to offer another one, e.g. `"es": "Español"`. |
| `TRIGGER_KEY` | `"space"` | Key pressed together with Ctrl + Alt. |
| `BEEP` | `True` | Beeps at start and end of recording. |
| `RESTORE_CLIPBOARD` | `True` | Put the previous clipboard text back after pasting. |
| `AUTO_START_SERVER` | `True` | Start FastFlowLM if it is not running. |
| `FLM_COMMAND` | `flm serve --asr 1` | Command used to start the server. |
| `MIN_SECONDS` | `0.4` | Shorter presses are ignored. |

## Troubleshooting

| Symptom | What to do |
|---|---|
| `'flm' command not found` | FastFlowLM is not installed or not on PATH. Reinstall it and open a new PowerShell window. |
| `FastFlowLM did not start` | Run `flm serve --asr 1` by hand to see the error. Check that step 2 was done. If the server asks for a language model, set `FLM_COMMAND = ["flm", "serve", "llama3.2:1b", "--asr", "1"]` in the script. |
| Nothing happens on Ctrl + Alt + Space | Check that the script is running (look at the log). If the active window runs as administrator, the script must also be started from an administrator PowerShell. |
| `No text recognised` or empty result | Check the default microphone in Windows Settings > System > Sound, and that Python is allowed under Privacy & security > Microphone. |
| Text is transcribed (visible in the log) but not pasted | The target application does not accept Ctrl + V; the text is still in the log. |
| `Another instance is already running` | Run `python dictee.py --stop`, then start again. |
| No tray icon | Click the **^** arrow next to the clock: Windows 11 hides new icons there. |
| No pill and no tray icon, log says `Graphical interface unavailable` | Run `pip install -r requirements.txt` again (`pystray` and `Pillow` are missing). |
| A microphone icon stays in the tray after `--stop` | Move the mouse over it; Windows then removes it. |
| Dictation no longer starts at logon after moving the folder | Run `python dictee.py --install-startup` again from the new location. |
| Another application already uses Ctrl + Alt + Space | Change `TRIGGER_KEY` in the script, or the shortcut in the other application. |

## Known limitations

- **Forced language.** Some FastFlowLM versions ignore the `language` field, in which
  case choosing a language has no effect and Whisper's automatic detection is always used.
- **Long dictations.** Whisper works on 30-second windows; keep each press to a sentence
  or a short paragraph for best results.
- **Clipboard.** Pasting goes through the clipboard. Text that was there before is
  restored; images or files that were there are not.
- **One NPU job at a time.** If another application is using the NPU through the same
  FastFlowLM server, transcription waits for it.
- **AltGr.** Windows treats AltGr as Ctrl + Alt, so AltGr + Space also starts dictation.

## Uninstall

```powershell
python dictee.py --stop
python dictee.py --uninstall-startup
pip uninstall -y sounddevice keyboard pyperclip pystray
flm remove whisper-v3:turbo
```

Then delete the script folder and `%LOCALAPPDATA%\Dictee`.
