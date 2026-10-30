"""Fake ACP agent for the native-loop tests: no model, no network, one scripted turn per prompt.

    python fake_agent.py STATE_DIR NAME

NAME is the ROLE line of the prompt (manager, worker or reviewer), else the launched name.
STATE_DIR/NAME.json holds {"turns": [{"run": "<bash>", "reply": "<text>"}, ...]}. flow-atelier
starts a fresh agent process for every loop iteration, so the turn index is the counter file
STATE_DIR/NAME.calls. On each prompt the agent saves the prompt text as NAME.prompt.<n>, runs the
turn's `run` script with bash in the session cwd (the worktree), then replies with `reply` where
{head} is the worktree's current HEAD. Past the last scripted turn the last turn repeats.
The session each prompt arrived in is saved as NAME.session.<n>; a session can be resumed.
"""

import asyncio
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import acp
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionResumeCapabilities,
    TextContentBlock,
)

STATE, LAUNCHED = Path(sys.argv[1]), sys.argv[2]


class Agent:
    def __init__(self):
        self.conn = None
        self.cwd = None

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version, **kwargs):
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                session_capabilities=SessionCapabilities(resume=SessionResumeCapabilities()),
                prompt_capabilities=PromptCapabilities(
                    audio=False, embedded_context=False, image=False
                ),
            ),
            agent_info=Implementation(name="fake-" + LAUNCHED, version="0"),
            auth_methods=[],
        )

    async def new_session(self, cwd, **kwargs):
        self.cwd = cwd
        return NewSessionResponse(session_id=uuid.uuid4().hex)

    async def prompt(self, prompt, session_id, **kwargs):
        prompt_text = "".join(getattr(block, "text", "") for block in prompt)
        # One fake per role: the role line every managing-managers prompt starts with picks the script.
        role = re.search(r"^ROLE: (\w+)", prompt_text, re.M)
        NAME = role.group(1) if role else LAUNCHED
        counter = STATE / f"{NAME}.calls"
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        (STATE / f"{NAME}.prompt.{n + 1}").write_text(
            "".join(getattr(block, "text", "") for block in prompt)
        )
        (STATE / f"{NAME}.session.{n + 1}").write_text(session_id)
        turns = json.loads((STATE / f"{NAME}.json").read_text())["turns"]
        turn = turns[min(n, len(turns) - 1)]
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", turn.get("run", "true")], cwd=self.cwd, check=True
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.cwd, capture_output=True, text=True, check=False
        ).stdout.strip()
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=turn["reply"].replace("{head}", head)),
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    async def authenticate(self, *args, **kwargs):
        return None

    async def cancel(self, *args, **kwargs):
        return None

    async def close_session(self, *args, **kwargs):
        return None

    async def ext_method(self, *args, **kwargs):
        return {}

    async def ext_notification(self, *args, **kwargs):
        return None

    async def fork_session(self, *args, **kwargs):
        raise NotImplementedError

    async def list_sessions(self, *args, **kwargs):
        raise NotImplementedError

    async def load_session(self, *args, **kwargs):
        return None

    async def resume_session(self, cwd, session_id, **kwargs):
        self.cwd = cwd
        return ResumeSessionResponse()

    async def set_config_option(self, *args, **kwargs):
        return None

    async def set_session_mode(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    try:
        # session/resume is unstable in ACP; the real adapters serve it.
        asyncio.run(acp.run_agent(Agent(), use_unstable_protocol=True))
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
