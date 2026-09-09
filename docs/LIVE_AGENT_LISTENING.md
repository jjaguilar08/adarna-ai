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

- **Is**: an opt-in log `wsl_app` writes in real time during a session, at a single fixed path
  (`wsl_app/live_agent_logs/live_agent_current.log`, overwritten fresh each session — not a unique
  file per session, deliberately, so a launcher script can point at it without you copying a path
  out of a console every time), purpose-built to be `tail -F`'d by a second interactive `claude`
  session.
- **Isn't**: a button that "just works" inside `windows_app`. It requires a second, manually-started,
  human-attended terminal per meeting — the same operational model Day 18.8 confirmed. What used to
  be several manual sub-steps (start `claude`, tell it to tail and monitor, paste it instructions)
  is now one command — see below — but that second terminal itself still isn't optional.
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
2. In a second WSL terminal, from this repo's root:
   ```
   ./start_live_agent_listening.sh
   ```
   That's it — no path to copy, nothing to type into the session afterward. The script starts an
   interactive `claude` session (`--permission-mode bypassPermissions`, needed since nobody's there
   to answer approval prompts during a real meeting — see the script's own comment) with
   `docs/live_agent_listening_prompt.txt` as its opening prompt. That prompt tells it, as its very
   first action, to background a `tail -F` of the fixed log path and attach Monitor, then just watch
   — see that file for the exact operating instructions (how it should react to `SEGMENT`/`TRIGGER`
   lines, the answer shape per mode) if you ever want to tweak them.
3. Run the meeting. Glance at this second terminal whenever you want the live agent's take.
4. When the meeting ends: Stop Session in `windows_app` closes the log file (with a footer line);
   stop the `claude` session in the second terminal (Ctrl+C, or however you'd normally end one)
   whenever you're done with it.

If you'd rather drive it manually instead of the script (e.g. to watch it start up, or to tweak
something for one run without editing the prompt file): open a second terminal, `cd` into this repo,
start `claude --permission-mode bypassPermissions`, then paste in the contents of
`docs/live_agent_listening_prompt.txt` yourself.

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

- **`start_live_agent_listening.sh`'s `tail -F` seems stuck waiting**: the checkbox wasn't checked
  before Start Session, or the session failed to start at all (check `wsl_app`'s own console for a
  "Live-agent-listening log: ..." line, or `windows_app` for a `session_start_failed` error dialog)
  — `tail -F` will just keep retrying until the file actually appears, rather than erroring.
- **`SEGMENT` lines but no `TRIGGER` lines**: see "Important caveat" above — auto-suggest is off and
  the hotkey was never pressed.
- **The log still has last session's content in it when you expected a fresh one**: `start()` opens
  and truncates `live_agent_current.log` fresh at the top of every session it's enabled for — if
  you're seeing stale content, the checkbox likely wasn't actually checked for the session you just
  ran (see the first bullet above).
- **`wsl_app/live_agent_logs/` directory missing**: it's created lazily on the first session that
  actually enables the export — nothing to do, it'll appear.
