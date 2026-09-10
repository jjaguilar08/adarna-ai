# Landing page design prompt — Adarna

Copy everything below the line into Claude Design. Fill in the `[ ... ]` blanks with your
own visual preferences first — those are the parts only you can decide.

---

Design a single-page portfolio landing page for **Adarna**, a personal project I built and
shipped, to showcase it on my software engineering portfolio site.

## What Adarna is

Adarna is a real-time AI co-pilot for live conversations. It runs during a video call or
interview, listens to the audio, transcribes it locally and instantly, and — on a pause or a
hotkey press — asks Claude to generate a short suggested response, which shows up in a
glanceable always-on-top overlay. Named after the Adarna bird from Filipino folklore (the app
icon is a stylized bird mark).

Frame it as a **live meeting assistant / interview-practice copilot** — the honest, general
version of the use case. Don't write copy that centers on secretly cheating in a real,
undisclosed job interview; that's a real use case for the builder personally, but it's not the
angle for a public case study.

This is a solo-built, personal-use tool (not a shipped commercial product), so the page should
read as an engineering case study / portfolio piece — "here's a real system I designed and
built end-to-end" — not as SaaS marketing copy. Confident and technical, not salesy.

## Why it's worth showcasing (the engineering substance)

- **Real-time, fully local pipeline**: WASAPI system-audio + microphone capture → voice-activity
  segmentation → local speech-to-text (`faster-whisper`) → Claude-generated suggestion, appearing
  within roughly 5–8 seconds of a detected pause or hotkey press. No cloud STT, no per-token API
  billing — everything runs on the user's own machine against an existing Claude subscription.
- **Cross-OS, two-process architecture**: a native Windows process (PySide6/Qt GUI + WASAPI audio
  capture — required because WSL cannot see native Windows system audio) talks to a WSL process
  (speech-to-text + a long-lived Claude CLI session) over a local TCP socket. Designed this way
  after real feasibility testing ruled out the simpler single-process approach.
- **Dual-source, labeled transcript**: microphone and system audio are captured, transcribed, and
  ordered independently, then merged in true spoken order and labeled "You" / "Them" — so
  suggestions are grounded in who said what.
- **Mode-aware suggestions**: a "Work Meeting" mode and an "Interview" mode produce differently
  shaped responses (casual and brief vs. a longer narrative-plus-bullets structure), branching
  further by question type within interview mode.
- **A glanceable, non-intrusive overlay**: always-on-top, draggable, resizable, adjustable opacity,
  optional click-through — built to sit on top of a real video call without getting in the way.
  Suggestions stream in token-by-token rather than appearing all at once.
- **Real engineering rigor, not just a demo**: hallucination-defense filtering on the speech-to-text
  output (catching both "confidently silent" and "not-enough-signal, model free-associates" failure
  modes), careful cross-thread synchronization between the audio thread and the transcription
  worker, and a privacy-first default — nothing is written to disk unless the user explicitly opts
  in to session recording.
- Built solo, iterated through real physical testing on real hardware (not just code review) —
  several rounds of "looked right in code, broke in the real world, root-caused and fixed."

## Tech stack (for a "built with" strip)

Python, PySide6 (Qt), `faster-whisper`, WebRTC VAD, Claude Code CLI, WASAPI loopback audio
capture, WSL2, local TCP socket IPC.

## Suggested sections

1. **Hero** — Adarna name/bird mark + a short, confident one-line pitch and a sub-line. One
   strong visual: the overlay UI (a mockup is fine — no real screenshot exists yet, so invent a
   plausible, tasteful one showing a transcript line and a suggestion card).
2. **How it works** — a simple 4-step visual flow: *Listen → Transcribe → Suggest → Glance*, in
   plain, non-jargon language a non-technical visitor can follow, with a technical caption under
   each step for the engineer-visitor.
3. **Feature highlights** — a grid of 4–6 cards from the "why it's worth showcasing" list above,
   each with a short plain-English headline and one supporting sentence.
4. **Under the hood** — a slightly more technical section (diagram-friendly) covering the
   cross-OS two-process architecture and the dual-source transcript, for visitors who want the
   real engineering story, not just the feature list.
5. **Built with** — the tech stack strip.
6. **Footer / closing** — link placeholders for GitHub and contact.

## Visual/design preferences

[ Add your own preferences here before sending — e.g. light or dark, color palette, font
pairing, any reference sites/screenshots you like, how playful vs. serious the tone should
feel. ]
