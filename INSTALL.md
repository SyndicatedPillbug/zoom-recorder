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

# Local post-call speaker diarization and reusable voice matching (enabled by default):
./install.sh --install-diarization
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
- **Live assistant ▸** — start the live transcript, reopen its HUD, pause or
  resume answers, and reach answer/diarization settings.
- **HUD: Window ▸** — choose the stable native panel, Glass HUD overlay, or
  compact Glass layout. Appearance changes apply to the next recording.
- **Transcript and context ▸** — configure transcript writeback, Obsidian or
  knowledge-base context, and speaker handling.
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

No Screen Recording permission is needed. The normal `auto` path uses the
Core Audio tap when the current process has System Audio Recording permission,
and falls back to BlackHole/Multi-Output otherwise. Explicit `--system-capture
tap` requires that permission; explicit `loopback` never does.

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

On macOS, **Start with live transcript** opens the native Window mode by
default. Settings also offers **Glass HUD**: a translucent, borderless overlay
for full-screen calls with adjustable opacity and compact density. Both modes
stay above normal windows and follow Spaces where macOS allows it. The native
surface requests best-effort capture exclusion, but that is not a guarantee for
every meeting application's screen-sharing path. Use window sharing or a
second display when the HUD must remain private.

Multi-speaker attribution is a separate, post-call feature. It is enabled by
default in the Control Center and cannot affect live capture or answer
latency. The supported setup installs the native NeMo-Speech runtime and its
local Sortformer model. On Apple Silicon it uses Metal; on Linux AMD it uses
Vulkan where available, or CPU. The model is downloaded once and then
inference is local. No Hugging Face token is required. The result is written
under `derived/`; missing local setup or a failed pass leaves the ordinary
transcript intact and is reported visibly in the Control Center.

During any live session, click a speaker label in the transcript to enter a
participant name. Names are saved as local session metadata and do not require
network access.

If the optional diarization backend provides acoustic embeddings, explicit
manual labels can also build a local reusable voice profile. Those profiles are
stored owner-only under the app configuration directory, contain no audio, and
can be disabled or deleted from Settings.

Reusable voice matching is a separate local layer. Explicit manual labels are
used to enroll profiles; generic diarization output is never silently treated
as a confirmed identity.

To keep everything on the machine, use **Settings → Offline mode** (blocks
all internet access). See `SECURITY.md` for exactly what is sent where.

### Obsidian context and session identity

In the native Main App, **Context for the next meeting** lets you choose a
meeting-specific set of folders without changing the permanent library. The
exact selection is saved in `derived/context_sources.json`; the app reports
whether it is indexed, needs indexing, contains no Markdown, or is unavailable.
Indexing runs in the background and uses a local semantic backend when one is
installed, with a deterministic local lexical/hash fallback always available.

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
