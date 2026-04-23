"""Slack channel introspection and management tool.

Provides the agent with the ability to interact with Slack workspaces
when running on the Slack gateway. Uses Slack Web API directly with
the bot token — no dependency on the gateway adapter's client.

Only included in the hermes-slack toolset, so it has zero cost for
users on other platforms.

Follows the same pattern as discord_tool.py: action-based dispatch,
config allowlist, schema generation from manifest.

Spec: specs/20260423-204500-001-slack-channel-tools.mdx
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import registry

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


class SlackAPIError(Exception):
    def __init__(self, method: str, error: str, detail: str = ""):
        self.method = method
        self.error = error
        self.detail = detail
        super().__init__(f"Slack API {method}: {error}" + (f" — {detail}" if detail else ""))


def _get_bot_token() -> Optional[str]:
    return os.getenv("SLACK_BOT_TOKEN", "").strip() or None


def _slack_request(method: str, params: Optional[dict] = None, post_json: Optional[dict] = None) -> dict:
    token = _get_bot_token()
    if not token:
        raise SlackAPIError(method, "SLACK_BOT_TOKEN not configured")

    url = f"{SLACK_API_BASE}/{method}"
    headers = {"Authorization": f"Bearer {token}"}

    if post_json is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(post_json).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    elif params:
        url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise SlackAPIError(method, f"HTTP {e.code}", body) from e
    except Exception as e:
        raise SlackAPIError(method, str(e)) from e

    if not result.get("ok"):
        raise SlackAPIError(method, result.get("error", "unknown"), result.get("detail", ""))

    return result


def _fmt_message(msg: dict) -> dict:
    return {
        "ts": msg.get("ts", ""),
        "user": msg.get("user", msg.get("bot_id", "")),
        "text": msg.get("text", "")[:500],
        "type": msg.get("subtype", "message"),
    }


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _react(*, token, channel, timestamp, emoji, remove, **kw) -> str:
    method = "reactions.remove" if remove else "reactions.add"
    _slack_request(method, post_json={"channel": channel, "timestamp": timestamp, "name": emoji})
    verb = "Removed" if remove else "Added"
    return json.dumps({"ok": True, "message": f"{verb} :{emoji}: reaction"})


def _edit_message(*, token, channel, timestamp, text, **kw) -> str:
    _slack_request("chat.update", post_json={"channel": channel, "ts": timestamp, "text": text})
    return json.dumps({"ok": True, "message": "Message updated"})


def _delete_message(*, token, channel, timestamp, **kw) -> str:
    _slack_request("chat.delete", post_json={"channel": channel, "ts": timestamp})
    return json.dumps({"ok": True, "message": "Message deleted"})


def _fetch_messages(*, token, channel, limit, latest, oldest, **kw) -> str:
    params = {"channel": channel, "limit": min(limit, 100)}
    if latest:
        params["latest"] = latest
    if oldest:
        params["oldest"] = oldest
    result = _slack_request("conversations.history", params=params)
    messages = [_fmt_message(m) for m in result.get("messages", [])]
    return json.dumps({"messages": messages, "count": len(messages)})


def _thread_replies(*, token, channel, timestamp, limit, **kw) -> str:
    params = {"channel": channel, "ts": timestamp, "limit": min(limit, 100)}
    result = _slack_request("conversations.replies", params=params)
    messages = [_fmt_message(m) for m in result.get("messages", [])]
    return json.dumps({"messages": messages, "count": len(messages)})


def _set_topic(*, token, channel, text, **kw) -> str:
    _slack_request("conversations.setTopic", post_json={"channel": channel, "topic": text})
    return json.dumps({"ok": True, "message": f"Topic set to: {text}"})


def _set_purpose(*, token, channel, text, **kw) -> str:
    _slack_request("conversations.setPurpose", post_json={"channel": channel, "purpose": text})
    return json.dumps({"ok": True, "message": f"Purpose set to: {text}"})


def _list_pins(*, token, channel, **kw) -> str:
    result = _slack_request("pins.list", params={"channel": channel})
    pins = []
    for item in result.get("items", []):
        msg = item.get("message", {})
        pins.append({"ts": msg.get("ts", ""), "text": msg.get("text", "")[:200], "user": msg.get("user", "")})
    return json.dumps({"pins": pins, "count": len(pins)})


def _pin_message(*, token, channel, timestamp, **kw) -> str:
    _slack_request("pins.add", post_json={"channel": channel, "timestamp": timestamp})
    return json.dumps({"ok": True, "message": "Message pinned"})


def _unpin_message(*, token, channel, timestamp, **kw) -> str:
    _slack_request("pins.remove", post_json={"channel": channel, "timestamp": timestamp})
    return json.dumps({"ok": True, "message": "Message unpinned"})


def _list_channels(*, token, limit, **kw) -> str:
    params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": min(limit, 200)}
    result = _slack_request("conversations.list", params=params)
    channels = [
        {"id": c["id"], "name": c.get("name", ""), "topic": c.get("topic", {}).get("value", ""),
         "is_member": c.get("is_member", False)}
        for c in result.get("channels", [])
    ]
    return json.dumps({"channels": channels, "count": len(channels)})


def _channel_info(*, token, channel, **kw) -> str:
    result = _slack_request("conversations.info", params={"channel": channel})
    c = result.get("channel", {})
    return json.dumps({
        "id": c.get("id", ""),
        "name": c.get("name", ""),
        "topic": c.get("topic", {}).get("value", ""),
        "purpose": c.get("purpose", {}).get("value", ""),
        "num_members": c.get("num_members", 0),
        "is_private": c.get("is_private", False),
        "created": c.get("created", 0),
    })


def _user_info(*, token, user, **kw) -> str:
    result = _slack_request("users.info", params={"user": user})
    u = result.get("user", {})
    profile = u.get("profile", {})
    return json.dumps({
        "id": u.get("id", ""),
        "name": u.get("name", ""),
        "real_name": profile.get("real_name", ""),
        "display_name": profile.get("display_name", ""),
        "status_text": profile.get("status_text", ""),
        "status_emoji": profile.get("status_emoji", ""),
        "tz": u.get("tz", ""),
        "is_bot": u.get("is_bot", False),
    })


def _list_users(*, token, limit, **kw) -> str:
    result = _slack_request("users.list", params={"limit": min(limit, 200)})
    users = [
        {"id": m["id"], "name": m.get("name", ""), "real_name": m.get("real_name", ""),
         "is_bot": m.get("is_bot", False), "deleted": m.get("deleted", False)}
        for m in result.get("members", [])
        if not m.get("deleted", False)
    ]
    return json.dumps({"users": users, "count": len(users)})


def _upload_file(*, token, channel, text, filename, title, **kw) -> str:
    _slack_request("files.uploadV2", post_json={
        "channel_id": channel,
        "content": text,
        "filename": filename or "file.txt",
        "title": title or filename or "file.txt",
    })
    return json.dumps({"ok": True, "message": f"File '{filename or 'file.txt'}' uploaded"})


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

_ACTIONS = {
    "react": _react,
    "edit_message": _edit_message,
    "delete_message": _delete_message,
    "fetch_messages": _fetch_messages,
    "thread_replies": _thread_replies,
    "set_topic": _set_topic,
    "set_purpose": _set_purpose,
    "list_pins": _list_pins,
    "pin_message": _pin_message,
    "unpin_message": _unpin_message,
    "list_channels": _list_channels,
    "channel_info": _channel_info,
    "user_info": _user_info,
    "list_users": _list_users,
    "upload_file": _upload_file,
}

_ACTION_MANIFEST: List[Tuple[str, str, str]] = [
    ("react", "(channel, timestamp, emoji)", "add or remove an emoji reaction (set remove=true to remove)"),
    ("edit_message", "(channel, timestamp, text)", "edit a message the bot previously sent"),
    ("delete_message", "(channel, timestamp)", "delete a message the bot previously sent"),
    ("fetch_messages", "(channel)", "fetch recent messages; optional latest/oldest timestamps"),
    ("thread_replies", "(channel, timestamp)", "fetch replies in a thread"),
    ("set_topic", "(channel, text)", "set the channel topic"),
    ("set_purpose", "(channel, text)", "set the channel purpose/description"),
    ("list_pins", "(channel)", "list pinned messages in a channel"),
    ("pin_message", "(channel, timestamp)", "pin a message"),
    ("unpin_message", "(channel, timestamp)", "unpin a message"),
    ("list_channels", "()", "list channels the bot is a member of"),
    ("channel_info", "(channel)", "get channel details (topic, purpose, member count)"),
    ("user_info", "(user)", "get user details (name, status, timezone)"),
    ("list_users", "()", "list workspace members"),
    ("upload_file", "(channel, text, filename)", "upload text content as a file to a channel"),
]

_REQUIRED_PARAMS: Dict[str, List[str]] = {
    "react": ["channel", "timestamp", "emoji"],
    "edit_message": ["channel", "timestamp", "text"],
    "delete_message": ["channel", "timestamp"],
    "fetch_messages": ["channel"],
    "thread_replies": ["channel", "timestamp"],
    "set_topic": ["channel", "text"],
    "set_purpose": ["channel", "text"],
    "list_pins": ["channel"],
    "pin_message": ["channel", "timestamp"],
    "unpin_message": ["channel", "timestamp"],
    "channel_info": ["channel"],
    "user_info": ["user"],
    "upload_file": ["channel", "text"],
}


# ---------------------------------------------------------------------------
# Config allowlist
# ---------------------------------------------------------------------------

def _load_allowed_actions_config() -> Optional[List[str]]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        return None

    raw = (cfg.get("slack") or {}).get("channel_actions")
    if raw is None or raw == "":
        return None

    if isinstance(raw, str):
        names = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        return None

    valid = [n for n in names if n in _ACTIONS]
    invalid = [n for n in names if n not in _ACTIONS]
    if invalid:
        logger.warning("slack.channel_actions: unknown action(s) ignored: %s", ", ".join(invalid))
    return valid


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------

def _build_schema(actions: List[str]) -> Dict[str, Any]:
    if not actions:
        actions = list(_ACTIONS.keys())

    manifest_lines = [
        f"  {name}{sig}  — {desc}"
        for name, sig, desc in _ACTION_MANIFEST
        if name in actions
    ]

    description = (
        "Interact with the Slack workspace via the Web API.\n\n"
        "Available actions:\n"
        + "\n".join(manifest_lines) + "\n\n"
        "Use list_channels to discover channel IDs. "
        "The bot can only edit/delete its own messages. "
        "Emoji names should be without colons (e.g. 'thumbsup' not ':thumbsup:')."
    )

    return {
        "type": "function",
        "function": {
            "name": "slack_channel",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": actions,
                    },
                    "channel": {
                        "type": "string",
                        "description": "Slack channel ID (e.g. C08235V51T3).",
                    },
                    "timestamp": {
                        "type": "string",
                        "description": "Message timestamp (e.g. 1234567890.123456).",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text content (message text, topic, purpose, or file content).",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. 'thumbsup').",
                    },
                    "remove": {
                        "type": "boolean",
                        "description": "For react: set true to remove instead of add. Default false.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Slack user ID.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename for upload_file.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for upload_file.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Max items to return (default 50).",
                    },
                    "latest": {
                        "type": "string",
                        "description": "End of time range (fetch_messages).",
                    },
                    "oldest": {
                        "type": "string",
                        "description": "Start of time range (fetch_messages).",
                    },
                },
                "required": ["action"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def slack_channel(
    action: str,
    channel: str = "",
    timestamp: str = "",
    text: str = "",
    emoji: str = "",
    remove: bool = False,
    user: str = "",
    filename: str = "",
    title: str = "",
    limit: int = 50,
    latest: str = "",
    oldest: str = "",
    task_id: str = None,
) -> str:
    token = _get_bot_token()
    if not token:
        return json.dumps({"error": "SLACK_BOT_TOKEN not configured."})

    action_fn = _ACTIONS.get(action)
    if not action_fn:
        return json.dumps({"error": f"Unknown action: {action}", "available_actions": list(_ACTIONS.keys())})

    allowlist = _load_allowed_actions_config()
    if allowlist is not None and action not in allowlist:
        return json.dumps({"error": f"Action '{action}' disabled by config (slack.channel_actions)."})

    local_vars = {"channel": channel, "timestamp": timestamp, "text": text, "emoji": emoji, "user": user}
    missing = [p for p in _REQUIRED_PARAMS.get(action, []) if not local_vars.get(p)]
    if missing:
        return json.dumps({"error": f"Missing required parameters for '{action}': {', '.join(missing)}"})

    try:
        return action_fn(
            token=token, channel=channel, timestamp=timestamp, text=text,
            emoji=emoji, remove=remove, user=user, filename=filename,
            title=title, limit=limit, latest=latest, oldest=oldest,
        )
    except SlackAPIError as e:
        logger.warning("Slack API error in action '%s': %s", action, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected error in slack_channel action '%s'", action)
        return json.dumps({"error": f"Unexpected error: {e}"})


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

def check_slack_tool_requirements() -> bool:
    return bool(_get_bot_token())


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

_STATIC_SCHEMA = _build_schema(list(_ACTIONS.keys()))

registry.register(
    name="slack_channel",
    toolset="slack",
    schema=_STATIC_SCHEMA,
    handler=lambda args, **kw: slack_channel(
        action=args.get("action", ""),
        channel=args.get("channel", ""),
        timestamp=args.get("timestamp", ""),
        text=args.get("text", ""),
        emoji=args.get("emoji", ""),
        remove=args.get("remove", False),
        user=args.get("user", ""),
        filename=args.get("filename", ""),
        title=args.get("title", ""),
        limit=args.get("limit", 50),
        latest=args.get("latest", ""),
        oldest=args.get("oldest", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_slack_tool_requirements,
    requires_env=["SLACK_BOT_TOKEN"],
)
