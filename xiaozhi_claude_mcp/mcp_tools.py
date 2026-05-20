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
            "查看 Claude Code 运行状态（会话数、待审批操作数等）。"
            "响应中 waiting 字段表示待审批操作数量，prompt 字段包含具体的权限请求（permission_id、tool、hint）。"
            "如有待审批操作，询问用户后，用 claude.approve 或 claude.deny 处理。"
            "⚠️ 仅用于回答「AI在干嘛」「电脑上有什么在跑」「有待审批吗」这类状态问题。"
            "⚠️ 不要为了发送消息而先调此工具——直接调 claude.send_message！"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "claude.send_message",
        "description": (
            "将用户的话发送给电脑上的 Claude Code，让 AI 编程助手来回答或执行任务。"
            "自动启动 Claude Code，无需事先检查状态，无需获取 session_id。"
            "⚠️ 这是让 Claude Code 响应用户的唯一方法。"
            "以下情况必须调用此工具（不要先调 claude.status）："
            "用户想对电脑上的AI说话、让AI帮忙、问编程问题、分析代码、写代码、修bug、"
            "操作文件、执行命令、解释项目、查看代码——总之只要用户想让AI做事，就调此工具。"
            "参数 prompt：用户原始问题的完整文本，一字不改。"
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
                    "description": "⚠️ 会话ID，不是项目名！只能用上一次 claude.send_message 返回结果里的 session_id 值（类似 961208df-23ba-46db-9014-6214497c5b1e 这种格式）。没有就留空，不要填任何其他值。",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "可选，默认2。简单问题用1就够了，需要读写文件或执行命令的任务用5-7。",
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
                    "description": "The permission request ID from the prompt field in claude.status",
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
                    "description": "The permission request ID from the prompt field in claude.status",
                },
            },
            "required": ["permission_id"],
        },
    },
    {
        "name": "claude.notify_permission",
        "description": (
            "[服务端推送通知，不要主动调用] "
            "当 Claude Code 需要你审批工具调用时，你会收到此通知。"
            "通知中包含 permission_id、tool、hint 三个字段。"
            "收到后用 claude.approve 或 claude.deny 回应。"
        ),
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
        "description": (
            "[服务端推送通知，不要主动调用] "
            "Claude Code 完成一轮操作后，你会收到此通知，包含 AI 的回复内容和 session_id。"
        ),
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
