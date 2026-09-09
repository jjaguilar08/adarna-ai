# Live-Agent-Listening — operational runbook

A real, shippable second suggestion mode, distinct from the app's normal one-shot `claude` CLI
suggestions. Instead of `wsl_app` firing a fresh headless subprocess call per suggestion, you
manually start a *separate*, ordinary interactive `claude` terminal session before a real meeting
and leave it running hands-off for the whole thing. It watches the transcript stream in as cheap
background notifications and only produces one real, fully-reasoned answer when the same trigger
that drives the shipped suggestion feature (auto-suggest's pause timer, or the hotkey) actually
fires — because the conversation is already live in that agent's own context by then, the answer
comes back with near-zero extra latency.

Proven three times against synthetic WAV playback (docs/DEV_PLAN.md, Days 18.8-18.10): narrated in
an IDE, fully unattended in a bare terminal, and over a realistic ~50-minute simulated meeting with
no degradation. This is the real production wiring for that mechanism.

## What this is — and isn't

- **Is**: an opt-in log `wsl_app` writes in real time during a session, purpose-built to be
  `tail -f`'d by a second interactive `claude` session.
- **Isn't**: a button that "just works" inside `windows_app`. It requires a second, manually-started,
  human-attended terminal per meeting — the same operational model Day 18.8 confirmed.
- **Isn't**: routed into `windows_app`'s overlay or transcript pane. The live agent's real answers
  show up only in its own terminal. Routing them back into the GUI was considered and deliberately
  not built — it would add a new hop and work against this mode's whole appeal.

## Prerequisites

- The "Enable live-agent-listening export" checkbox (in `windows_app`'s Session Settings) must be
  checked **before** pressing Start Session — like the rest of that panel, it's read once at session
  start, not live-toggleable.
- A second terminal, run from **WSL** (not Windows) with `claude` available — the log lives in
  `wsl_app`'s own filesystem (`wsl_app/live_agent_logs/`), not `windows_app`'s.

## Step by step

1. Check "Enable live-agent-listening export", then press Start Session as usual.
2. Look at `wsl_app`'s own console output. Once the session starts, it prints a line like:
   ```
   Live-agent-listening log: /home/jon/projects/adarna-ai/wsl_app/live_agent_logs/live_agent_2026-09-09_143210.log
   ```
   Copy that path.
3. Open a second terminal and start an ordinary interactive `claude` session in this project
   (`cd /home/jon/projects/adarna-ai && claude`). If you're not going to be at the keyboard to approve
   tool calls during the meeting, start it with `--permission-mode bypassPermissions` instead — the
   same setup detail Day 18.9's real unattended run needed, not a finding about the mechanism itself.
4. Give it a background task tailing the log, and attach the Monitor tool to it — the exact pattern
   Day 18.9/18.10 already proved works, e.g.: "run `tail -f <the path from step 2>` in the background
   and monitor it."
5. Paste it these operating instructions (there's no `--system-prompt` flag for an interactive
   session the way `wsl_app`'s own `ClaudeCli` sets one, so this has to be typed/pasted by you):

   > You'll receive lines from a live meeting transcript as background notifications. The header line
   > tells you the mode (`meeting` or `interview`) and any context notes — use them the same way
   > `MEETING_SYSTEM_PROMPT`/`INTERVIEW_SYSTEM_PROMPT` in `wsl_app/main.py` do. Each `SEGMENT` line is
   > one thing someone said, labeled `You:` or `Them:` — just note it silently, don't respond to it.
   > Only when a `TRIGGER` line arrives, produce one real answer for what the user (`You`) could say
   > next, in the same plain-text lead-plus-bullets shape `RESPOND_WITH_LEAD_AND_BULLETS_INSTRUCTION`/
   > `INTERVIEW_RESPOND_INSTRUCTION` use for the mode in the header — grounded in everything
   > accumulated in this conversation so far, not just the most recent segment.

6. Run the meeting. Glance at this second terminal whenever you want the live agent's take.
7. When the meeting ends: Stop Session in `windows_app` closes the log file (with a footer line); stop
   the `tail -f` background task yourself in the second terminal whenever you're done with it.

## Important caveat

A `TRIGGER` line only appears when the *shipped* `SuggestionTrigger` mechanism actually fires — the
auto-suggest pause timer (if enabled) or the hotkey/button. Checking the export checkbox alone
creates no independent trigger cadence of its own; if auto-suggest is off and you never press the
hotkey, you'll see `SEGMENT` lines but no `TRIGGER` lines at all.

## Known limitations

- **Trailing segment gap**: the last sentence spoken right before you press Stop Session, if it's
  still transcribing when the session tears down, reaches `windows_app`'s transcript pane but not
  this log — there's no live session left to record it into by then. The same accepted gap the
  shipped suggestion trigger already has for that exact moment.
- **No GUI routing** — by design, see above.
- **Single-operator, local convenience** — this is one person tailing one file in one terminal, not a
  scaled notification channel.
- **Subscription rate-limit exposure** (PRD §10) — carried over from Day 18.8-18.10: a real meeting's
  worth of `SEGMENT`+`TRIGGER` notifications is dozens to hundreds of turns for the watching agent,
  a much heavier usage pattern than the shipped suggestion feature's ~1 call per suggestion.

## Privacy / consent

Same stance as Day 22's session recording (PRD §5/§9, RA 4200): off by default, nothing written
unless you explicitly check the box before starting a session, and the log directory
(`wsl_app/live_agent_logs/`) is gitignored — real conversation content must never be committed.

## Troubleshooting

- **No file appears / no path printed**: the checkbox wasn't checked before Start Session, or the
  session failed to start at all (check for a `session_start_failed` message / error dialog).
- **`SEGMENT` lines but no `TRIGGER` lines**: see "Important caveat" above — auto-suggest is off and
  the hotkey was never pressed.
- **`wsl_app/live_agent_logs/` directory missing**: it's created lazily on the first session that
  actually enables the export — nothing to do, it'll appear.
