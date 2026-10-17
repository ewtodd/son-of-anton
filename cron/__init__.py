"""
Cron job scheduling system for Son of Anton Agent.

Scheduled tasks (cron expressions, intervals, one-shots) run in their own
fresh sessions. The gateway's in-process ticker (cron/scheduler_provider.py)
ticks the scheduler every 60 seconds; a file lock prevents duplicate execution
if multiple processes overlap.
"""

from cron.jobs import get_job

__all__ = ["get_job"]
