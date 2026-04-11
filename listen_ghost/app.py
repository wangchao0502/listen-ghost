"""
app.py
------
tkinter UI for listen-ghost.

Layout (400 × 400 px):
  ┌───────────────────────────────┐
  │  [        START / STOP       ]│  44px button
  ├───────────────────────────────┤
  │  device name / status         │  status label (small)
  │                               │
  │     A4   F#5   C3             │  note display (36pt bold)
  │                               │
  │  ▁▃▇▅▂▁▁▄▆▃▁▁▁▁             │  spectrum canvas (80px)
  └───────────────────────────────┘

Threading:
  Audio thread  → pitch_detector.process() → AudioQueue.put_nowait()
  Main thread   → root.after(33ms)          → _poll_queue() → update UI
"""

import ctypes
import logging
import queue
import tkinter as tk
from tkinter import messagebox
from typing import List, Optional

log = logging.getLogger(__name__)

import numpy as np

from listen_ghost.audio_capture import AudioCapture, find_loopback_device
from listen_ghost.pitch_detector import (
    PitchDetector, VocalPitchDetector,
    VOCAL_FREQ_MIN, VOCAL_FREQ_MAX,
)
from listen_ghost.threading_bridge import AudioQueue

# ── Scene constants ───────────────────────────────────────────────────────────

SCENE_GENERAL: str = 'general'   # polyphonic FFT — any pitched sound
SCENE_VOCAL: str   = 'vocal'     # monophonic YIN — optimised for singing voice

# ── UI constants ──────────────────────────────────────────────────────────────

WINDOW_W: int = 400
WINDOW_H: int = 400
SCREEN_PADDING: int = 12     # px gap from screen edge

BG_DARK: str = '#1a1a2e'
BG_CANVAS: str = '#0d0d1a'
FG_NOTE: str = '#e0e0ff'
FG_MUTED: str = '#3a3a5c'
FG_STATUS: str = '#6a6a9a'
FG_ERROR: str = '#ff6b6b'
ACCENT: str = '#7b68ee'
ACCENT_STOP: str = '#c0392b'
BG_SCENE_ACTIVE: str = '#2a2a4e'   # scene button: selected
BG_SCENE_IDLE: str   = '#111126'   # scene button: unselected

NOTE_FONT: tuple = ('Consolas', 34, 'bold')
STATUS_FONT: tuple = ('Segoe UI', 8)
BTN_FONT: tuple = ('Segoe UI', 12, 'bold')
SCENE_FONT: tuple = ('Segoe UI', 9)

SPECTRUM_BARS: int = 48       # number of bars in the spectrum display
SPECTRUM_FREQ_MIN: float = 80.0
SPECTRUM_FREQ_MAX: float = 2000.0
POLL_INTERVAL_MS: int = 33    # ~30 fps


# ── Helper: get usable screen area via ctypes ─────────────────────────────────

def _get_work_area():
    """Return (left, top, right, bottom) of the primary monitor's work area."""

    class RECT(ctypes.Structure):
        _fields_ = [
            ('left', ctypes.c_long), ('top', ctypes.c_long),
            ('right', ctypes.c_long), ('bottom', ctypes.c_long),
        ]

    rect = RECT()
    try:
        # SPI_GETWORKAREA = 48
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        # Fallback: use tkinter screen dimensions
        return 0, 0, 1920, 1040


# ── Main application class ────────────────────────────────────────────────────

class ListenGhostApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self._running: bool = False
        self._capture: Optional[AudioCapture] = None
        self._detector = None          # PitchDetector | VocalPitchDetector | None
        self._audio_queue: AudioQueue = AudioQueue(maxsize=10)
        self._spectrum_data: Optional[np.ndarray] = None
        self._scene: str = SCENE_VOCAL  # default — the most common use case

        self._setup_window()
        self._build_ui()
        self._build_spectrum_bars()
        self._poll_queue()  # kick off the UI polling loop

    # ── Window setup ──────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        log.debug('Setting up window')
        self.root.title('listen-ghost')
        self.root.resizable(False, False)
        self.root.attributes('-topmost', True)
        self.root.configure(bg=BG_DARK)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        left, top, right, bottom = _get_work_area()
        x = right - WINDOW_W - SCREEN_PADDING
        y = bottom - WINDOW_H - SCREEN_PADDING
        self.root.geometry(f'{WINDOW_W}x{WINDOW_H}+{x}+{y}')

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Start / Stop button ──
        self.btn_toggle = tk.Button(
            self.root,
            text='START',
            bg=ACCENT,
            fg='white',
            activebackground='#9b8eff',
            activeforeground='white',
            font=BTN_FONT,
            relief='flat',
            bd=0,
            cursor='hand2',
            command=self._toggle_capture,
        )
        self.btn_toggle.pack(fill='x', padx=8, pady=(8, 2))

        # ── Scene selector ──
        scene_frame = tk.Frame(self.root, bg=BG_DARK)
        scene_frame.pack(fill='x', padx=8, pady=(0, 2))

        tk.Label(
            scene_frame, text='场景：', bg=BG_DARK, fg=FG_STATUS, font=SCENE_FONT,
        ).pack(side='left')

        self._scene_btns: dict = {}
        for label, value, tooltip in [
            ('通用', SCENE_GENERAL, '多音 · 乐器 / 和声'),
            ('人声', SCENE_VOCAL,   '单音 · 演唱者音高'),
        ]:
            btn = tk.Button(
                scene_frame,
                text=label,
                font=SCENE_FONT,
                relief='flat', bd=0,
                padx=10, pady=2,
                cursor='hand2',
                command=lambda v=value: self._set_scene(v),
            )
            btn.pack(side='left', padx=(0, 3))
            self._scene_btns[value] = btn

        # Initialise button highlight to match default scene
        self._refresh_scene_buttons()

        # ── Status / device label ──
        self.lbl_status = tk.Label(
            self.root,
            text='准备就绪 — 点击 START 开始监听',
            bg=BG_DARK,
            fg=FG_STATUS,
            font=STATUS_FONT,
            anchor='w',
        )
        self.lbl_status.pack(fill='x', padx=10, pady=(0, 4))

        # ── Note display area ──
        self.note_frame = tk.Frame(self.root, bg=BG_DARK)
        self.note_frame.pack(fill='both', expand=True, padx=8)

        self.lbl_notes = tk.Label(
            self.note_frame,
            text='·',
            bg=BG_DARK,
            fg=FG_MUTED,
            font=NOTE_FONT,
            wraplength=380,
            justify='center',
            anchor='center',
        )
        self.lbl_notes.pack(expand=True, fill='both')

        # ── Spectrum canvas ──
        self.canvas = tk.Canvas(
            self.root,
            bg=BG_CANVAS,
            height=80,
            highlightthickness=0,
        )
        self.canvas.pack(fill='x', padx=8, pady=(0, 8))

    def _build_spectrum_bars(self) -> None:
        """Pre-create rectangle items on the canvas for fast updates."""
        self._bar_ids: List[int] = []
        canvas_w = WINDOW_W - 16  # padx=8 on each side
        bar_w = max(1, canvas_w // SPECTRUM_BARS - 1)
        gap = (canvas_w - SPECTRUM_BARS * (bar_w + 1)) // 2

        for i in range(SPECTRUM_BARS):
            x0 = gap + i * (bar_w + 1)
            x1 = x0 + bar_w
            bar_id = self.canvas.create_rectangle(
                x0, 79, x1, 79,
                fill=ACCENT, outline='',
            )
            self._bar_ids.append(bar_id)

    # ── Scene management ──────────────────────────────────────────────────────

    def _set_scene(self, scene: str) -> None:
        """Switch detection scene.  Safe to call while capture is running."""
        if scene == self._scene:
            return
        self._scene = scene
        self._refresh_scene_buttons()
        log.info('Scene switched to %r', scene)
        # Hot-swap the detector so the audio thread picks it up immediately.
        # CPython assignment is atomic w.r.t. the GIL, so no explicit lock needed.
        if self._running and self._capture is not None:
            self._detector = self._make_detector(self._capture.sample_rate)

    def _refresh_scene_buttons(self) -> None:
        """Highlight the currently active scene button."""
        for value, btn in self._scene_btns.items():
            if value == self._scene:
                btn.config(bg=ACCENT, fg='white')
            else:
                btn.config(bg=BG_SCENE_ACTIVE, fg=FG_STATUS)

    def _make_detector(self, sample_rate: int):
        """Return a freshly constructed detector for the current scene."""
        if self._scene == SCENE_VOCAL:
            return VocalPitchDetector(sample_rate=sample_rate)
        return PitchDetector(sample_rate=sample_rate)

    # ── Audio control ─────────────────────────────────────────────────────────

    def _toggle_capture(self) -> None:
        if self._running:
            self._stop_capture()
        else:
            self._start_capture()

    def _start_capture(self) -> None:
        log.info('Starting capture...')
        device = find_loopback_device()
        if device is None:
            msg = '未找到系统音频 Loopback 设备，请检查声卡驱动'
            log.error(msg)
            self.lbl_status.config(text=msg, fg=FG_ERROR)
            return

        log.info('Using loopback device: %r', device.name)
        try:
            self._capture = AudioCapture(
                callback=self._on_audio_block,
                device=device,
            )
            self._detector = self._make_detector(self._capture.sample_rate)
            log.info('AudioCapture created: %s @ %d Hz  scene=%s',
                     self._capture.device_name, self._capture.sample_rate, self._scene)
            self._capture.start()
            log.info('Stream started')
        except Exception as exc:
            import traceback
            log.error('Failed to start capture:\n%s', traceback.format_exc())
            self.lbl_status.config(text=f'启动失败：{exc}', fg=FG_ERROR)
            self._capture = None
            return

        device_name = self._capture.device_name[:42]
        self.lbl_status.config(text=device_name, fg=FG_STATUS)
        self._running = True
        self.btn_toggle.config(text='STOP', bg=ACCENT_STOP)

    def _stop_capture(self) -> None:
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        self._detector = None
        self.btn_toggle.config(text='START', bg=ACCENT)
        self.lbl_notes.config(text='·', fg=FG_MUTED)
        self.lbl_status.config(text='已停止 — 点击 START 重新开始', fg=FG_STATUS)
        self._clear_spectrum()

    def _on_close(self) -> None:
        self._stop_capture()
        self.root.destroy()

    # ── Audio thread callback ─────────────────────────────────────────────────

    def _on_audio_block(self, mono: np.ndarray, sample_rate: int) -> None:
        """
        Called on the audio thread.
        Runs pitch detection and queues results for the UI thread.
        Must be fast and non-blocking.
        """
        if self._detector is None:
            return
        notes = self._detector.process(mono)
        # Also queue a small spectrum snapshot for the visualizer
        self._audio_queue.put_nowait({'notes': notes, 'block': mono})

    # ── UI polling (main thread) ──────────────────────────────────────────────

    def _poll_queue(self) -> None:
        """Drains the audio queue and updates the UI. Runs on the main thread."""
        latest = None
        try:
            while True:
                latest = self._audio_queue.get_nowait()
        except queue.Empty:
            pass

        if latest is not None:
            self._update_notes(latest.get('notes', []))
            block = latest.get('block')
            if block is not None:
                self._update_spectrum(block)

        self.root.after(POLL_INTERVAL_MS, self._poll_queue)

    # ── Display update ────────────────────────────────────────────────────────

    def _update_notes(self, notes: List[str]) -> None:
        if notes:
            self.lbl_notes.config(
                text='   '.join(notes),
                fg=FG_NOTE,
            )
        else:
            self.lbl_notes.config(text='·', fg=FG_MUTED)

    def _update_spectrum(self, block: np.ndarray) -> None:
        """Render a mini bar-graph spectrum on the canvas."""
        if not self._bar_ids:
            return

        n = len(block)
        from listen_ghost.pitch_detector import FFT_PAD
        window = np.hanning(n)
        spectrum = np.abs(np.fft.rfft(block * window, n=FFT_PAD))
        freqs = np.fft.rfftfreq(FFT_PAD, d=1.0 / (self._capture.sample_rate if self._capture else 48000))

        # In vocal mode narrow the spectrum view to the vocal range so the
        # singer's fundamental is always in the visible window.
        spec_max = VOCAL_FREQ_MAX if self._scene == SCENE_VOCAL else SPECTRUM_FREQ_MAX

        # Log-spaced frequency bins for the bars
        log_freqs = np.logspace(
            np.log10(SPECTRUM_FREQ_MIN),
            np.log10(spec_max),
            num=SPECTRUM_BARS + 1,
        )

        canvas_h = 80
        bar_max_h = canvas_h - 2

        for i, bar_id in enumerate(self._bar_ids):
            f_lo = log_freqs[i]
            f_hi = log_freqs[i + 1]
            mask = (freqs >= f_lo) & (freqs < f_hi)
            if not mask.any():
                bar_h = 0
            else:
                avg_mag = float(spectrum[mask].mean())
                # Normalise: 0–1 using a soft log scale
                val = np.log1p(avg_mag * 200.0) / np.log1p(200.0)
                bar_h = int(val * bar_max_h)

            y1 = canvas_h - 1
            y0 = y1 - bar_h
            coords = self.canvas.coords(bar_id)
            if coords:
                self.canvas.coords(bar_id, coords[0], y0, coords[2], y1)

    def _clear_spectrum(self) -> None:
        canvas_h = 80
        for bar_id in self._bar_ids:
            coords = self.canvas.coords(bar_id)
            if coords:
                self.canvas.coords(bar_id, coords[0], canvas_h - 1, coords[2], canvas_h - 1)


# ── Entry point for this module ───────────────────────────────────────────────

def main() -> None:
    root = tk.Tk()
    app = ListenGhostApp(root)  # noqa: F841
    root.mainloop()
