"""In-process host callback; never serialized into Worker or checkpoint state."""
from contextlib import contextmanager
from contextvars import ContextVar

_guard = ContextVar("agentg_worker_memory_guard", default=None)


@contextmanager
def worker_memory_guard(callback):
    token = _guard.set(callback)
    try:
        yield
    finally:
        _guard.reset(token)


def refresh_worker_memory(state):
    callback = _guard.get()
    if callback is not None:
        callback(state)
