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

Look for 🎙 in the menu bar. From there:

- **Start Recording** — records mic + system audio, transcribes at the end.
- **Start with Live HUD** — the live transcript window (needs an API key; see
  below).
- **Volume** — top-level volume control (slider, ±5%, mute, presets) for the
  Multi-Output Device macOS gives no volume control at all.
- **Audio Out** — which physical output to pair with the loopback, and
  *Restore Normal Routing*.

Optional: `./install.sh --autostart` enables login autostart (a per-user
LaunchAgent). Without it, nothing starts at login and `./run-menubar.command`
is how you launch the app.

## 3. The one-time microphone grant

macOS asks for **Microphone** access the first time something records. The
menu-bar app spawns the recorder, so the grant belongs to the app context
that launched it.

- If a prompt appears, click **Allow**.
- If it does not (background contexts sometimes skip the prompt), open
  **System Settings → Privacy & Security → Microphone** and enable the entry
  for the Python/menu-bar app. `./zoom_record.py --doctor` prints the exact
  pane link and tells you when the microphone is being read as digital
  silence.

No Screen Recording or System Audio Recording permission is needed for the
normal (loopback) path. `--system-capture tap` is the only mode that needs
it, and it is opt-in.

## 4. Optional: live transcript + answers

```bash
export GROQ_API_KEY=...      # or put it in ~/.config/zoom-recorder/config.json
./run-menubar.command        # then "Start with Live HUD"
```

To keep everything on the machine, run with `--offline` (no network at all)
or configure the local backends described in `README.md`. See `SECURITY.md`
for exactly what is sent where.

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
