"""Keyed async burst debouncer with bounded maximum wait."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _PendingBucket(Generic[T]):
    items: list[T] = field(default_factory=list)
    first_mono: float = 0.0
    task: asyncio.Task[None] | None = None


class BurstDebouncer(Generic[T]):
    """Coalesces rapid message bursts per key and invokes an async callback on flush."""

    def __init__(
        self,
        on_flush: Callable[[str, list[T]], Awaitable[None]],
        *,
        debounce_s: float = 25.0,
        max_debounce_s: float = 60.0,
    ) -> None:
        self._on_flush = on_flush
        self.debounce_s = max(0.0, float(debounce_s))
        self.max_debounce_s = max(self.debounce_s, float(max_debounce_s))
        self._buckets: dict[str, _PendingBucket[T]] = {}

    def pending_count(self, key: str | None = None) -> int:
        if key is not None:
            b = self._buckets.get(key)
            return len(b.items) if b else 0
        return sum(len(b.items) for b in self._buckets.values())

    def add(self, key: str, items: list[T], *, arm_timer: bool = True) -> None:
        if not items:
            return
        loop = asyncio.get_running_loop()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _PendingBucket(first_mono=loop.time())
            self._buckets[key] = bucket
        bucket.items.extend(items)

        if bucket.task is not None and not bucket.task.done():
            bucket.task.cancel()
            bucket.task = None

        if not arm_timer:
            return

        elapsed = max(0.0, loop.time() - bucket.first_mono)
        delay = min(self.debounce_s, max(0.0, self.max_debounce_s - elapsed))

        async def _wait_and_flush() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.flush(key)
            except asyncio.CancelledError:
                return

        bucket.task = asyncio.create_task(_wait_and_flush())

    async def flush(self, key: str) -> list[T]:
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            return []
        current = asyncio.current_task()
        if bucket.task is not None and bucket.task is not current and not bucket.task.done():
            bucket.task.cancel()
        items = list(bucket.items)
        if items:
            await self._on_flush(key, items)
        return items

    async def flush_all(self) -> int:
        keys = list(self._buckets.keys())
        total = 0
        for key in keys:
            flushed = await self.flush(key)
            total += len(flushed)
        return total

    def cancel_all(self) -> None:
        for bucket in self._buckets.values():
            if bucket.task is not None and not bucket.task.done():
                bucket.task.cancel()
        self._buckets.clear()
