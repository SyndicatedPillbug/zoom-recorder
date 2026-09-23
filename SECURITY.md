Build a # Security & privacy notes

Written for a security reviewer (and for you, when someone asks "what does
this thing actually do?"). Everything here is verifiable from the source in
this repository.

## What it does

Records two audio tracks during a meeting: **your microphone**, and the
**other party's audio** via the standard third-party BlackHole loopback
driver. It optionally transcribes locally and, only if you ask, runs a live
transcript/answers HUD against an LLM provider you configure.

## Permissions it needs

| Permission | When | Why |
| --- | --- | --- |
| **Microphone** | always | record your side of the call |

That is the only macOS privacy permission the default path uses. It does
**not** request Screen Recording, and it does **not** request the System
Audio Recording / Audio Capture permission. System audio is captured through
BlackHole, a normal CoreAudio device.

When the live HUD is enabled, it normally opens as a native AppKit/WebKit
surface. Window mode is a decorated panel; Glass HUD mode is a translucent,
borderless `NSPanel` designed to sit over a full-screen call. Both request
macOS's legacy sharing-exclusion setting so some screen-capture paths can omit
them while they remain visible locally. This is a best-effort privacy feature,
not a security boundary: meeting applications or newer capture frameworks may
ignore it. The HUD reports the active surface and capture status, and the
browser fallback is explicitly marked as visible to capture.

The optional Core Audio **tap** mode (`--system-capture tap`, not used
automatically) is the one path that needs the System Audio Recording
permission; it exists because it leaves output routing completely untouched,
and it is strictly opt-in.

## System-level components

- **BlackHole 2ch** (`brew install blackhole-2ch`): a signed, notarized,
  widely used open-source audio driver, installed by you, not bundled here.
  It is required for loopback capture. Removing it does not affect anything
  else on the machine.
- **Login agent (optional, opt-in)**: `./install.sh --autostart` installs a
  per-user LaunchAgent (`com.zoomrecorder.menubar`) that starts the menu-bar
  toggle at login. It runs `/usr/bin/python3 <repo>/menubar.py`; there is no
  daemon, no root, no system-wide install, and it can be removed with
  `./uninstall.sh`.
- **Karabiner rule (optional)**: `karabiner/zoom-recorder-volume.json` maps
  the hardware volume keys to this tool's CLI. Karabiner executes it as a
  `shell_command`; import it only if you want that.

## What it writes

- Recordings: `~/ZoomRecordings/<date>/<time>_<id>/` (never deleted by the
  tool; `uninstall.sh` leaves them untouched).
- Small runtime state: `~/.zoom_recorder.pid`, `~/.zoom_recorder_hud.url`,
  `~/.zoom_recorder_settings.*`, `~/.zoom_recorder_control.*`,
  `~/.zoom_recorder_routing.json`.
- Optional KB cache: `~/.cache/zoom-recorder/kb`.
- Menu-bar log: `~/Library/Logs/zoom-recorder-menubar.log`.

The **Control Center** (Setup / Recordings / Settings / Help) is a local web
page served on `[IP_ADDRESS]` with a random per-run token, exactly like the
settings GUI; it shuts itself down when idle, sends no data anywhere, and
never exposes stored API keys to the browser. See `QUICKSTART.md` for the
non-technical walkthrough.

Security posture of the local servers (Control Center, settings GUI, live HUD):

- **Loopback only** (`[IP_ADDRESS]`/`localhost`), with a `Host` check against
  DNS-rebinding.
- A **random per-run token**; HTML responses carry `Referrer-Policy:
  no-referrer` and the page strips the token from the URL immediately, sending
  it in an `X-Auth-Token` header instead.
- Marker/URL files (`~/.zoom_recorder_*.url`, `*.pid`) are `chmod 0600`.
- The Control Center's privileged actions are **whitelisted server-side**: the
  terminal helper runs only fixed install commands (`brew install ...`) and the
  model downloader accepts only known whisper model filenames. `/api/open` can
  only open files under the recordings folder or the fixed documentation set.
- Recordings are **unencrypted** on disk (your own files, as with any audio
  recorder). API keys are stored plaintext in a `0600` config file; the
  environment is preferred.

## Network

Nothing leaves the machine during a normal recording. Transcription uses the
local `whisper-cli`/`whisper-cpp` binary and a local model file.

With `--live` (opt-in) the HUD can call, using **your** API key:

- Speech-to-text: `api.groq.com`, `api.openai.com`, or a local
  `whisper-server` (backend `local`).
- Answers/embeddings: `api.groq.com`, `openrouter.ai`, `api.openai.com`, or
  a local `ollama` at `http://localhost:11434`.

Audio chunks (STT) and transcript context (answers) are sent to whichever
provider you configure.

**`--offline`** is a hard switch: the HTTP client refuses every non-loopback
address, answers and KB embeddings are disabled, STT falls back to the local
backend, and desktop notifications are turned off. Local Ollama keeps
working. The same defaults can be set in
`~/.config/zoom-recorder/config.json` under `privacy.offline`.

API keys are read from the environment (`GROQ_API_KEY`, ...) or the local
config file; nothing is committed to this repository.

## What it does not do

- No telemetry, analytics, or update pings.
- No kernel extensions and no bundled drivers.
- No Screen Recording.
- No root, no system-wide installation, no network listener beyond the HUD's
  `127.0.0.1` server (random port, per-run token).

## How to verify

```bash
./zoom_record.py --doctor     # environment + permission check, with fixes
./zoom_record.py --list       # devices, transports, routing advice
./uninstall.sh --dry-run      # exactly what removal would delete
```
