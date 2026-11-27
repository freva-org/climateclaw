import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from climateclaw.services.streaming.stream_variants import SVServerHint

REPLAYING_CODE_STATUS = (
    "Executing previous code blocks... "
    "Please wait a moment and do not refresh the page."
)
REPLAY_DONE_STATUS = "Execution of previous code blocks is done."


@dataclass
class ReplayProgress:
    """Tracks which code block of the replay is currently running."""

    total: int
    current: int = 1
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def advance(self, step: int) -> None:
        self.current = step
        self.changed.set()


@dataclass
class ReplayGate:
    task: asyncio.Task | None
    progress: ReplayProgress | None = None
    start_sent: bool = False
    done_sent: bool = False
    last_step_sent: int | None = None

    def _busy_hint(self) -> SVServerHint:
        content: dict = {"busy": True, "detail": REPLAYING_CODE_STATUS}
        if self.progress is not None:
            current, total = self.progress.current, self.progress.total
            self.last_step_sent = current
            # Structured progress so the frontend can render a progress bar
            content["progress"] = {"current": current, "total": total}
        return SVServerHint(content=content)

    def _done_hint(self) -> SVServerHint:
        self.done_sent = True
        return SVServerHint(content={"busy": False, "detail": REPLAY_DONE_STATUS})

    def start_hint(self) -> SVServerHint | None:
        if self.start_sent or self.task is None or self.task.done():
            return None

        self.start_sent = True
        return self._busy_hint()

    def progress_hint_if_changed(self) -> SVServerHint | None:
        if (
            not self.start_sent
            or self.done_sent
            or self.progress is None
            or self.task is None
            or self.task.done()
            or self.progress.current == self.last_step_sent
        ):
            return None

        return self._busy_hint()

    def pending_hint_if_ready(self) -> SVServerHint | None:
        """Done hint if the replay finished, otherwise a progress hint if it advanced."""
        if self.done_sent or self.task is None:
            return None

        if self.task.done():
            return self._done_hint()

        return self.progress_hint_if_changed()

    async def wait_done_hints(self) -> AsyncIterator[SVServerHint]:
        """Wait for the replay to finish, yielding progress hints along the way."""
        if self.done_sent or self.task is None:
            return

        while not self.task.done():
            if self.progress is None:
                break

            waiter = asyncio.create_task(self.progress.changed.wait())
            try:
                await asyncio.wait(
                    {self.task, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                waiter.cancel()

            self.progress.changed.clear()
            if hint := self.progress_hint_if_changed():
                yield hint

        await self.task
        yield self._done_hint()
