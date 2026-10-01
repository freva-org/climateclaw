import asyncio
from dataclasses import dataclass

from climateclaw.services.streaming.stream_variants import SVServerHint

REPLAYING_CODE_STATUS = "Executing previous code blocks... Please wait a moment."
REPLAY_DONE_STATUS = "Execution of previous code blocks is done."


@dataclass
class ReplayGate:
    task: asyncio.Task | None
    start_sent: bool = False
    done_sent: bool = False

    def start_hint(self) -> SVServerHint | None:
        if self.start_sent or self.task is None or self.task.done():
            return None

        self.start_sent = True
        return SVServerHint(content={"busy": True, "detail": REPLAYING_CODE_STATUS})

    def done_hint_if_ready(self) -> SVServerHint | None:
        if self.done_sent or self.task is None or not self.task.done():
            return None

        self.done_sent = True
        return SVServerHint(content={"busy": False, "detail": REPLAY_DONE_STATUS})

    async def wait_done_hint(self) -> SVServerHint | None:
        if self.done_sent or self.task is None:
            return None

        if not self.task.done():
            await self.task

        self.done_sent = True
        return SVServerHint(content={"busy": False, "detail": REPLAY_DONE_STATUS})
