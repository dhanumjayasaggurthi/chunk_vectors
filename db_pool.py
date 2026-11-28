"""Bounded, process-local PostgreSQL connection reuse.

Opt-in via [POSTGRES] pool_enabled=true. All existing callers retain the same
get_conn()/cursor()/commit()/rollback()/close() contract. A borrowed close()
returns its connection to the pool, always rolling back and resetting session
state to avoid read-only/isolation setting leaks between workers.
"""
from __future__ import annotations

import os
import threading
from psycopg2.pool import ThreadedConnectionPool


class PooledConnection:
    def __init__(self, raw, owner):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_returned", False)

    def __getattr__(self, name):
        if self._returned:
            raise RuntimeError("PostgreSQL lease already returned")
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            if self._returned:
                raise RuntimeError("PostgreSQL lease already returned")
            setattr(self._raw, name, value)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, trace):
        # Match psycopg2's existing 'with connection' transaction behavior:
        # callers still explicitly close() after leaving the transaction.
        return self._raw.__exit__(exc_type, exc_value, trace)

    def close(self):
        if self._returned:
            return
        object.__setattr__(self, "_returned", True)
        self._owner.release(self._raw)


class ConnectionPoolManager:
    def __init__(self, settings):
        self.pid = os.getpid()
        self.min_conn = int(settings["pool_min_connections"])
        self.max_conn = int(settings["pool_max_connections"])
        self.timeout = int(settings["pool_acquire_timeout_seconds"])
        self._slots = threading.BoundedSemaphore(self.max_conn)
        self._pool = ThreadedConnectionPool(
            self.min_conn,self.max_conn,
            host=settings["host"], port=settings["port"],
            dbname=settings["database"], user=settings["user"],
            password=settings["password"],
            connect_timeout=settings["connect_timeout_seconds"],
        )

    def acquire(self):
        if os.getpid() != self.pid:
            raise RuntimeError("PostgreSQL connection pool must not cross fork")
        if not self._slots.acquire(timeout=self.timeout):
            raise TimeoutError(
                f"PostgreSQL pool exhausted: max={self.max_conn} "
                f"timeout={self.timeout}s; tune [POSTGRES] pool_* settings"
            )
        try:
            raw = self._pool.getconn()
            if raw.closed:
                self._pool.putconn(raw, close=True)
                raw = self._pool.getconn()
            return PooledConnection(raw, self)
        except Exception:
            self._slots.release()
            raise

    def release(self, raw):
        try:
            healthy = not raw.closed
            if healthy:
                try:
                    # DISCARD ALL style reset prevents transaction/read-only
                    # and session settings from contaminating the next owner.
                    raw.rollback()
                    raw.reset()
                except Exception:
                    healthy = False
            self._pool.putconn(raw, close=not healthy)
        finally:
            self._slots.release()

    def closeall(self):
        self._pool.closeall()


_manager = None
_manager_lock = threading.Lock()


def acquire(settings):
    global _manager
    with _manager_lock:
        # Windows spawn gets a new process; forked workers must never reuse
        # inherited PostgreSQL sockets.
        if _manager is None or _manager.pid != os.getpid():
            _manager = ConnectionPoolManager(settings)
        manager = _manager
    return manager.acquire()


def close_pool():
    """Explicit process-local shutdown; call only after workers have stopped."""
    global _manager
    with _manager_lock:
        manager = _manager
        _manager = None
    if manager and manager.pid == os.getpid():
        manager.closeall()
