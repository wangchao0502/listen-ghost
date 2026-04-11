"""
threading_bridge.py
-------------------
Thread-safe queue connecting the audio capture thread to the tkinter UI thread.

Rules:
- Producer (audio thread) must NEVER block → put_nowait, discard on Full
- Consumer (UI thread) drains via get_nowait(), handles Empty itself
"""

import queue
from typing import Any


class AudioQueue:
    """
    Thin wrapper around queue.Queue that ensures the producer (audio thread)
    can never block. If the queue is full, the new item is silently discarded —
    the UI will simply display the next available frame.
    """

    def __init__(self, maxsize: int = 10):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put_nowait(self, item: Any) -> None:
        """Put item without blocking. Silently drops item if queue is full."""
        try:
            self._q.put_nowait(item)
        except queue.Full:
            pass

    def get_nowait(self) -> Any:
        """Get item without blocking. Raises queue.Empty if queue is empty."""
        return self._q.get_nowait()
