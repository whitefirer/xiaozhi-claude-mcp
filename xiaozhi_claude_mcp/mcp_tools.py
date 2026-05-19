from __future__ import annotations

import logging
from typing import Callable, Awaitable
from dataclasses import dataclass

from xiaozhi_claude_mcp.protocol import JsonRpcRequest

logger = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "name": "claude.status",
        "description": (
            "查看电脑上正在运行的 Claude Code（AI编程助手）的状态。"
            "当用户问「Claude Code在干嘛」「AI助手在做什么」「编程助手状态」「电脑上的AI在跑什么」"
            "「claude状态」「看下代码助手」时必须调用此工具。"
            "返回当前会话数、工作目录、是否有待审批的操作等信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "claude.send_message",
        "description": (
            "把用户说的话发送给电脑上的 Claude Code（AI编程助手），让Claude Code来回答或执行编程任务。"
            "当用户明确要对「Claude Code」「AI编程助手」「代码助手」「claude」说话、提问、下指令时必须调用此工具。"
            "用户问编程问题、让AI写代码、分析代码、修bug、操作文件时，都应该调用此工具而不是让小智自己回答。"
            "参数prompt是用户原始问题的完整文本，不要改写或总结。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "用户对Claude Code说的完整原始问题，一字不改地传递。例如「帮我分析src/main.py的性能问题」「看看这个项目有哪些bug」",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，上次对话返回的session_id，用于继续之前的对话",
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
