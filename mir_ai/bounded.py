from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from collections.abc import Iterable, Iterator, Callable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def bounded_parallel_map(func: Callable[[T], R], iterable: Iterable[T], max_workers: int, max_inflight: int) -> Iterator[R]:
    if max_workers < 1 or max_inflight < 1:
        raise ValueError("max_workers and max_inflight must be >= 1")
    max_inflight = max(max_workers, max_inflight)
    source = iter(iterable)
    pending: set[Future[R]] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        exhausted = False
        while not exhausted or pending:
            while not exhausted and len(pending) < max_inflight:
                try:
                    item = next(source)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(pool.submit(func, item))
            if not pending:
                continue
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                yield fut.result()


def bounded_ordered_map(func: Callable[[T], R], iterable: Iterable[T], max_workers: int, max_inflight: int) -> Iterator[R]:
    from collections import deque
    if max_workers < 1 or max_inflight < 1:
        raise ValueError("max_workers and max_inflight must be >= 1")
    max_inflight = max(max_workers, max_inflight)
    source = iter(iterable)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        q = deque()
        exhausted = False
        while not exhausted or q:
            while not exhausted and len(q) < max_inflight:
                try:
                    item = next(source)
                except StopIteration:
                    exhausted = True
                    break
                q.append(pool.submit(func, item))
            if q:
                yield q.popleft().result()


class ContiguousProgress:
    def __init__(self, start_completed: int = 0):
        self.completed = start_completed
        self._pending: set[int] = set()

    def mark(self, value: int) -> int:
        if value <= self.completed:
            return self.completed
        self._pending.add(value)
        while self.completed + 1 in self._pending:
            self._pending.remove(self.completed + 1)
            self.completed += 1
        return self.completed
