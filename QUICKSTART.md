# Quick start (no terminal needed)

Everything happens from the **🎙 icon in the menu bar**.

## Record a call

1. Click **🎙** → **Start recording**.
2. Talk normally. A red dot (🔴) shows you are recording.
3. When the call ends, click **🎙** again → **Stop recording (12:34)**.
4. Your recording and transcript are saved automatically.

That's it. Works with Zoom, Teams, Discord, Slack, or any call in your browser.

## See what you recorded

Click **🎙** → **My recordings…**. Each entry shows the date, how long it was,
and buttons to open the transcript, the summary, play it, or show it in Finder.

## Sound

The menu bar has a **Volume** control at the very top (its title shows the
current level). Use it during calls — macOS's own volume keys can't control the
special audio setup recording needs.

If you switch between speakers and headphones, click **🎙** →
**Play sound through ▸** and pick the one you're using.

## If something doesn't work

Click **🎙** → **Check my audio setup…**. It checks everything and offers
one-click fixes:

- **Microphone silent** → *Open Microphone settings*, then allow access.
- **System audio missing** → *Fix automatically*.
- **Transcription missing** → install/download from the same screen.

## Optional: live transcript

Click **🎙** → **Check my audio setup…** → step 4:

- **On this Mac** — free, private, nothing leaves the computer (downloads a
  one-time `large-v3-turbo-q5_0` model, about 547 MiB). Local mode also shows
  a fast provisional word draft; only stable words become permanent evidence.
- **Online** — best accuracy; needs an API key and sends audio to that
  service while recording.

Then use **🎙** → **Live assistant** → **Start with live transcript** to see
the words as they're said, with **Suggestions** and a box to **Ask about the
call**. The **HUD: Window** menu lets you choose the stable native Window or
the translucent Glass HUD for a full-screen call. While live, **Pause answers**
is available from the same menu, and **Transcript and context** exposes
writeback, Obsidian/knowledge-base, and diarization settings. Native capture
exclusion is best effort, not a guarantee for every screen-sharing path.

## Optional: start at login

**🎙** → **Settings…** → **Start at login**. Off by default.

## Privacy in one line

It records your microphone and the other person's audio (through the standard
BlackHole driver), never your screen, and sends nothing anywhere unless you
turn on Online transcription. See `SECURITY.md` for the full list.
