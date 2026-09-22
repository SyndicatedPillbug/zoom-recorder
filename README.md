# zoom-recorder

Records Zoom meeting audio (speaker + microphone) with automatic device
selection, continuous capture verification, dynamic failover, and transcription.

> New here? **QUICKSTART.md** is the no-terminal guide for non-technical users,
> **INSTALL.md** has the five-minute setup, and **SECURITY.md** documents
> exactly what the tool accesses, writes and (optionally) sends over the
> network. `./zoom_record.py --doctor` checks the environment and prints fixes.

## Requirements

- macOS
- Homebrew
- Python 3 (the system `python3` is fine)
- Optional: Zoom, if you want its `ZoomAudioDevice`; a general loopback
  (BlackHole) is recommended for capturing system audio.

## Setup

```bash
git clone <this repo> ~/zoom-recorder
cd ~/zoom-recorder
./install.sh --install-deps   # checks/installs ffmpeg, BlackHole, rumps
# Optional post-call speaker attribution and reusable voice profiles:
./install.sh --install-diarization
./.venv-diarization/bin/hf auth login
./run-menubar.command         # start the menu bar
```

`./install.sh` on its own only checks and reports; `--dry-run` changes
nothing. See **INSTALL.md** for the one-time microphone grant and
troubleshooting, and **SECURITY.md** for exactly what this tool accesses,
writes and (optionally) sends over the network.

Manual equivalent of `--install-deps`:

```bash
brew install ffmpeg whisper-cpp
mkdir -p ~/.cache/whisper-cpp
curl -L -o ~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

pip3 install --user rumps        # menu-bar UI
brew install blackhole-2ch       # system-audio loopback (asks for admin)
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
🎙 idle, 🔴 recording, 🧠 recording with the transcript window up:

```
Volume 44%                                   (top-level: slider, ±5%, mute, presets)
Start recording      ↔ Stop recording (12:34)
Start with live transcript → Live transcript active ✓   (disabled while recording)
Open transcript window                                   (enabled while it runs)
My recordings…                                           (Control Center → Recordings)
Check my audio setup…                                    (Control Center → Setup)
Settings…                                                (Control Center → Settings)
Help                                                     (Control Center → Help)
Play sound through ▸                                     (device pairing, Reset audio)
Quit zoom-recorder                                       (stops and saves if recording)
```

The **Control Center** (`hud/control.py`) is a local, loopback-only page with
four tabs — Setup (guided checks, microphone test, output picker, a ten-second
test recording, transcription setup), Recordings (past sessions with
transcript/summary/play/Show-in-Finder, plus **Move to Trash** — recordings are
never permanently deleted), Settings (Basics + Advanced), and
Help. The first run opens Setup automatically and runs the audio check.

Click "Stop recording" to send the same clean-shutdown signal Ctrl+C would;
**Open transcript window** re-opens the window in the browser (you can also
stop the recording from that window's **Stop recording** button).

The status icon is intentionally a **single glyph** (🎙 / 🔴 / 🧠) to keep it
narrow. On MacBooks with a notch, macOS hides menu-bar items that don't fit,
and status items overflow leftward under the notch — see
[Menu-bar icon hidden by the notch](#menu-bar-icon-hidden-by-the-notch) if the
icon disappears.

**Note:** the repo must live outside `~/Documents`, `~/Desktop`, and
`~/Downloads`. Those are TCC-protected on macOS, and a process spawned by
launchd (as opposed to one you run from Terminal, which already has its own
granted access) gets silently denied trying to even read the script.

#### Auto-start at login (optional)

```bash
./install.sh --autostart      # installs the LaunchAgent and starts it now
./uninstall.sh                # removes it (and other state)
```

Autostart is opt-in: without it, nothing runs at login and you launch the
menu bar with `./run-menubar.command`. The agent only registers
`menubar.py` (the always-visible toggle) to start at login -- never the
recorder itself, and it runs `/usr/bin/python3 <repo>/menubar.py` directly
(no bundle, no wrapper). Logs go to
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
./zoom_record.py --live --transcript-dir ~/Obsidian/LiveTranscripts
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
| `diarization.json` | Optional post-call speaker turns and confidence metadata |
| `diarized_transcript.md` | Optional derived transcript with generic remote speaker IDs |

The live session also writes `live_events.jsonl` (a provider-free replay
fixture), `meeting_memory.json` (decisions, commitments and numeric facts with
evidence), and `live_diagnostics.json` (non-secret STT/answer timing data).

On a clean stop, the numeric session folder is finalized as
`<HH-MM-SS>_<topic-slug>_<collision-id>`, for example
`14-32-08_enrollment-planning_a1b2c3d4`. The topic is derived from the first
substantive transcript line and recorded in `session.json` with its evidence
hash, timestamps, participants, models and original folder name. If there is
not enough speech, a timestamp fallback is used. Existing numeric folders
remain readable.

The window itself is interactive: type a question in the **Ask** box at the
bottom of the Q&A pane, click a talking point's **pin** to keep it at the top,
and **copy** on any point or answer. **Pause answers** stops AI generation while
the transcript keeps running. The header shows a live STT **lag** indicator, the
input **level vs the speech threshold**, and the **mic/system devices** in use,
with a warning banner when the other party's audio can't be captured (see
*Capturing the other party's audio* below).

**Optional live transcript mirror.** Set `transcript.writeback_dir` in the
settings page, choose **Transcript mirror**, or pass `--transcript-dir PATH`.
The recorder creates one unique Markdown document per live session in that
folder and appends recognized transcript lines from a background writer. This
is an additional destination: the normal recording folder, `derived/`, and
their crash-safe files are unchanged. If macOS denies the folder, the app logs
the writeback failure and continues recording and transcribing normally.

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

**Renaming participants.** Click any speaker label in the live transcript, type
the participant's name, and press Enter. Escape cancels; clearing the field
restores the captured generic label. This is a local metadata override: it is
instant, does not call an AI provider, and changes historical display/writeback
views without rewriting raw audio or transcript events. The mapping is saved in
`session.json`.

**Optional post-call diarization.** For calls with several people sharing one
remote/system channel, the safe first step is an opt-in background pass after
capture. It never delays live STT, question detection, or answer TTFT. The
supported setup installs WhisperX and its diarization dependencies in the
repo-local `.venv-diarization` environment; the app finds that environment
automatically. Authenticate with `hf auth login` or set `HF_TOKEN`, then run:

```bash
./zoom_record.py --live --diarize --self-name "Dana"
```

The pass processes only the saved remote track when one exists, uses fixed
non-shell arguments, keeps the credential out of the child process arguments,
has a timeout, and writes only derived files. If WhisperX, the model, or the
token is missing, the call completes with the normal channel-labelled
transcript. Speaker IDs remain generic (`Remote 1`, `Remote 2`) until the user
renames them; acoustic attribution never silently claims a real person's
identity.

**Reusable voice matches.** When a diarization backend supplies acoustic
embeddings, an explicit manual name can enroll one representative sample into
the local voice-profile store at `~/.config/zoom-recorder/voice_profiles.json`.
Later calls can use those profiles to raise or lower confidence, but a close
match is still marked `voice_profile` and remains generic below the configured
threshold. The store contains aggregate embeddings and metadata, not audio;
it is created with owner-only permissions. Disable **Reuse confirmed voices**
in Settings to stop matching, and delete the profile file to erase stored
matches. When enabled, WhisperX emits the speaker embeddings during the same
post-call pass; otherwise it safely falls back because WhisperX's session-local
speaker IDs are not reusable identities by themselves.

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
chunk), including indirect interview questions without a question mark and
questions split across adjacent STT chunks. They are answered with the stronger
model, giving it the surrounding turns,
the earlier Q&A, and your notes so follow-ups like *"what about the other one?"*
resolve correctly; **talking points** are refreshed every ~35 s on the cheap
model and appended, deduplicated (lexically and semantically), to their own
persistent pane — they never get mixed into the Q&A cards. Talking points are
strictly **transcript-grounded**: the model must supply a verbatim quote for
each one, that quote is verified locally, and anything unsupported is dropped.
A minimum amount of new speech is required before a refresh fires, so sparse or
noisy audio produces **no** points rather than invented ones. Question jobs and
talking-point jobs have separate queues and workers; stale talking-point work
is dropped when the provider lane is busy. Nothing in the HUD can affect the
recording — if it fails to start, recording proceeds normally.

For private low-latency transcription, local whisper.cpp is a practical option
on Apple silicon. The installed runtime uses Metal and BLAS on this Mac. The
24 GB M4 Air was measured with the official `large-v3-turbo-q5_0` model: a
persistent local server transcribed a 7-second speech chunk in about 1.17 s,
versus about 0.11 s for `base.en`. That is roughly 6x real-time for turbo, so
turbo is now the local baseline; `base.en` remains available as a speed-first
fallback when a smaller machine needs it.
The quantized model is about 547 MiB on disk. Use it explicitly with:

```bash
./zoom_record.py --live --stt-backend local \
  --model ~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin
```

With two remote Groq streams, the recorder raises sub-7-second chunks to 7
seconds to stay under the provider's current STT request rate; local Whisper
keeps the configured chunk size. The answer loop wakes on new transcript
events instead of waiting for a fixed polling tick, so question detection
starts immediately after recognition. Local mode also removes STT network
latency, audio egress, and Groq audio-second usage, although it does not make
the Whisper computation itself faster than Groq's hosted hardware.

Local mode also enables near-real-time interim words by default. It re-decodes
an overlapping two-second window about every 0.8 seconds. The HUD shows the
newest unstable words as a muted live draft; words are promoted to the normal
transcript only after they remain stable across windows. Draft words are never
written to the optional transcript mirror, indexed in the live KB, or used as
answer evidence. This is intentionally a stable-word stream rather than false
certainty: the final chunk pass remains authoritative and removes any overlap.
Set `stt.partial_enabled` to `false`, or adjust
`stt.partial_window_seconds` / `stt.partial_interval_seconds`, when running a
smaller or thermally constrained Mac. Remote Groq remains on the chunked path
because its current transcription API accepts uploaded audio files rather than
an app-side audio stream.

The HUD's Details view now exposes the measurements needed to tune latency:
STT lag and interim latency, answer queue wait, prompt assembly time and size,
provider time-to-first-token, and total provider time. Talking-point refreshes
are lower priority and are not admitted while a question is queued or already
being answered; their own queue wait is recorded separately.

Question answers now request claim-level evidence. Claims with an explicit
quote or note source are checked locally against the conversation and retrieved
notes before they are shown; dropped claims are recorded on the answer event.
The app also keeps a small deterministic meeting-memory stream for decisions,
commitments, and numeric facts. It is updated off the answer loop and each item
keeps the exact transcript evidence that produced it.

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
system audio out of the box; the recorder has two capture paths:

1. **Loopback / Multi-Output (default).** A Multi-Output Device (BlackHole +
   your real output) is created and selected automatically at recording start
   (idempotent; the pairing lives in `~/.zoom_recorder_routing.json`), and the
   default output is handed back to your real device when the recording stops,
   so the normal volume keys work between calls. This path needs no privacy
   permission beyond the microphone. Because macOS gives Multi-Output Devices
   no volume control at all, the menu bar has a top-level **Volume** menu (its
   title always shows the current level): a slider, `Volume +5%` /
   `Volume −5%`, `Mute`/`Unmute`, and 25/50/75/100% presets. The same controls
   are available from the CLI:
   `python3 -m hud.routing_fix --volume 40 | --volume-up | --volume-down |
   --mute | --unmute | --toggle-mute`. To put the volume keys back on the
   hardware keys, import `karabiner/zoom-recorder-volume.json` in Karabiner
   Elements (Complex Modifications → Add rule → Import more rules from a
   file); it maps volume up/down/mute to those CLI actions in both modes.
   Muting silences your speakers/headphones only — the recording keeps
   capturing system audio. `--restore-routing` (also in the `Audio Out ▸`
   menu) removes the routing entirely when you are done with loopback mode.
2. **Core Audio process tap (`--system-capture tap`, opt-in).** Where the
   calling app context holds the **System Audio Recording** permission (e.g.
   Apple Terminal), `hud/system_tap.py` creates a *private, observe-only* tap
   that mirrors every playing process. Output routing is completely untouched
   and the recording level is independent of the volume slider. It is never
   chosen automatically — the audio-capture permission path stays opt-in — and
   `python3 -m hud.system_tap` self-tests it.

`./zoom_record.py --list` prints every device with its transport, the current
defaults, the capture mode, and exactly what to fix; `--self-test` plays a
tone and verifies the system track end to end. If system audio can't be
captured, the HUD shows a warning banner and flags the devices pill, so room
audio isn't mistaken for the remote party. Switching to Bluetooth mid-call
changes the mic route; the recorder re-detects it and re-resolves the mic
(the tap side follows on its own), and the HUD follows along.

**Providers.** All are OpenAI-compatible, so the same client serves each of
them. Set `answers.backend` / `stt.backend` or use the `--answer-backend` /
`--stt-backend` flags:

| Provider | STT | Answers | Notes |
| --- | --- | --- | --- |
| `groq` (default) | ✅ `whisper-large-v3-turbo` | ✅ `gpt-oss-120b` / `gpt-oss-20b` | Fastest/cheapest; token- and audio-second-limited, not context-limited |
| `openrouter` | — | ✅ | Use GPT-4o/Claude/Gemini; LLM routing only, no STT |
| `openai` | ✅ `whisper-1` | ✅ | One key for both |
| `ollama` | — | ✅ | Fully local; pair with `--stt-backend local` |
| `local` | ✅ whisper.cpp (`large-v3-turbo-q5_0`) | — | Private; the turbo model is the baseline and needs a one-time download |

Talking points use the lighter model (`openai/gpt-oss-20b` on Groq) while
detected questions use the stronger one (`openai/gpt-oss-120b`). By default only
questions from the other party are answered (`answers.answer_self_questions`
includes your own); rhetorical/backchannel questions are skipped. Only very
short fragments are treated as inherently ambiguous; a longer, self-contained
interview question is not forced through the rewrite path. When a
follow-up question is ambiguous (or full of *it/that/the other one*), one
extra cheap call first rewrites it into a self-contained question using the
recent turns; unambiguous questions cost nothing extra. The budget governor
trusts the provider's own rate-limit headers, so it adapts to whatever plan
you're on; if a daily token cap exists and runs low, talking-point refreshes are
dropped first so question answers keep working. Set `budget.tpm` / `budget.tpd`
in the config to impose limits below the provider's.

When Groq returns a transient rate limit, the answer worker tries the next
configured provider before pausing the answer queue. STT uses a short provider
backoff and drops stale queued audio rather than allowing the live transcript
to drift minutes behind the call.

**Configuration.** Defaults can be set in `~/.config/zoom-recorder/config.json`
(keep it `chmod 600`); environment variables always win:

```json
{
  "stt":     {"backend": "groq", "chunk_seconds": 5, "partial_enabled": true,
               "partial_window_seconds": 2, "partial_interval_seconds": 0.8,
               "glossary": ["Acme", "Q3"],
               "vad_backend": "auto", "vad_margin_db": 6, "hallucination_filter": true},
  "answers": {"backend": "groq", "interval": 35, "rolling_enabled": true,
               "context_minutes": 5, "question_rewrite": true,
               "answer_self_questions": false, "summary_enabled": true,
               "talking_points_grounded": true, "talking_points_max": 3,
               "talking_points_min_new_words": 60,
               "fallback": ["openrouter", "ollama"]},
  "kb":      {"dirs": ["~/notes"], "top_k": 5, "embed_backend": "auto"},
  "hud":     {"port": 0, "open_browser": true, "persist_seconds": 20},
  "transcript": {"writeback_dir": "~/Obsidian/LiveTranscripts"},
  "diarization": {"enabled": false, "backend": "auto", "timeout_seconds": 300,
                   "voice_profiles": {"enabled": true, "threshold": 0.78}},
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

The index is cached locally and rebuilt only when files change. Its cache is
per note, so changing one file in a large vault re-embeds only that file; the
index skips `.obsidian`, `.git`, `.trash`, plugin/build folders, and reports
when macOS cannot read a protected path. With a remote embedding backend, note
chunks leave the machine; with a local backend or `sentence-transformers`, they
never do.

For large Obsidian vaults the cache also maintains a SQLite FTS5 lexical side
index. Retrieval uses exact-term candidates before semantic scoring, so the
answer path does not need to scan every chunk for each question. YAML
frontmatter is retained as chunk metadata, and `derived/live_events.jsonl` can
be replayed without audio or network access:

```bash
python3 -m hud.replay ~/ZoomRecordings/2026-09-22/14-32-08_enrollment-planning_a1b2c3d4/derived/live_events.jsonl
```

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
loopback only, retries IPv4/IPv6 when the preferred local address is blocked,
requires a random one-time token, never displays stored API keys
(shows `configured ✓` / `not set`), writes the file atomically with a `.bak`
backup, and shuts down when idle — there's no persistent server. Changes apply
to the **next** recording, since the HUD reads config at session start.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mic NAME` | auto | Force a microphone by name (env `MIC_AUDIO_DEVICE`) |
| `--system NAME` | auto | Force the system/loopback input (env `ZOOM_AUDIO_DEVICE`) |
| `--no-system` | off | Record the microphone only |
| `--system-only` | off | Record the other party only (no microphone) |
| `--list` | — | List audio devices (transport, defaults, loopback advice), then exit |
| `--self-test` | off | Play a tone and verify the output→loopback capture path |
| `--check-routing` | off | Verify system audio reaches a loopback, then exit |
| `--fix-routing` | off | Loopback mode only: create/rebuild the Multi-Output Device and select it as default output, then exit |
| `--system-capture MODE` | loopback | `loopback` (BlackHole/Multi-Output, default) or `tap` (Core Audio process tap; opt-in, needs the System Audio Recording permission) |
| `--restore-routing` | off | Undo everything: real default output/input, remove the Multi-Output Device, then exit |
| `--doctor` | off | Check tools, BlackHole, routing, output volume and the microphone (with fixes), then exit |
| `--offline` | off | Privacy: block every non-loopback network call (live STT/answers/KB) and turn notifications off |
| `--no-notifications` | off | Do not post desktop notifications |
| `--fix-output NAME` | stored | With `--fix-routing`/`--restore-routing`: which real output device |
| `--fix-input NAME` | auto | With `--restore-routing`: which microphone to select |
| `--volume PCT` | — | Set the audible output's volume (loopback member, else the default output) |
| `--volume-up` / `--volume-down` | — | Step the volume by `--step` (default 5); used by remapped keys |
| `--mute` / `--unmute` / `--toggle-mute` | — | Mute the audible output without affecting the recording |
| `--loopback-only` | off | With a volume action: only act while the Multi-Output Device is the default output |
| `--chunk-seconds N` | 5 | How often the active mic is tested |
| `--fail-threshold N` | 3 | Consecutive silent checks before cycling inputs |
| `--cycle-seconds N` | 60 | How often every inactive input is tested |
| `--probe-seconds N` | 1.5 | Length of each signal probe |
| `--silence-db N` | -60 | `max_volume` below this counts as silence |
| `--no-transcribe` | off | Skip transcription |
| `--model PATH` | large-v3-turbo-q5_0 | Whisper model to use; base.en remains a speed-first fallback |
| `--live` | off | Open the live transcript + AI answer HUD |
| `--live-no-answers` | off | Live transcript only; never call an answer provider |
| `--no-live-summary` | off | Skip the end-of-call summary/action items/email |
| `--hud-port N` | random | Port for the local HUD |
| `--transcript-dir PATH` | config | Additional folder for a live Markdown transcript mirror |
| `--no-hud-browser` | off | Do not auto-open the HUD in a browser |
| `--stt-backend X` | groq | Live STT backend: `groq` \| `openai` \| `local` |
| `--stt-chunk-seconds N` | 5 (7 with two Groq streams) | Live STT chunk length; the provider guard may raise it for two remote streams |
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
| `--diarize` | off | Run optional post-call WhisperX attribution after a live session |
| `--diarization-backend X` | config | Post-call attribution: `auto` \| `whisperx` \| `off` |
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
| `v1.6-routing-fix` | One-command automated routing fix (`--fix-routing`, menu-bar item), bare-BlackHole misroute advice, click-by-click fallback; `Audio Out` menu-bar dropdown to switch the passthrough output mid-call, stored pairing preference (`~/.zoom_recorder_routing.json`), stale-device rebuilds |
| `v1.7-system-tap` | Core Audio process-tap capture (`--system-capture auto|tap|loopback`): system audio recorded directly where permitted (Terminal context), loopback+volume-slider mode for the menu bar, `--restore-routing`, one-time System Audio Recording permission, wedge recovery, preserved failure diagnostics |
| `v1.8-volume` | First-class top-level **Volume** menu (live level in the title, slider, ±5%, mute, presets), CLI volume/mute actions, Karabiner key mapping for loopback mode, mute leaves the recording intact, default output auto-restored when a recording stops (native keys return between calls) |
| `v1.9-hardening` | Shareable/EDR-friendly install: unsigned `.app` wrapper removed, login autostart opt-in, tap capture opt-in (loopback default), `--doctor`, `--offline` hard network kill-switch, notification toggle, `install.sh`/`uninstall.sh`, `SECURITY.md`/`INSTALL.md` |
| `v2.0-ui` | Non-technical UI pass: Control Center (Setup wizard with one-click fixes, Recordings list, Basics/Advanced settings, Help), menu bar rewritten in plain language with a recording timer, Stop button and Details toggle in the transcript window, recording modes (`both`/`mic`/`system`), recorder settings in the config file, local *and* online transcription setup in the wizard, `QUICKSTART.md` |

Running the tests: `python3 -m unittest discover -s tests`.
