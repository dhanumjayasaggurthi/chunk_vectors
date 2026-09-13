from __future__ import annotations

from contextlib import contextmanager
import threading


class ResourceGovernor:
    def __init__(self, vision: int = 16, chat: int = 6, embedding: int = 8):
        self.vision = threading.BoundedSemaphore(max(1, vision))
        self.chat = threading.BoundedSemaphore(max(1, chat))
        self.embedding = threading.BoundedSemaphore(max(1, embedding))

    @contextmanager
    def slot(self, sem: threading.BoundedSemaphore):
        sem.acquire()
        try:
            yield
        finally:
            sem.release()
