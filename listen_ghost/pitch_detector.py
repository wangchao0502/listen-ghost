"""
pitch_detector.py
-----------------
FFT-based polyphonic pitch detection.

Pipeline:
  1. Silence gate (RMS threshold)
  2. Hann window + zero-padded rfft
  3. Convert to dB, band-limit to [FREQ_MIN, FREQ_MAX]
  4. Find local maxima above DB_THRESHOLD
  5. Suppress harmonics
  6. Convert frequency → MIDI → note name + octave
  7. Temporal smoothing via 3-frame majority vote
  8. Return top MAX_NOTES notes, sorted by loudness
"""

import numpy as np
from collections import deque
from typing import List, Tuple, Optional

# ── Tunable constants ────────────────────────────────────────────────────────

FREQ_MIN: float = 80.0       # Hz  — ignore below (bass rumble)
FREQ_MAX: float = 2000.0     # Hz  — ignore above (high harmonics)
DB_THRESHOLD: float = -30.0  # dB re peak; peaks below this are noise
MAX_NOTES: int = 6           # maximum polyphonic display count
FFT_PAD: int = 8192          # zero-pad length → ~5.4 Hz/bin at 44100 Hz
HARMONIC_TOL: float = 0.05   # ±5 % frequency tolerance for harmonic detection
SMOOTH_FRAMES: int = 3       # number of frames kept for majority-vote smoothing
SMOOTH_MIN_VOTES: int = 2    # minimum frames a note must appear in to be shown
RMS_SILENCE: float = 1e-4    # RMS below this → treat as silence

NOTE_NAMES: List[str] = [
    'C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'
]


# ── Core functions ────────────────────────────────────────────────────────────

def freq_to_note(freq: float) -> str:
    """Convert a frequency in Hz to scientific pitch notation (e.g. 440.0 → 'A4')."""
    if freq <= 0:
        return ''
    midi = 69.0 + 12.0 * np.log2(freq / 440.0)
    midi_int = int(round(midi))
    octave = (midi_int // 12) - 1
    name = NOTE_NAMES[midi_int % 12]
    return f"{name}{octave}"


def _suppress_harmonics(
    peaks: List[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """
    Remove peaks that are integer harmonics of a stronger fundamental.

    peaks: list of (db_value, freq_hz), sorted loudest-first.
    Returns a filtered list in the same order.
    """
    kept: List[Tuple[float, float]] = []
    for db_val, freq in peaks:
        is_harmonic = False
        for _ref_db, ref_freq in kept:
            for h in range(2, 9):
                ratio = freq / (ref_freq * h)
                if abs(ratio - 1.0) < HARMONIC_TOL:
                    is_harmonic = True
                    break
            if is_harmonic:
                break
        if not is_harmonic:
            kept.append((db_val, freq))
    return kept


def _find_peaks_in_band(
    db: np.ndarray, freqs: np.ndarray
) -> List[Tuple[float, float]]:
    """
    Find local maxima in the dB spectrum within [FREQ_MIN, FREQ_MAX]
    that exceed DB_THRESHOLD.

    Returns a list of (db_value, freq_hz) sorted by db descending.
    """
    mask = (freqs >= FREQ_MIN) & (freqs <= FREQ_MAX)
    band_db = db[mask]
    band_freq = freqs[mask]

    peaks: List[Tuple[float, float]] = []
    n = len(band_db)
    for i in range(1, n - 1):
        if band_db[i] < DB_THRESHOLD:
            continue
        if band_db[i] > band_db[i - 1] and band_db[i] > band_db[i + 1]:
            peaks.append((band_db[i], band_freq[i]))

    peaks.sort(key=lambda x: x[0], reverse=True)
    return peaks[:MAX_NOTES * 3]  # keep extra candidates for harmonic filtering


def _note_to_midi(note_name: str) -> Optional[int]:
    """Convert note name string (e.g. 'A4', 'F#5') to MIDI note number."""
    if not note_name or len(note_name) < 2:
        return None
    if '#' in note_name:
        letter = note_name[:2]   # e.g. 'F#'
        octave_str = note_name[2:]
    else:
        letter = note_name[0]
        octave_str = note_name[1:]
    try:
        octave = int(octave_str)
    except ValueError:
        return None
    if letter not in NOTE_NAMES:
        return None
    return NOTE_NAMES.index(letter) + (octave + 1) * 12


def _filter_close_notes(
    notes_with_db: List[Tuple[str, float]],
    min_semitones: int = 3,
) -> List[Tuple[str, float]]:
    """
    Remove notes that are within `min_semitones` of a louder note.
    Input is sorted loudest-first.
    """
    kept: List[Tuple[str, float]] = []
    for note, db_val in notes_with_db:
        midi = _note_to_midi(note)
        if midi is None:
            continue
        too_close = False
        for kept_note, _ in kept:
            kept_midi = _note_to_midi(kept_note)
            if kept_midi is not None and abs(midi - kept_midi) < min_semitones:
                too_close = True
                break
        if not too_close:
            kept.append((note, db_val))
    return kept


def _peaks_to_notes(peaks: List[Tuple[float, float]]) -> List[str]:
    """Convert (db, freq) peaks to deduplicated, well-separated note name strings."""
    # Suppress harmonics first
    filtered = _suppress_harmonics(peaks)

    # Convert to note names, deduplicate keeping loudest per note
    seen: dict = {}
    for db_val, freq in filtered:
        note = freq_to_note(freq)
        if not note:
            continue
        if note not in seen or db_val > seen[note]:
            seen[note] = db_val

    # Sort by loudness
    ordered = sorted(seen.items(), key=lambda x: x[1], reverse=True)

    # Remove notes that are too close in pitch to a louder note (≥3 semitones)
    separated = _filter_close_notes(ordered, min_semitones=3)

    return [note for note, _ in separated[:MAX_NOTES]]


# ── PitchDetector class (stateful, owns smoothing buffer) ────────────────────

class PitchDetector:
    """
    Stateful pitch detector that wraps the FFT pipeline and applies
    temporal smoothing via a majority-vote across the last N frames.

    Usage:
        detector = PitchDetector(sample_rate=44100)
        notes = detector.process(audio_block)  # call on each audio frame
    """

    def __init__(self, sample_rate: int = 44100):
        self.sample_rate = sample_rate
        self._history: deque = deque(maxlen=SMOOTH_FRAMES)

    def process(self, block: np.ndarray) -> List[str]:
        """
        Process a mono float32 audio block and return detected note names.

        Args:
            block: 1-D numpy float32 array of audio samples

        Returns:
            List of note name strings (e.g. ['A4', 'F#5', 'C3']),
            loudest first, or empty list if silent.
        """
        # 1. Silence gate
        rms = float(np.sqrt(np.mean(block ** 2)))
        if rms < RMS_SILENCE:
            self._history.append(set())
            return []

        # 2. Hann window + zero-padded rfft
        n = len(block)
        window = np.hanning(n)
        windowed = block * window

        spectrum = np.fft.rfft(windowed, n=FFT_PAD)
        magnitude = np.abs(spectrum)

        # 3. Convert to dB (relative to peak, avoiding log(0))
        peak = magnitude.max()
        if peak < 1e-10:
            self._history.append(set())
            return []
        magnitude = np.where(magnitude < 1e-10, 1e-10, magnitude)
        db = 20.0 * np.log10(magnitude / peak)

        # 4. Frequency axis
        freqs = np.fft.rfftfreq(FFT_PAD, d=1.0 / self.sample_rate)

        # 5. Find peaks in band
        peaks = _find_peaks_in_band(db, freqs)
        if not peaks:
            self._history.append(set())
            return []

        # 6. Convert to note names
        raw_notes = _peaks_to_notes(peaks)
        self._history.append(set(raw_notes))

        # 7. Temporal smoothing: only show notes that appear in >= SMOOTH_MIN_VOTES frames
        if len(self._history) < SMOOTH_FRAMES:
            # Not enough history yet — return raw result
            return raw_notes

        all_notes: set = set()
        for frame_notes in self._history:
            all_notes.update(frame_notes)

        stable: List[str] = []
        for note in raw_notes:  # preserve loudness order from latest frame
            vote_count = sum(1 for frame in self._history if note in frame)
            if vote_count >= SMOOTH_MIN_VOTES:
                stable.append(note)

        return stable
