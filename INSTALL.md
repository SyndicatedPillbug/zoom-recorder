# Installing zoom-recorder

Five minutes, no admin rights for the app itself. The only step that asks for
your password is the BlackHole driver install (a Homebrew package).

## 1. Get the code and the basics

```bash
git clone <this repo> ~/zoom-recorder
cd ~/zoom-recorder

# Checks everything and reports what is missing:
./install.sh

# ...or install what is missing (Homebrew ffmpeg + BlackHole, pip rumps):
./install.sh --install-deps
```

`./install.sh --dry-run` prints everything it would do without changing
anything.

## 2. Start the menu bar

```bash
./run-menubar.command
```

Look for 🎙 in the menu bar. Everything a non-technical user needs is there:

- **Volume 44%** — top-level volume (slider, ±5%, mute, presets).
- **Start recording** / **Stop recording (mm:ss)**.
- **Open transcript window** — while a live transcript is running.
- **My recordings…** — the Control Center's list of past recordings.
- **Check my audio setup…** — guided checks with one-click fixes.
- **Settings…** — the essentials (advanced form behind one button).
- **Help** — plain-language FAQ.
- **Play sound through ▸** — which speakers/headphones to use, and *Reset audio*.

The **Control Center** is a local page (no internet) opened from those menu
items. The first time you run it, follow **Setup** end to end: it checks the
audio, tests the microphone, picks your output, records ten seconds and plays
it back so you can hear both sides.

Optional: `./install.sh --autostart` enables login autostart (a per-user
LaunchAgent). Without it, nothing starts at login and `./run-menubar.command`
is how you launch the app. The same switch exists in the Setup screen and
Settings.

## 3. The one-time microphone grant

macOS asks for **Microphone** access the first time something records. The
menu-bar app spawns the recorder, so the grant belongs to the app context
that launched it.

- If a prompt appears, click **Allow**.
- If it does not (background contexts sometimes skip the prompt), open
  **System Settings → Privacy & Security → Microphone** and enable the entry
  for the Python/menu-bar app — the Setup screen's *Open Microphone settings*
  button takes you straight there and tells you when the microphone reads as
  digital silence.

No Screen Recording or System Audio Recording permission is needed for the
normal (loopback) path. `--system-capture tap` is the only mode that needs
it, and it is opt-in.

## 4. Optional: live transcript + answers

Open **Setup** (menu: *Check my audio setup…*) and choose step 4:

- **On this Mac** — free and private: `whisper.cpp` (installed by
  `./install.sh --install-deps`) plus a one-time ~547 MiB
  `large-v3-turbo-q5_0` model. No account. `base.en` remains available as a
  smaller speed-first fallback.
- Local mode also shows a live, provisional word draft from overlapping
  two-second windows. Only words stable across windows are committed to the
  transcript and answer context; unstable draft text is never written back or
  indexed. This is enabled by default and can be adjusted in Settings.
- **Online** — Groq / OpenAI / OpenRouter with an API key for the best
  accuracy and for Suggestions/answers.

Optional multi-speaker attribution is a separate, post-call feature. It is
disabled by default so it cannot affect live capture or answer latency. After
installing WhisperX and its diarization dependencies, provide the model's
Hugging Face credential as `HF_TOKEN` and start a live session with
`./zoom_record.py --live --diarize`. The result is written under `derived/`;
missing dependencies or a failed pass leave the ordinary transcript intact.

During any live session, click a speaker label in the transcript to enter a
participant name. Names are saved as local session metadata and do not require
network access.

To keep everything on the machine, use **Settings → Offline mode** (blocks
all internet access). See `SECURITY.md` for exactly what is sent where.

### Obsidian context and session identity

In Advanced settings, add the folders that contain your Obsidian Markdown
notes and past transcripts to the knowledge-base directories. The app builds a
local incremental embedding cache plus a SQLite lexical index, skips Obsidian
metadata/plugin folders, and continues with lexical retrieval if macOS denies
the embedding cache directory. For a protected vault, grant the menu-bar
launcher Files & Folders access in **System Settings → Privacy & Security**.

Each finalized recording has a `session.json` sidecar and a readable topic
folder name derived from the first substantive transcript line. This is
additional metadata; the original recording and `derived/` outputs remain in
the same session folder. `live_events.jsonl` can be replayed with
`python3 -m hud.replay PATH` for offline troubleshooting.

## 5. Optional: hardware volume keys in loopback mode

macOS volume keys do nothing while a Multi-Output Device is the default
output. If you want them back, import
`karabiner/zoom-recorder-volume.json` in Karabiner Elements
(Complex Modifications → **Add rule** → *Import more rules from a file*).
It maps volume up/down/mute to this tool's CLI in both modes. Note that
Karabiner runs it as a shell command; skip this if you would rather not.

## Troubleshooting

```bash
./zoom_record.py --doctor      # tools, BlackHole, routing, mic, with fixes
./zoom_record.py --list        # every device and what it is
./zoom_record.py --self-test   # play a tone, verify the capture path
```

- **"No usable mic audio" / digital silence** — microphone permission (step 3).
- **System track silent** — BlackHole missing, or the default output is not
  the Multi-Output Device; recording start sets that up automatically, and
  `--fix-routing` does it on demand.
- **Volume keys dead** — expected with the Multi-Output Device; use the
  **Volume** menu or the Karabiner rule (step 5).

## Uninstalling

```bash
./uninstall.sh                  # remove the login agent + state files
./uninstall.sh --restore-routing # also put your normal audio routing back
brew uninstall blackhole-2ch     # optional: remove the driver
```

Recordings under `~/ZoomRecordings` are never touched.
