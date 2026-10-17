"""Contract: a gateway that has not been asked to exit is not exiting.

``start_gateway`` reads ``runner.should_exit_cleanly`` right after
``runner.start()`` and returns early when it is truthy, before the cron ticker
and housekeeping threads start and while the adapters stop being serviced. The
exit flags must therefore be real booleans: a bound method (the decorator lost
in a block deletion) is always truthy, and a gateway built that way answered
nothing and ran no cron job.
"""

from __future__ import annotations


def test_a_fresh_runner_reports_no_exit() -> None:
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    runner = GatewayRunner(GatewayConfig())

    assert runner.should_exit_cleanly is False
    assert runner.should_exit_with_failure is False
    assert runner.exit_reason is None
    assert runner.exit_code is None
