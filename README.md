# zoom-recorder

Records Zoom meeting audio (speaker + microphone) with automatic device
selection, continuous capture verification, dynamic failover, and transcription.

## Requirements

- macOS
- Homebrew
- Python 3 (the system `python3` is fine)
- Optional: Zoom, if you want its `ZoomAudioDevice`; a general loopback
  (BlackHole) is recommended for capturing system audio.

## Setup

```bash
brew install ffmpeg whisper-cpp
mkdir -p ~/.cache/whisper-cpp
curl -L -o ~/.cache/whisper-cpp/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

# optional: menu-bar start/stop toggle
pip3 install --user rumps

# optional: capture the other party's audio (see "Capturing the other party's
# audio" below -- you also need a Multi-Output Device)
brew install blackhole-2ch
```

### Optional: live HUD

The live transcript + AI answer window (`--live`) needs an API key for one
provider. The defaults use [Groq](https://console.groq.com) (fast and cheap,
with a usable free tier):

```bash
export GROQ_API_KEY=...        # or put it in ~/.config/zoom-recorder/config.json
```

To ground answers in your own notes you need an embedding backend. The lightest
options are a local [Ollama](https://ollama.com) (`ollama pull nomic-embed-text`)
or an OpenAI key; both are selected automatically under `kb.embed_backend:
"auto"`. To run embeddings fully locally with `sentence-transformers` instead,
install it in a virtual environment (this pulls in `torch`, so keep it out of
the system Python):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install sentence-transformers
```

Without any backend the HUD still runs, just with no knowledge-base grounding.

## Usage

```bash
./zoom-record.sh              # 5-min segments, devices chosen automatically
./zoom-record.sh 10           # 10-min segments
./zoom_record.py --list          # list audio devices + defaults + routing advice
./zoom_record.py --self-test     # play a tone and verify the capture path
./zoom_record.py --check-routing # verify system audio reaches a loopback
./menubar.py                     # menu-bar toggle: click to start/stop
./settings.py                    # open the settings GUI (providers, KB, speakers, keys)
```

Press **Ctrl+C** (or click "Stop Recording" in the menu bar) to stop. Mic and
system audio are recorded and merged as **separate original files**, and
every recording gets its own timestamped + unique-ID folder so multiple
recordings on the same day never collide or overwrite each other:

```
~/ZoomRecordings/<YYYY-MM-DD>/<HH-MM-SS>_<uid>/
  recording_mic.wav       read-only original, mic track
  recording_sys.wav       read-only original, system/loopback track (if used)
  .segments/              raw crash-safe segments, archived not deleted
  derived/
    recording_mixed.wav   mixed copy for transcription only -- not an original
    transcript.txt
  capture.log
~/ZoomRecordings/manifest.jsonl   append-only sha256 + duration + coverage log
```

`<uid>` is an 8-character random ID appended to the start time (e.g.
`15-26-03_a1b2c3f4`); it exists so that even a same-second start is a hard
error (folder creation fails loudly) rather than two recordings silently
sharing one folder. Each manifest line records both `date` and `session` so
you can tell same-day recordings apart at a glance.

### Why originals are protected

On 2026-09-16, running a third-party audio enhancer with its output pointed
at the same file as the only copy of a recording silently zeroed it out, and
the raw segments had already been cleaned up by the recorder itself. To make
that structurally impossible:

- Segments are **archived, never deleted** (`.segments/`), and are a bit-exact
  fallback since the merge is a lossless concat.
- Merged originals are **chmod 0o444** (read-only) immediately after they're
  verified.
- Every run appends a line to `manifest.jsonl` with a sha256 checksum,
  duration, and signal-coverage percentage — `sha256sum` against that line
  answers "does this still match what was recorded," forever.
- Any downstream tool (transcription, enhancement, etc.) should write into
  `derived/`, never over an original. Wrap it with `safe_derive.py`, which
  refuses to run if the declared output path lands inside a recording
  session's folder (detected by the `capture.log` it contains, not by name)
  outside that session's `derived/`:

  ```bash
  ./safe_derive.py --out ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/derived/enhanced.wav -- \
      some-enhancer --in ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/recording_mic.wav \
                     --out ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/derived/enhanced.wav
  ```

### Verification on stop

Right after merging and before anything is archived or locked, the recorder:
- Sanity-checks merged duration against the sum of segment durations (catches
  a broken concat).
- Runs a full-file silence scan and reports **% of the recording with signal**
  above `--silence-db`, plus any stretches over 60s of silence.
- If coverage is below 50%, it's flagged loudly (terminal bell + macOS
  notification) instead of being something you discover during transcription
  weeks later.

Mic and system audio are **never pre-mixed** during capture — they're kept as
separate mono originals so a later enhancement/leveling pass can be applied
per-source. `derived/recording_mixed.wav` is a disposable mixdown built only
for the transcription step.

### Menu-bar activation (`menubar.py`)

One-keystroke-equivalent start/stop with no background daemon: the menu-bar
icon only spawns the recorder subprocess while you're actually recording, so
there's never a persistent, silent mic-access process for an EDR to flag or
that could record something by accident. The icon shows three states —
🎙 idle, 🔴 recording, 🧠 recording with the live window up — and the
menu gives the HUD its own options, separate from the plain recording toggle:

```
Start Recording        ↔ Stop Recording
Start with Live HUD    → Live HUD active ✓   (disabled while recording)
Open Live HUD…                                (enabled only while the HUD runs)
Settings…                                     (config GUI)
Quit
```

Click "Stop Recording" to send the same clean-shutdown signal Ctrl+C would;
**Open Live HUD…** re-opens the window in the browser.

The status icon is intentionally a **single glyph** (🎙 / 🔴 / 🧠) to keep it
narrow. On MacBooks with a notch, macOS hides menu-bar items that don't fit,
and status items overflow leftward under the notch — see
[Menu-bar icon hidden by the notch](#menu-bar-icon-hidden-by-the-notch) if the
icon disappears.

**Note:** the repo must live outside `~/Documents`, `~/Desktop`, and
`~/Downloads`. Those are TCC-protected on macOS, and a process spawned by
launchd (as opposed to one you run from Terminal, which already has its own
granted access) gets silently denied trying to even read the script.

#### Auto-start at login

```bash
./install-launch-agent.sh     # installs a LaunchAgent, starts it now, and
                               # makes it start automatically at every login
./uninstall-launch-agent.sh   # removes it
```

This only registers `menubar.py` (the always-visible toggle) to start at
login -- never the recorder itself. Logs go to
`~/Library/Logs/zoom-recorder-menubar.log`. If it ever crashes, launchd
restarts it automatically (`KeepAlive` on non-zero exit only, so quitting it
yourself via the menu doesn't trigger an immediate respawn).

#### If it's not running for some reason

Double-click **`run-menubar.command`** in Finder (or run it from a shell). It
restarts the LaunchAgent if one is installed, or starts `menubar.py` directly
otherwise. Safe to run at any time, including while it's already up.

### Live transcript + AI answer HUD (`--live`)

`--live` opens a small local window in your browser: **live transcript on the
left**, and on the right a persistent **talking-points** bullet list above a
**Q&A** column. Both are generated from the conversation and, when configured,
a background database of `.md` files:

```bash
./zoom_record.py --live                          # Groq STT + Groq answers
./zoom_record.py --live --kb-dir ~/notes         # ground answers in your notes
./zoom_record.py --live --live-no-answers        # offline transcript only
./zoom_record.py --live --answer-backend openrouter   # use OpenRouter instead
```

The window is served from `127.0.0.1` (a random free port) and closes when the
recording stops. These files are written under `derived/` (flushed every
`hud.persist_seconds`, default 20, so a crash doesn't lose the session):

| File | Contents |
| --- | --- |
| `live_transcript.txt` | The transcript alone (unchanged transcript behaviour) |
| `live_conversation.md` | Transcript **and** answers interleaved in order, so each answer sits next to the speech that prompted it |
| `live_answers.md` | Just the AI answers and talking points |
| `live_summary.md` | End-of-call summary, action items and a follow-up email draft |

The window itself is interactive: type a question in the **Ask** box at the
bottom of the Q&A pane, click a talking point's **pin** to keep it at the top,
and **copy** on any point or answer. **Pause answers** stops AI generation while
the transcript keeps running. The header shows a live STT **lag** indicator, the
input **level vs the speech threshold**, and the **mic/system devices** in use,
with a warning banner when the other party's audio can't be captured (see
*Capturing the other party's audio* below).

**Speaker labels (partial diarization).** Because the mic and the
system/loopback are captured as *separate channels*, the HUD can attribute
speech without any diarization model: the mic is labelled with your name and
the loopback with the other party's. Set the names in the config or with
`--self-name` / `--remote-name`:

```bash
./zoom_record.py --live --self-name "Dana" --remote-name "Client"
```

Each stream is transcribed independently and tagged, so the transcript reads
`[10:14:02] Dana: ...` / `[10:14:09] Client: ...`. This is exact for two
parties; multiple *remote* speakers all fall under the single loopback label
(that case is what full diarization would be needed for). Multiple speakers
means two STT streams, so audio usage roughly doubles. `--no-speaker-labels`
mixes mic + system into one unlabelled stream as before.

**How it works.** A dedicated, isolated `ffmpeg` process taps the same mic +
loopback devices the recorder uses and emits 16 kHz mono PCM. Speech is
detected by a **voice-activity gate** — an adaptive noise floor per source, or
`webrtcvad` when it is installed — so steady room noise (air conditioning, fan
hum) is never sent to be transcribed, and chunks are queued to per-source STT
workers (a slow call never makes the tap fall behind; if it does, the lag is
shown and stale audio is dropped to stay live). The transcript is either
transcribed by a remote OpenAI-compatible endpoint or locally with whisper.cpp,
seeded with a short, sentence-aligned context prompt plus a configurable
glossary (`stt.glossary`). Whisper's known non-speech output is filtered twice:
at the segment level (using `verbose_json` `no_speech_prob` / `avg_logprob` /
`compression_ratio`) and with a text filter that drops repetition loops and
canned silence phrases. The HUD then runs two independent streams:
**questions** are detected across the recent conversation (not just the newest
chunk) and answered with the stronger model, giving it the surrounding turns,
the earlier Q&A, and your notes so follow-ups like *"what about the other one?"*
resolve correctly; **talking points** are refreshed every ~35 s on the cheap
model and appended, deduplicated (lexically and semantically), to their own
persistent pane — they never get mixed into the Q&A cards. Talking points are
strictly **transcript-grounded**: the model must supply a verbatim quote for
each one, that quote is verified locally, and anything unsupported is dropped.
A minimum amount of new speech is required before a refresh fires, so sparse or
noisy audio produces **no** points rather than invented ones. Nothing in the HUD
can affect the recording — if it fails to start, recording proceeds normally.

**Voice activity (optional).** Speech is detected with an adaptive noise-floor
gate by default, which rejects steady hum without any dependency. Installing
`webrtcvad` (`pip install webrtcvad`) switches to a real VAD automatically
(`stt.vad_backend: "auto"`); set it to `"energy"` to force the stdlib gate or
`"webrtcvad"` to require the package. `stt.hallucination_filter` and the
confidence thresholds control the non-speech text filter.

The HUD header shows `lvl <level>/<threshold> dB` while recording. If dialogue
isn't appearing, that pill tells you why:

- **level stays below threshold** — the gate is too strict for your input.
  Lower `stt.vad_margin_db` (default 6), or set `stt.adaptive_vad: false`
  (fixed peak gate at `stt.silence_db`, the original behaviour), or force
  `stt.vad_backend: "energy"`.
- **level clears threshold but nothing appears** — turn off `stt.verbose_stt`
  (segment confidence gating) or `stt.hallucination_filter` to isolate the text
  filter, and check the log for `dropped likely hallucination` lines.

**Capturing the other party's audio (macOS).** macOS cannot capture arbitrary
system audio out of the box. You need a **loopback** device that mirrors your
output, and it must actually be in the output path:

1. Install BlackHole: `brew install blackhole-2ch`.
2. Open **Audio MIDI Setup → + → Create Multi-Output Device**, tick **both** your
   headphones/speakers **and** `BlackHole 2ch`.
3. Select that Multi-Output Device as the system output (and in Zoom).

System audio is then mirrored into BlackHole and captured automatically; the
HUD shows `mic: … · sys: BlackHole 2ch`. Note that `ZoomAudioDevice` is **not**
a general loopback — it only carries audio Zoom itself shares, so it is ranked
last and flagged.

`./zoom_record.py --list` prints every device with its transport, the current
defaults, and exactly what to fix; `./zoom_record.py --check-routing` plays a
tone and verifies that it reaches a loopback. If system audio can't be
captured, the HUD shows a warning banner and flags the devices pill, so room
audio isn't mistaken for the remote party. Plugging in headphones or switching
to Bluetooth changes the route mid-call; the recorder re-detects it and
re-resolves the source, and the HUD follows along.

**Providers.** All are OpenAI-compatible, so the same client serves each of
them. Set `answers.backend` / `stt.backend` or use the `--answer-backend` /
`--stt-backend` flags:

| Provider | STT | Answers | Notes |
| --- | --- | --- | --- |
| `groq` (default) | ✅ `whisper-large-v3-turbo` | ✅ `gpt-oss-120b` / `gpt-oss-20b` | Fastest/cheapest; token- and audio-second-limited, not context-limited |
| `openrouter` | — | ✅ | Use GPT-4o/Claude/Gemini; LLM routing only, no STT |
| `openai` | ✅ `whisper-1` | ✅ | One key for both |
| `ollama` | — | ✅ | Fully local; pair with `--stt-backend local` |
| `local` | ✅ whisper.cpp | — | Private; needs `--model` pointing at a ggml file |

Talking points use the lighter model (`openai/gpt-oss-20b` on Groq) while
detected questions use the stronger one (`openai/gpt-oss-120b`). By default only
questions from the other party are answered (`answers.answer_self_questions`
includes your own); rhetorical/backchannel questions are skipped. When a
follow-up question is ambiguous (short, or full of *it/that/the other one*), one
extra cheap call first rewrites it into a self-contained question using the
recent turns; unambiguous questions cost nothing extra. The budget governor
trusts the provider's own rate-limit headers, so it adapts to whatever plan
you're on; if a daily token cap exists and runs low, talking-point refreshes are
dropped first so question answers keep working. Set `budget.tpm` / `budget.tpd`
in the config to impose limits below the provider's.

**Configuration.** Defaults can be set in `~/.config/zoom-recorder/config.json`
(keep it `chmod 600`); environment variables always win:

```json
{
  "stt":     {"backend": "groq", "chunk_seconds": 10, "glossary": ["Acme", "Q3"],
               "vad_backend": "auto", "vad_margin_db": 6, "hallucination_filter": true},
  "answers": {"backend": "groq", "interval": 35, "rolling_enabled": true,
               "context_minutes": 5, "question_rewrite": true,
               "answer_self_questions": false, "summary_enabled": true,
               "talking_points_grounded": true, "talking_points_max": 3,
               "talking_points_min_new_words": 60,
               "fallback": ["openrouter", "ollama"]},
  "kb":      {"dirs": ["~/notes"], "top_k": 5, "embed_backend": "auto"},
  "hud":     {"port": 0, "open_browser": true, "persist_seconds": 20},
  "speakers": {"enabled": true, "self_name": "You", "remote_name": "Others"},
  "api_keys": {"openrouter": "sk-or-..."}
}
```

**Grounding in your notes (knowledge base).** Point `kb.dirs` / `--kb-dir` at a
folder of `.md` files and the HUD retrieves the few most relevant snippets for
each answer, citing the file. Embeddings are pluggable via `kb.embed_backend`:

| Backend | Where it runs | Needs |
| --- | --- | --- |
| `sentence-transformers` | fully local | `pip install sentence-transformers` (heavy: pulls torch) |
| `ollama` | fully local | an [Ollama](https://ollama.com) server with an embedding model (`ollama pull nomic-embed-text`) |
| `openai` | remote | an OpenAI key (`text-embedding-3-small`) |
| `auto` (default) | picks the first available of the above | — |

The index is cached locally and rebuilt only when files change. With a remote
embedding backend, note chunks leave the machine; with a local backend or
`sentence-transformers`, they never do.

**Privacy.** With `--stt-backend groq/openai` the **audio** leaves the machine;
with answers enabled the **transcript text** (plus relevant snippets from your
`.md` files) is sent to the answer provider. If `kb.embed_backend` is `openai`,
note chunks are also sent to OpenAI to compute embeddings. The HUD header always
shows the egress state. For a fully local setup, use `--stt-backend local` with
an `ollama` answer backend and a local embedding backend
(`sentence-transformers` or `ollama`).

### Settings GUI

Everything above is editable in a simple local form instead of hand-editing
JSON — open it from the menu bar (**Settings…**) or run it directly:

```bash
./settings.py                 # opens a localhost page
./settings.py --no-browser    # just start it (prints the URL)
./settings.py --timeout 0     # never auto-close
```

The page covers STT, answers, knowledge base, speakers, HUD/budget and API
keys, with live model dropdowns (from each provider's `/models`), a **Test**
button per provider, and a native folder picker for KB directories. It binds
loopback only, requires a random one-time token, never displays stored API keys
(shows `configured ✓` / `not set`), writes the file atomically with a `.bak`
backup, and shuts down when idle — there's no persistent server. Changes apply
to the **next** recording, since the HUD reads config at session start.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mic NAME` | auto | Force a microphone by name (env `MIC_AUDIO_DEVICE`) |
| `--system NAME` | auto | Force the system/loopback input (env `ZOOM_AUDIO_DEVICE`) |
| `--no-system` | off | Record the microphone only |
| `--list` | — | List audio devices (transport, defaults, loopback advice), then exit |
| `--self-test` | off | Play a tone and verify the output→loopback capture path |
| `--check-routing` | off | Verify system audio reaches a loopback, then exit |
| `--chunk-seconds N` | 5 | How often the active mic is tested |
| `--fail-threshold N` | 3 | Consecutive silent checks before cycling inputs |
| `--cycle-seconds N` | 60 | How often every inactive input is tested |
| `--probe-seconds N` | 1.5 | Length of each signal probe |
| `--silence-db N` | -60 | `max_volume` below this counts as silence |
| `--no-transcribe` | off | Skip transcription |
| `--model PATH` | base.en | Whisper model to use |
| `--live` | off | Open the live transcript + AI answer HUD |
| `--live-no-answers` | off | Live transcript only; never call an answer provider |
| `--no-live-summary` | off | Skip the end-of-call summary/action items/email |
| `--hud-port N` | random | Port for the local HUD |
| `--no-hud-browser` | off | Do not auto-open the HUD in a browser |
| `--stt-backend X` | groq | Live STT backend: `groq` \| `openai` \| `local` |
| `--stt-chunk-seconds N` | 10 | Live STT chunk length |
| `--glossary-term WORD` | none | Name/acronym to bias live transcription (repeatable) |
| `--answer-backend X` | groq | Answer provider: `groq` \| `openrouter` \| `openai` \| `ollama` |
| `--answer-interval N` | 35 | Seconds between rolling talking-point refreshes |
| `--kb-dir PATH` | none | Directory of `.md` files for context (repeatable) |
| `--kb-top-k N` | 5 | Knowledge-base snippets per answer |
| `--kb-embed-backend X` | auto | KB embeddings: `auto` \| `sentence-transformers` \| `ollama` \| `openai` |
| `--kb-reindex` | off | Rebuild the embedding index |
| `--self-name NAME` | You | Label your microphone audio with this name in the transcript |
| `--remote-name NAME` | Others | Label the system/loopback audio with this name |
| `--no-speaker-labels` | off | Mix mic+system into one unlabelled stream |
| `--live-audio-file PATH` | none | Feed a media file to the HUD instead of a live tap (testing) |

## How It Works

- Audio devices are matched by **name**, not positional index (avfoundation's
  index order changes with what's connected and whether Zoom is outputting
  audio), and the device **topology** is read from `system_profiler` so the app
  knows the current default input/output and each device's transport.
- The **best mic is selected automatically**: virtual/loopback devices are
  excluded, the system default input is preferred, and every candidate is
  probed with ffmpeg's `volumedetect` — the pick prefers a device that is
  actually delivering signal, then falls back to a quality ranking
  (wired external > built-in > Bluetooth).
- The **system source is a real loopback** (BlackHole, the Loopback app,
  Soundflower, or a Multi-Output Device), preferred over `ZoomAudioDevice`,
  which only carries audio Zoom itself shares. If there is no loopback at all,
  the recorder records microphone-only and prints the exact fix; if a loopback
  exists but isn't in the output path (or is silent), it's flagged rather than
  silently trusted.
- Microphone and system audio are **never mixed at capture**: they are written
  as **two separate mono tracks** (crash-safe segments), so a later
  enhancement/leveling pass can be applied per-source. The only mixdown is a
  disposable `derived/recording_mixed.wav` for transcription.
- **Every `--chunk-seconds` the active mic is tested.** A dead capture reads about
  `-91 dB`, so `--silence-db` cleanly separates a dead input from a live one.
- After `--fail-threshold` consecutive silent checks, the recorder **cycles
  through every other candidate**, probes them, and switches to the best one
  that produces signal. The current capture keeps running throughout, so
  natural meeting silence never drops audio.
- When the **default input/output changes mid-call** (headphones, Bluetooth),
  the recorder re-reads the topology and re-resolves the affected source; the
  live HUD follows the switch and restarts only that STT source.
- Every `--cycle-seconds` all inactive inputs are tested and their levels logged.
- Every run writes `capture.log` next to the recording, documenting the devices
  selected, every probe level, and every failover.

## Troubleshooting

- **Recording is silent** — the recorder now detects this itself and will fail
  over to another input. Check `capture.log` to see which devices were probed
  and why a switch did or did not happen.
- **The other party isn't in the transcript, or YouTube was labelled as you** —
  macOS system audio needs a loopback in the output path. Run
  `./zoom_record.py --list` (prints the default output and the exact fix) and
  `./zoom_record.py --check-routing` (plays a tone and verifies capture). The
  usual fix: install [BlackHole](https://github.com/ExistentialAudio/BlackHole)
  and add it to a Multi-Output Device — see
  *Capturing the other party's audio (macOS)* above. `ZoomAudioDevice` alone is
  not enough; it only carries audio Zoom itself shares.
- **Wrong device selected** — pass `--mic NAME` / `--system NAME` (or set
  `MIC_AUDIO_DEVICE` / `ZOOM_AUDIO_DEVICE`); `--list` shows exact names.
- **Is the capture path actually working?** — run `./zoom_record.py --self-test`
  (or `--check-routing`); it plays a short tone and checks whether a loopback
  input records it.

### Menu-bar icon hidden by the notch

On MacBooks with a notch, macOS **hides menu-bar items that don't fit** —
status items overflow leftward into the notch, so the 🎙 can be pushed out of
view even though the app is running. Fixes, in order of effort:

1. **Reposition it** — hold **Command** and drag the 🎙 icon toward the right
   end of the menu bar (past other icons). If you can't grab it because it's
   fully hidden, use Ice below.
2. **Use a menu-bar manager (recommended)** — install
   [Ice](https://icemenubar.app) (free, open-source):

   ```bash
   brew install --cask jordanbaird-ice
   ```

   Launch it and grant **Accessibility** permission (System Settings → Privacy
   & Security → Accessibility). Then open **Ice → Settings → Menu Bar Layout**,
   find our item (owner **Python**, microphone glyph) and drag it into the
   visible section; drag anything you don't need into the Hidden section to
   free space. Ice shows *all* items there, including ones hidden under the
   notch, so it's the reliable fix.
3. **Bypass the menu bar** — `./settings.py` always opens the settings GUI from
   a terminal, regardless of menu-bar state.

Note: Ice adds its own status icon; if the bar is very full, hide that too
(Ice → Settings → General → "Show Ice icon") and use the hotkey instead.

### Where the HUD settings live

The settings GUI (`./settings.py`, or **Settings…** in the menu bar) edits
`~/.config/zoom-recorder/config.json` (chmod `600`). The full set of keys is
shown under **Configuration** in the HUD section above; the GUI covers all of
them and never displays stored API keys.

## Version history

Annotated tags mark each milestone (`git tag -n` for the full messages):

| Tag | Highlights |
| --- | --- |
| `v1.0-core` | Robust mic + system recorder: verification, manifest, post-hoc transcription |
| `v1.1-hud` | Live transcript + AI answer HUD (`--live`), local KB, derived outputs |
| `v1.2-settings` | Settings GUI, channel-based speaker labels, menu-bar docs |
| `v1.3-grounding` | Transcript-grounded talking points, faster STT/answers, HUD controls (ask/pause/copy/pin), end-of-call summary |
| `v1.4-stt-hallucination` | Adaptive VAD, `verbose_json` segment confidence gating, text hallucination filter, prompt hygiene |
| `v1.5-routing` | macOS audio topology, BlackHole-first loopback selection, route re-detection, HUD device status, `--list`/`--check-routing` |

Running the tests: `python3 -m unittest discover -s tests`.
