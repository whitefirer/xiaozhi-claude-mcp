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
            "查看 Claude Code 运行状态。"
            "响应字段："
            "  total: 当前会话总数"
            "  waiting: 待审批的权限请求数量（Claude在等用户批准工具调用）"
            "  pending_tasks: 正在后台执行的异步任务数（发送消息后的处理任务）"
            "  prompt: 具体权限请求列表（permission_id、tool、hint）"
            "  pty: PTY会话状态（idle/busy/error）"
            "⚠️ 当 waiting > 0 时，必须逐一列出每个权限请求（tool和hint），逐条问用户「批准还是拒绝？」。"
            "  用户说「批准/同意/允许/可以」→ 立即调 claude.approve"
            "  用户说「拒绝/不行/不允许」→ 立即调 claude.deny"
            "  permission_id 在 permission_requests 数组的每一项中。"
            "⚠️ 当 pending_tasks = 0 且 waiting = 0 时，Claude空闲无任务。"
            "⚠️ 不要为发消息而先调此工具——直接调 claude.send_message！"
            "⚠️ 涉及编程助手状态时，说话简洁精确，不啰嗦。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "claude.send_message",
        "description": (
            "将用户的话发送给电脑上的 Claude Code 处理。异步操作——调用后立即返回任务ID。"
            "Claude 在后台处理，完成后通过 claude.get_result 获取回复。"
            "流程："
            "  1. 调用 claude.send_message(prompt) → 获得 task_id"
            "  2. 告诉用户「正在让Claude处理你的问题，稍等片刻...」"
            "  3. 后续用户问结果时，调 claude.get_result(task_id) 拉取"
            "⚠️ 不要等！不要反复调 claude.status 轮询——下次用户主动问时才查。"
            "⚠️ session_id 填上次返回的，没有就留空。"
            "⚠️ max_turns 控制 Claude 执行轮数，默认2。简单问题1，读写文件5-7。"
            "⚠️ 转述Claude回复时简洁精确，不加废话不润色。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "用户对Claude Code说的完整原始问题，一字不改。",
                },
                "session_id": {
                    "type": "string",
                    "description": "上次 claude.send_message 返回的 session_id，没有就留空。",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Claude执行轮数上限，默认2，最大10。简单问题用1。",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude.get_result",
        "description": (
            "获取 claude.send_message 异步任务的处理结果。"
            "传入 task_id（来自 send_message 的返回值），获取 Claude 的回复。"
            "如果任务还在处理中，返回 status: pending 和 preview（终端实时画面），可据此告诉用户进度。"
            "如果任务完成，返回 status: done 和 Claude 的回复内容。"
            "⚠️ 拿到结果后简洁转述，不添加无关解释。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "claude.send_message 返回的 task_id",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "claude.approve",
        "description": (
            "【批准/同意/允许/可以】批准权限请求。"
            "用户表示批准时调用此工具。"
            "permission_id 取 claude.status 返回的 permission_requests[].permission_id。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "permission_id": {
                    "type": "string",
                    "description": "来自 claude.status 的 permission_requests 数组中的 permission_id",
                },
            },
            "required": ["permission_id"],
        },
    },
    {
        "name": "claude.deny",
        "description": (
            "【拒绝/不行/不允许/不批准/不同意】拒绝权限请求。"
            "用户表示拒绝、不批准或不同意时调用此工具。"
            "permission_id 取 claude.status 返回的 permission_requests[].permission_id。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "permission_id": {
                    "type": "string",
                    "description": "来自 claude.status 的 permission_requests 数组中的 permission_id",
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
