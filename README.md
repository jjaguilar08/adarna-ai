# Adarna

A real-time AI co-pilot for live conversations. Adarna listens to a meeting or interview as it
happens, transcribes it locally on your own machine, and — on a pause or a hotkey — asks Claude
to draft a short suggested response, which shows up in a glanceable, always-on-top overlay.

**Live case study / landing page:** https://adarna-ai.jjaguilar.dev

Named after the Adarna bird of Filipino folklore.

## What it does

- **Listens** to both sides of a call — your own microphone and the other participant's audio
  (via WASAPI loopback) — as two independent, labeled streams.
- **Transcribes** locally with `faster-whisper`. Nothing is sent to a cloud STT service.
- **Suggests** a response by handing the transcript to Claude (via the Claude Code CLI, using
  your own Claude subscription — no per-token API billing) once a pause is detected or a global
  hotkey is pressed.
- **Displays** the suggestion in a small, draggable, resizable, opacity-adjustable overlay window
  that stays on top of whatever call you're on, plus a separate, larger Suggestions window for
  reading a full answer comfortably.
- **Two modes** — Work Meeting (short, casual) and Interview (a longer narrative-plus-bullets
  shape, itself branching by the kind of question asked) — shape how a suggestion reads.
- **Forgets by default.** Audio and transcripts live in memory for the session only. Nothing is
  written to disk unless you explicitly opt in (a "save this session's transcript" setting).

## Why it's built the way it is

WSL cannot see native Windows system audio — there's no loopback path through WSLg's audio
bridge. That one constraint drives the whole architecture: Adarna is split into two cooperating
processes across two operating systems.

```
Windows-native process  (windows_app/)         WSL process  (wsl_app/)
--------------------------------------         -----------------------
WASAPI loopback + mic capture                  Voice-activity segmentation
PySide6 GUI (main window,                      faster-whisper transcription
  Suggestions window, overlay)                 Hallucination-defense filtering
Global hotkey listener            <-- TCP -->  Dual-source transcript merge
                                   socket       Long-lived `claude` CLI session
```

- **`windows_app/`** — native Windows Python (not run inside WSL). PySide6 GUI + WASAPI audio
  capture via `pyaudiowpatch`. Needs to be genuinely native because WSLg can't do system-audio
  loopback.
- **`wsl_app/`** — WSL-side Python. Speech-to-text (`faster-whisper`, gated by `webrtcvad`) and a
  long-lived `claude` CLI subprocess that holds conversation context across a whole session
  instead of paying cold-start cost on every suggestion.
- The two talk over a local TCP socket (`ipc_config.json` sets the port): raw audio one way,
  transcript/suggestion text the other, relying on WSL2's default localhost port-forwarding.

## Requirements

- **Windows 10/11** with WSL2 installed (an Ubuntu distro), since `windows_app` needs a genuine
  native Windows Python, not one running inside WSL.
- **Two Python installs**: one native Windows Python (3.10+) for `windows_app`, one inside WSL
  (3.10.12 is what this was built and tested against) for `wsl_app`.
- **The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code)**, installed and logged
  into an active Claude subscription, reachable as `claude` on WSL's `PATH` — `wsl_app` spawns it
  directly as a subprocess. No `ANTHROPIC_API_KEY` needed or used.
- A working microphone and a WASAPI-capable output device (basically any Windows audio setup) for
  the two audio sources.

## Setup

Clone the repo somewhere inside your WSL filesystem (e.g. `~/projects/adarna-ai`) — both sides
are authored from there, and `windows_app`'s native Windows Python is invoked against those same
files via WSL's built-in interop, no separate Windows-side checkout needed.

```bash
# wsl_app: speech-to-text + orchestration
cd wsl_app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# windows_app: GUI + audio capture -- needs a native Windows Python, invoked
# here via WSL->Windows interop (adjust the python.exe path if it's not on PATH)
cd ../windows_app
python.exe -m venv .venv
.venv/Scripts/pip.exe install -r requirements.txt
```

Then, from the repo root:

```bash
./start_app.sh
```

This starts `wsl_app` in the background, waits for it to be ready, launches `windows_app`'s GUI,
and stops `wsl_app` automatically when the window closes.

On Windows, `start_adarna.bat` is a double-click launcher that just runs `start_app.sh` inside WSL
for you (handy for a Desktop shortcut) — it hardcodes a WSL distro name and repo path, so edit
those two lines first if yours differ.

### Optional: live-agent-listening mode

A second, experimental suggestion path that trades a bit of setup friction for near-zero
suggestion latency, by keeping an actual interactive `claude` session attached to the meeting
instead of spawning a fresh headless call each time. See
[`docs/LIVE_AGENT_LISTENING.md`](docs/LIVE_AGENT_LISTENING.md) for how to enable and run it.

## Project layout

```
windows_app/   Native Windows GUI + audio capture
wsl_app/       WSL-side speech-to-text + Claude orchestration
docs/          Product requirements, day-by-day dev log, live-agent-listening runbook
design_preferences/   Landing page source and design brief
```

`docs/PRD.md` and `docs/DEV_PLAN.md` are the real, running design/build log this project was
developed against — kept as-is (including the false starts and reverts) rather than cleaned up
into a highlight reel, if you want the actual engineering story rather than just the code.

## Privacy

Audio and transcripts are processed in memory for the life of a session and discarded when it
ends. Nothing touches disk unless you explicitly opt in (a per-session "save transcript" setting,
and the separate live-agent-listening export above). The only thing that leaves the machine at
all is the transcript sent to Claude through your own authenticated CLI session — there's no
separate telemetry or analytics.

## License

No license has been chosen yet — this repo is shared as a portfolio/engineering-case-study
reference rather than a project set up for third-party use or contribution.
