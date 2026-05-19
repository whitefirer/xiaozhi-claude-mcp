from __future__ import annotations

import logging
from typing import Callable, Awaitable
from dataclasses import dataclass

from xiaozhi_claude_mcp.protocol import JsonRpcRequest

logger = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "name": "claude.status",
        "description": "Get Claude Code session state: session counts, token stats, recent output, and pending permission requests. Modeled after claude-desktop-buddy heartbeat snapshots.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "claude.send_message",
        "description": "Send a prompt to Claude Code and get the response. Use --resume to continue a previous session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt text to send to Claude Code",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional session ID to resume a previous conversation",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude.approve",
        "description": "Approve a pending permission request. The Claude Code tool call will be allowed to proceed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "permission_id": {
                    "type": "string",
                    "description": "The permission request ID from claude.notify_permission",
                },
            },
            "required": ["permission_id"],
        },
    },
    {
        "name": "claude.deny",
        "description": "Deny a pending permission request. The Claude Code tool call will be blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "permission_id": {
                    "type": "string",
                    "description": "The permission request ID from claude.notify_permission",
                },
            },
            "required": ["permission_id"],
        },
    },
    {
        "name": "claude.notify_permission",
        "description": "[Notification] Push a permission request to xiaozhi when Claude Code needs approval for a tool call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "permission_id": {
                    "type": "string",
                    "description": "Unique permission request ID",
                },
                "tool": {
                    "type": "string",
                    "description": "The tool being called (e.g., Bash, Write)",
                },
                "hint": {
                    "type": "string",
                    "description": "Human-readable summary of the tool call",
                },
            },
        },
    },
    {
        "name": "claude.notify_turn",
        "description": "[Notification] Push a completed Claude Code turn to xiaozhi with the assistant's response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Always 'assistant'",
                },
                "content": {
                    "type": "string",
                    "description": "The assistant's response text",
                },
                "session_id": {
                    "type": "string",
                    "description": "The session ID this turn belongs to",
                },
                "tokens": {
                    "type": "integer",
                    "description": "Token count for this turn",
                },
            },
        },
    },
]


def get_tools_list() -> list[dict]:
    return TOOL_SCHEMAS


@dataclass
class ToolHandler:
    name: str
    handler: Callable[[dict], Awaitable[dict]]


def make_text_content(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]
