"""
audio_capture.py
----------------
WASAPI Loopback audio capture using the `soundcard` library.

soundcard explicitly supports Windows WASAPI Loopback via
    sc.get_microphone(id=..., include_loopback=True)

Threading model:
  AudioCapture.start() spawns a daemon thread that runs a blocking
  record loop.  On each block it calls the user callback with a mono
  float32 numpy array.  stop() sets a flag that the loop checks.
"""

import logging
import threading
import numpy as np
from typing import Callable, Optional

try:
    import soundcard as sc
except ImportError:
    sc = None  # handled at startup in main.py

log = logging.getLogger(__name__)

BLOCK_SIZE: int = 2048   # samples per callback (~43 ms at 48000 Hz)
SAMPLE_RATE: int = 48000


def find_loopback_device():
    """
    Return the best loopback microphone for the default speaker, or None.
    Returns a soundcard Microphone object.
    """
    if sc is None:
        return None

    try:
        default_speaker = sc.default_speaker()
        log.debug('Default speaker: %r', default_speaker.name)
    except Exception as e:
        log.error('Cannot get default speaker: %s', e)
        return None

    try:
        loopback = sc.get_microphone(
            id=default_speaker.name,
            include_loopback=True,
        )
        log.debug('Loopback device: %r', loopback.name)
        return loopback
    except Exception as e:
        log.warning('Cannot find loopback for default speaker: %s', e)

    # Fallback: first available loopback device
    try:
        all_mics = sc.all_microphones(include_loopback=True)
        for m in all_mics:
            if m.isloopback:
                log.debug('Fallback loopback device: %r', m.name)
                return m
    except Exception as e:
        log.error('Cannot enumerate loopback devices: %s', e)

    return None


class AudioCapture:
    """
    Captures system audio output (loopback) using soundcard.

    The `callback(mono_block, sample_rate)` is called from a background
    thread for each audio block.  It must be fast and non-blocking.

    Usage:
        device = find_loopback_device()
        capture = AudioCapture(callback=fn, device=device)
        capture.start()
        ...
        capture.stop()
    """

    def __init__(
        self,
        callback: Callable[[np.ndarray, int], None],
        device,                          # soundcard Microphone object
        sample_rate: int = SAMPLE_RATE,
        block_size: int = BLOCK_SIZE,
    ):
        if sc is None:
            raise RuntimeError('soundcard is not installed')

        self._callback = callback
        self._device = device
        self.sample_rate = sample_rate
        self._block_size = block_size
        self.device_name: str = device.name if device else ''
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        """Background thread: blocking record loop."""
        import ctypes
        # COM must be initialised on every thread that uses Media Foundation.
        # COINIT_MULTITHREADED = 0x0
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        log.debug('CoInitializeEx hr=0x%x', hr & 0xFFFFFFFF)
        try:
            log.debug('Record thread started for %r @ %d Hz', self.device_name, self.sample_rate)
            with self._device.recorder(
                samplerate=self.sample_rate,
                blocksize=self._block_size,
                channels=2,
            ) as recorder:
                while not self._stop_event.is_set():
                    data = recorder.record(numframes=self._block_size)
                    # data shape: (block_size, channels) float32
                    mono = data.mean(axis=1).astype(np.float32)
                    self._callback(mono, self.sample_rate)
        except Exception as e:
            log.error('Record thread error: %s', e, exc_info=True)
        finally:
            ctypes.windll.ole32.CoUninitialize()

    def start(self) -> None:
        """Start the background recording thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='audio-capture')
        self._thread.start()
        log.info('AudioCapture started: %r', self.device_name)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        log.info('AudioCapture stopped')

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
