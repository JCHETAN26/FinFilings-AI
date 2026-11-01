"""Bounded asynchronous query grouping; model slots remain owned by the caller's executor."""
import asyncio
import time
from typing import Any, Awaitable, Callable


class DenseBatcher:
    def __init__(self, execute: Callable[[list[Any]], Awaitable[list[Any]]], *, batch_size: int,
                 coordinators: int, capacity: int, wait_ms: float = 2.):
        if not 1 <= batch_size <= 8 or not 1 <= coordinators <= 8 or not 1 <= capacity <= 256 or not 0 <= wait_ms <= 10:
            raise ValueError('invalid dense batching bounds')
        self.execute, self.batch_size = execute, batch_size
        self.coordinators, self.wait_seconds = coordinators, wait_ms/1000
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self.tasks: list[asyncio.Task] = []
        self.closed = False
        self.metrics = dict(submitted=0, batches=0, dispatched_requests=0, cancelled_before_dispatch=0,
                            queue_peak=0, max_batch_seen=0, assembly_wait_seconds=0., rejected=0)

    async def submit(self, request: Any) -> Any:
        if self.closed:
            raise RuntimeError('dense batcher is closed')
        future = asyncio.get_running_loop().create_future()
        # No await between starting the fixed coordinator set and enqueuing.
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._coordinate()) for _ in range(self.coordinators)]
        try:
            self.queue.put_nowait((request, future, time.perf_counter()))
        except asyncio.QueueFull:
            self.metrics['rejected'] += 1
            future.cancel()
            raise
        self.metrics['submitted'] += 1
        self.metrics['queue_peak'] = max(self.metrics['queue_peak'], self.queue.qsize())
        # HTTP cancellation cancels only this result; the native executor keeps
        # its completion-owned slot until the entire dispatched batch finishes.
        return await future

    async def _coordinate(self) -> None:
        while True:
            group = []
            try:
                group.append(await self.queue.get())
                deadline = asyncio.get_running_loop().time()+self.wait_seconds
                while len(group) < self.batch_size:
                    try:
                        item = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        remaining = deadline-asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            item = await asyncio.wait_for(self.queue.get(), remaining)
                        except asyncio.TimeoutError:
                            break
                    group.append(item)
                live = [item for item in group if not item[1].cancelled()]
                self.metrics['cancelled_before_dispatch'] += len(group)-len(live)
                if not live:
                    continue
                self.metrics['batches'] += 1
                self.metrics['dispatched_requests'] += len(live)
                self.metrics['max_batch_seen'] = max(self.metrics['max_batch_seen'], len(live))
                now = time.perf_counter()
                self.metrics['assembly_wait_seconds'] += sum(now-item[2] for item in live)
                try:
                    outputs = await self.execute([item[0] for item in live])
                    if len(outputs) != len(live):
                        raise ValueError('dense batch returned incorrect response count')
                except Exception as exc:
                    for _, future, _ in live:
                        if not future.done():
                            future.set_exception(exc)
                else:
                    for (_, future, _), response in zip(live, outputs):
                        if not future.done():
                            future.set_result(response)
            finally:
                for _, future, _ in group:
                    if not future.done():
                        future.cancel()
                    self.queue.task_done()

    async def close(self) -> None:
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while not self.queue.empty():
            _, future, _ = self.queue.get_nowait()
            if not future.done():
                future.cancel()
            self.queue.task_done()
