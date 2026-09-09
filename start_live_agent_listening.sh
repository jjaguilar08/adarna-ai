#!/usr/bin/env bash
# Starts the live-agent-listening watcher for a real, ongoing meeting: an
# ordinary interactive `claude` session that backgrounds a `tail -F` of
# wsl_app's live-export log, attaches Monitor to it, and reacts to real
# SEGMENT/TRIGGER lines hands-off for the rest of the meeting. See
# docs/LIVE_AGENT_LISTENING.md for the full explanation of what this is.
#
# Run this in a SECOND terminal, after checking "Enable live-agent-listening
# export" and pressing Start Session in windows_app:
#   ./start_live_agent_listening.sh
#
# --permission-mode bypassPermissions is required, not optional: this needs
# to run genuinely unattended for the length of the meeting, and a stalled
# approval prompt with nobody there to answer it would silently break the
# whole point (same setup detail Day 18.9's real unattended run needed --
# see docs/DEV_PLAN.md). Only run this against a project you trust for that
# reason.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec claude --permission-mode bypassPermissions "$(cat docs/live_agent_listening_prompt.txt)"
