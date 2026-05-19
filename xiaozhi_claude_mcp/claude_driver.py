from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResponse:
    content: str
    session_id: str
    tokens: int = 0
    cost_usd: float = 0.0
    raw: dict | None = None


class ClaudeDriver:
    def __init__(self, binary: str = "claude"):
        self.binary = binary

    async def send(
        self,
        prompt: str,
        session_id: str | None = None,
        max_turns: int = 10,
        allowed_tools: list[str] | None = None,
    ) -> ClaudeResponse:
        # First attempt with --resume if session_id provided
        if session_id:
            try:
                return await self._run(prompt, session_id, max_turns, allowed_tools)
            except RuntimeError:
                logger.warning("--resume %s failed, starting new session", session_id)
        return await self._run(prompt, None, max_turns, allowed_tools)

    async def _run(
        self,
        prompt: str,
        session_id: str | None,
        max_turns: int,
        allowed_tools: list[str] | None,
    ) -> ClaudeResponse:
        cmd = self._build_command(prompt, session_id, max_turns, allowed_tools)
        logger.info("Running: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode()[:500]
            raise RuntimeError(f"claude exited {proc.returncode}: {err}")
        return self._parse_output(stdout.decode())

    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        max_turns: int = 10,
        allowed_tools: list[str] | None = None,
    ) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns)]
        if session_id:
            cmd.extend(["--resume", session_id])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        return cmd

    def _parse_output(self, raw: str) -> ClaudeResponse:
        data = json.loads(raw)
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return ClaudeResponse(
            content=data.get("result", ""),
            session_id=data.get("session_id", ""),
            tokens=tokens,
            cost_usd=data.get("total_cost_usd", 0.0),
            raw=data,
        )
