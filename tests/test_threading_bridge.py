"""
tests/test_threading_bridge.py
-------------------------------
Unit tests for listen_ghost.threading_bridge.AudioQueue.
"""

import queue
import threading

import pytest

from listen_ghost.threading_bridge import AudioQueue


class TestAudioQueue:

    def test_put_and_get(self):
        q = AudioQueue()
        q.put_nowait('hello')
        assert q.get_nowait() == 'hello'

    def test_get_empty_raises(self):
        q = AudioQueue()
        with pytest.raises(queue.Empty):
            q.get_nowait()

    def test_full_queue_does_not_block(self):
        """put_nowait on a full queue must silently drop — never raise or block."""
        q = AudioQueue(maxsize=2)
        q.put_nowait('a')
        q.put_nowait('b')
        # Third put on a full queue: must return immediately without raising
        q.put_nowait('c')   # 'c' is silently dropped
        assert q.get_nowait() == 'a'
        assert q.get_nowait() == 'b'
        with pytest.raises(queue.Empty):
            q.get_nowait()   # 'c' was never enqueued

    def test_put_from_thread_get_from_main(self):
        """Simulate the audio thread / UI thread pattern."""
        q = AudioQueue()
        results = []

        def producer():
            for i in range(5):
                q.put_nowait(i)

        t = threading.Thread(target=producer)
        t.start()
        t.join()

        try:
            while True:
                results.append(q.get_nowait())
        except queue.Empty:
            pass

        assert results == list(range(5))

    def test_accepts_arbitrary_payloads(self):
        q = AudioQueue()
        payload = {'notes': ['A4', 'F#5'], 'block': [0.1, 0.2]}
        q.put_nowait(payload)
        assert q.get_nowait() == payload
