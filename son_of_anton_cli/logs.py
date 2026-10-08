"""``son-of-anton logs`` — view and filter Son of Anton log files.

Supports tailing, following, session filtering, level filtering,
component filtering, and relative time ranges.  All log files live
under ``~/.son-of-anton/logs/``.

Usage examples::

    son-of-anton logs                    # last 50 lines of agent.log
    son-of-anton logs -f                 # follow agent.log in real time
    son-of-anton logs errors             # last 50 lines of errors.log
    son-of-anton logs gateway -n 100    # last 100 lines of gateway.log
    son-of-anton logs gui -f            # follow gui.log (dashboard/pty/ws)
    son-of-anton logs desktop -f        # follow desktop.log (Electron app boot/backend)
    son-of-anton logs --level WARNING    # only WARNING+ lines
    son-of-anton logs --session abc123   # filter by session ID substring
    son-of-anton logs --component tools  # only tool-related lines
    son-of-anton logs --since 1h         # lines from the last hour
    son-of-anton logs --since 30m -f     # follow, starting 30 min ago
"""



# Known log files (name → filename)

# Log line timestamp regex — matches "2026-04-05 22:35:00,123" or
# "2026-04-05 22:35:00" at the start of a line.

# Level extraction — matches " INFO ", " WARNING ", " ERROR ", " DEBUG ", " CRITICAL "

# Logger name extraction — after level and optional session tag, the next
# non-space token before ":" is the logger name.
# Matches: "INFO gateway.run:" or "INFO [sess_abc] tools.terminal_tool:"

# Level ordering for >= filtering
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


