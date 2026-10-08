"""``son-of-anton slack ...`` CLI subcommands.

Today only ``son-of-anton slack manifest`` is implemented — it generates the
Slack app manifest JSON for registering every gateway command as a native
Slack slash (``/btw``, ``/stop``, ``/model``, …) so users get the same
first-class slash UX Discord already has.

Typical workflow::

    $ son-of-anton slack manifest > slack-manifest.json
    # or:
    $ son-of-anton slack manifest --write

Then paste the printed JSON into the Slack app config (Features → App
Manifest → Edit) and click Save. Slack diffs the manifest and prompts
for reinstall when scopes/commands change.
"""
from __future__ import annotations



def _build_full_manifest(
    bot_name: str,
    bot_description: str,
    include_assistant: bool = True,
    messaging_experience: str | None = None,
    long_description: str | None = None,
) -> dict:
    """Build a full Slack manifest merging display info + our slash list.

    The slash-command list is always generated from ``COMMAND_REGISTRY`` so
    it stays in sync with the rest of Son of Anton. Other manifest sections
    (display info, OAuth scopes, socket mode) are set to sensible defaults
    for a Son of Anton deployment — users can tweak them in the Slack UI after
    pasting.

    By default, this keeps Son of Anton on Slack's older Assistant messaging
    experience (``assistant_view``) for backward compatibility. Pass
    ``messaging_experience="agent"`` (``--agent-view``) to emit Slack's Agent
    messaging experience (``agent_view`` + ``app_home_opened``). Pass
    ``include_assistant=False`` or ``messaging_experience="none"``
    (``--no-assistant``) to omit Slack AI messaging features and get a flat DM
    surface where ``/help``, ``/new``, etc. work inline.
    """
    from son_of_anton_cli.commands import slack_app_manifest

    if messaging_experience is None:
        messaging_experience = "assistant" if include_assistant else "none"
    messaging_experience = str(messaging_experience).strip().lower()
    if messaging_experience not in {"assistant", "agent", "none"}:
        raise ValueError(
            "messaging_experience must be one of: assistant, agent, none"
        )

    partial = slack_app_manifest()
    slashes = partial["features"]["slash_commands"]

    features = {
        "app_home": {
            "home_tab_enabled": False,
            "messages_tab_enabled": True,
            "messages_tab_read_only_enabled": False,
        },
        "bot_user": {
            "display_name": bot_name[:80],
            "always_online": True,
        },
        "slash_commands": slashes,
    }

    bot_scopes = [
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "commands",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "reactions:read",
        "users:read",
    ]

    bot_events = [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim",
        "reaction_added",
        "reaction_removed",
    ]

    if messaging_experience == "assistant":
        features["assistant_view"] = {
            "assistant_description": "Chat with Son of Anton in threads and DMs.",
        }
        bot_scopes.append("assistant:write")
        bot_events.extend(
            [
                "assistant_thread_context_changed",
                "assistant_thread_started",
            ]
        )
    elif messaging_experience == "agent":
        features["agent_view"] = {
            "agent_description": "Chat with Son of Anton in Slack Messages.",
        }
        bot_scopes.append("assistant:write")
        # Slack includes current viewing context in Agent DM events only after
        # this subscription is enabled; the adapter consumes that context to
        # preserve the referred channel across the agent turn.
        bot_events.extend(["app_context_changed", "app_home_opened"])

    bot_scopes.sort()
    bot_events.sort()

    display_information = {
        "name": bot_name[:35],
        "description": (bot_description or "Your Son of Anton agent on Slack")[:140],
        "background_color": "#1a1a2e",
    }
    if long_description is not None:
        display_information["long_description"] = long_description

    return {
        "_metadata": {
            "major_version": 1,
            "minor_version": 1,
        },
        "display_information": display_information,
        "features": features,
        "oauth_config": {
            "scopes": {
                "bot": bot_scopes,
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": bot_events,
            },
            "interactivity": {
                "is_enabled": True,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


