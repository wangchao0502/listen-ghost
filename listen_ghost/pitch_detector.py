"""
pitch_detector.py
-----------------
Two pitch detection engines:

PitchDetector (通用 / general):
  FFT-based polyphonic detection, returns up to MAX_NOTES notes per frame.
  Pipeline:
    1. Silence gate  2. Hann window + zero-padded rfft  3. dB + band-limit
    4. Local-maxima search  5. Harmonic suppression  6. Frequency → note name
    7. 3-frame majority-vote smoothing

VocalPitchDetector (人声 / vocal):
  YIN autocorrelation algorithm, monophonic, optimised for singing voice.
  Pipeline:
    1. Silence gate  2. YIN difference function  3. CMNDF + threshold
    4. Parabolic interpolation  5. Confidence gate
    6. Median smoothing across recent valid frames
"""

import numpy as np
from collections import deque
from typing import List, Tuple, Optional

# ── Shared constants ─────────────────────────────────────────────────────────

RMS_SILENCE: float = 1e-4    # RMS below this → treat as silence

NOTE_NAMES: List[str] = [
    'C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'
]

# ── General-mode constants ───────────────────────────────────────────────────

FREQ_MIN: float = 80.0       # Hz  — ignore below (bass rumble)
FREQ_MAX: float = 2000.0     # Hz  — ignore above (high harmonics)
DB_THRESHOLD: float = -30.0  # dB re peak; peaks below this are noise
MAX_NOTES: int = 6           # maximum polyphonic display count
FFT_PAD: int = 8192          # zero-pad length → ~5.4 Hz/bin at 44100 Hz
HARMONIC_TOL: float = 0.05   # ±5 % frequency tolerance for harmonic detection
SMOOTH_FRAMES: int = 5       # number of frames kept for majority-vote smoothing
SMOOTH_MIN_VOTES: int = 3    # minimum frames a note must appear in to be shown
# Minimum dB (relative to peak) a candidate must reach to count as a "strong harmonic"
# during harmonic-coverage scoring.  Weaker peaks are spectral leakage or room noise,
# not genuine instrument partials.
HARMONIC_SCORE_MIN_DB: float = -20.0

# ── Vocal-mode constants ─────────────────────────────────────────────────────

# 80 Hz is too low for vocal mode: bass guitar/kick-drum fundamentals sit in
# the 80–140 Hz range and will dominate YIN over a genuine singing voice.
# Raising to 150 Hz excludes the bass fundamental while keeping all voice types
# (the lowest common bass-singer fundamental is ~82 Hz, but in typical pop
# production the bass is much louder and would always "win" below 150 Hz).
VOCAL_FREQ_MIN: float = 150.0   # Hz — YIN search lower bound (pop vocal safe floor)
VOCAL_FREQ_MAX: float = 1100.0  # Hz — highest soprano (~C6 ≈ 1047 Hz)
VOCAL_HP_CUTOFF: float = 150.0  # Hz — high-pass filter removes bass fundamental energy
YIN_THRESHOLD: float = 0.15     # CMNDF threshold; lower → stricter harmonic match
YIN_CONFIDENCE_MIN: float = 0.55  # 1 − CMNDF_min; reject below this
VOCAL_SMOOTH_FRAMES: int = 5    # frames kept for median-frequency smoothing
VOCAL_MIN_VALID: int = 2        # need at least this many valid frames before reporting
# FFT fallback search range: tighter than VOCAL_FREQ_MIN/MAX because in a full
# mix we want to avoid bass harmonics (often strong at 150–180 Hz).
VOCAL_FFT_MIN: float = 200.0    # Hz — FFT fallback lower bound (200 Hz > bass 2nd harmonic ~196 Hz)
VOCAL_FFT_MAX: float = 700.0    # Hz — covers all common singing fundamentals

# ── Core helpers (shared) ─────────────────────────────────────────────────────

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
    Remove peaks that are integer harmonics of any lower-frequency peak.

    Two peaks are in a harmonic relationship when freq_high / freq_low is
    within HARMONIC_TOL (±5%) of an integer ≥ 2.  In that case freq_high
    is suppressed and freq_low (the fundamental) is kept — regardless of
    which peak is louder.

    This direction-agnostic check is essential for instruments like piano
    where upper harmonics (especially the 2nd partial = octave above) are
    often louder than the fundamental.  The original loudness-first approach
    would keep the louder harmonic first and then fail to recognise the
    quieter fundamental as the true root.

    peaks: list of (db_value, freq_hz), sorted loudest-first.
    Returns a filtered list preserving the original loudness order.
    """
    if not peaks:
        return []

    # Collect all candidate frequencies sorted ascending so we always compare
    # a lower frequency against higher ones.
    all_freqs = sorted(f for _, f in peaks)

    # Mark every frequency that is an integer multiple of some lower frequency.
    harmonics: set = set()
    for i, f_low in enumerate(all_freqs):
        for f_high in all_freqs[i + 1:]:
            ratio = f_high / f_low
            nearest_int = round(ratio)
            if nearest_int >= 2 and abs(ratio - nearest_int) < HARMONIC_TOL:
                harmonics.add(f_high)

    # Return peaks not identified as harmonics, in the original loudness order.
    return [(db, f) for db, f in peaks if f not in harmonics]


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


def _harmonic_coverage(
    f_hz: float,
    all_peaks: List[Tuple[float, float]],
) -> int:
    """
    Count how many peaks in all_peaks are strong upper harmonics of f_hz.

    A peak qualifies when:
      • its dB value is ≥ HARMONIC_SCORE_MIN_DB (i.e. within 20 dB of the frame peak)
      • its frequency is above f_hz
      • freq / f_hz is within HARMONIC_TOL of an integer ≥ 2

    A high score indicates that f_hz is a genuine played fundamental with a
    visible harmonic series.  A low score (0 or 1) suggests a sympathetic
    resonance from another string, room reflection, or background noise.
    """
    count = 0
    for db_val, peak_f in all_peaks:
        if db_val < HARMONIC_SCORE_MIN_DB or peak_f <= f_hz:
            continue
        ratio = peak_f / f_hz
        nearest_h = round(ratio)
        if nearest_h >= 2 and abs(ratio - nearest_h) < HARMONIC_TOL:
            count += 1
    return count


def _peaks_to_notes(peaks: List[Tuple[float, float]]) -> List[str]:
    """Convert (db, freq) peaks to deduplicated, well-separated note name strings."""
    # Step 1 — Suppress peaks that are integer harmonics of a lower frequency.
    filtered = _suppress_harmonics(peaks)

    # Step 2 — Harmonic coverage filter.
    #
    # After harmonic suppression, surviving peaks are candidate fundamentals.
    # A genuinely played note has multiple strong harmonics in the spectrum
    # (score ≥ 2).  Sympathetic resonances (other strings vibrating in
    # sympathy with the played note) produce only 0–1 detected harmonics.
    #
    # Rule: if at least one surviving note has a harmonic score ≥ 2, apply a
    # coverage threshold (max_score − 2, minimum 1) so that all genuine chord
    # tones pass while sympathetic resonances are suppressed.
    #
    # Fallback: when no note has score ≥ 2 (pure sine tests, high notes above
    # ~E5 where few harmonics fit in FREQ_MAX, very quiet signals), all
    # surviving notes are kept.
    if filtered:
        scores = {f: _harmonic_coverage(f, peaks) for _, f in filtered}
        max_score = max(scores.values())
        if max_score >= 2:
            threshold = max(1, max_score - 2)
            filtered = [(db, f) for db, f in filtered if scores[f] >= threshold]

    # Step 3 — Convert to note names, deduplicate keeping loudest per note.
    seen: dict = {}
    for db_val, freq in filtered:
        note = freq_to_note(freq)
        if not note:
            continue
        if note not in seen or db_val > seen[note]:
            seen[note] = db_val

    # Sort by loudness
    ordered = sorted(seen.items(), key=lambda x: x[1], reverse=True)

    # Step 4 — Remove notes closer than 3 semitones to a louder neighbour.
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


# ── Vocal pre-processing ──────────────────────────────────────────────────────

def _highpass_fft(block: np.ndarray, cutoff: float, sample_rate: int) -> np.ndarray:
    """
    Zero-phase high-pass filter implemented via FFT.

    Applies a smooth linear rolloff from 0 (at 0.5×cutoff) to 1 (at cutoff)
    so there is no sharp brick-wall discontinuity in the spectrum.
    This is fast (two small FFTs on a 2048-sample block) and requires no
    additional dependencies.

    Purpose in vocal mode: attenuate bass guitar / kick-drum energy below
    VOCAL_HP_CUTOFF so it cannot dominate the YIN autocorrelation.
    """
    spectrum = np.fft.rfft(block)
    freqs = np.fft.rfftfreq(len(block), 1.0 / sample_rate)
    half = cutoff * 0.5
    mask = np.where(
        freqs < half, 0.0,
        np.where(freqs < cutoff, (freqs - half) / half, 1.0),
    )
    return np.fft.irfft(spectrum * mask, n=len(block)).astype(np.float32)


# ── YIN algorithm ─────────────────────────────────────────────────────────────

def _yin_pitch(block: np.ndarray, sample_rate: int) -> Tuple[Optional[float], float]:
    """
    YIN fundamental-frequency estimator (de Cheveigné & Kawahara, 2002).

    Returns (frequency_hz, confidence) where confidence = 1 − CMNDF_minimum.
    Returns (None, 0.0) when no reliable pitch is found.

    Frequency search range: [VOCAL_FREQ_MIN, VOCAL_FREQ_MAX].
    """
    min_lag = max(1, int(sample_rate / VOCAL_FREQ_MAX))   # ~44 @ 48 kHz
    max_lag = min(len(block) // 2, int(sample_rate / VOCAL_FREQ_MIN))  # ~600 @ 48 kHz

    if max_lag <= min_lag:
        return None, 0.0

    N = len(block)

    # Step 1 — Difference function d(τ) = Σ (x[j] − x[j+τ])²
    d = np.empty(max_lag + 1)
    d[0] = 0.0
    for tau in range(1, max_lag + 1):
        diff = block[:N - tau] - block[tau:N]
        d[tau] = float(np.dot(diff, diff))

    # Step 2 — Cumulative mean normalised difference function (CMNDF)
    d_prime = np.ones(max_lag + 1)  # d'[0] = 1 by definition
    cum = 0.0
    for tau in range(1, max_lag + 1):
        cum += d[tau]
        d_prime[tau] = d[tau] * tau / cum if cum > 0.0 else 1.0

    # Step 3 — Absolute threshold: first τ where d'[τ] < YIN_THRESHOLD
    tau_est: Optional[int] = None
    for tau in range(min_lag, max_lag):
        if d_prime[tau] < YIN_THRESHOLD:
            # Walk to local minimum
            while tau + 1 < max_lag and d_prime[tau + 1] < d_prime[tau]:
                tau += 1
            tau_est = tau
            break

    # Fallback — global minimum in search range (looser criterion)
    if tau_est is None:
        tau_est = int(np.argmin(d_prime[min_lag:max_lag + 1])) + min_lag
        if d_prime[tau_est] > 0.5:
            return None, 0.0

    # Step 4 — Parabolic interpolation for sub-sample accuracy
    best_tau: float = float(tau_est)
    if min_lag < tau_est < max_lag - 1:
        alpha = d_prime[tau_est - 1]
        beta  = d_prime[tau_est]
        gamma = d_prime[tau_est + 1]
        denom = 2.0 * beta - alpha - gamma
        if abs(denom) > 1e-10:
            best_tau = tau_est + 0.5 * (alpha - gamma) / denom
            best_tau = max(float(min_lag), min(best_tau, float(max_lag)))

    frequency = sample_rate / best_tau
    confidence = 1.0 - float(d_prime[tau_est])
    return frequency, confidence


# ── VocalPitchDetector class ──────────────────────────────────────────────────

class VocalPitchDetector:
    """
    Monophonic pitch detector optimised for human singing voice in a full mix.

    Detection pipeline per block:
      1. High-pass filter (VOCAL_HP_CUTOFF = 150 Hz) — removes bass fundamental
         so it cannot override the vocal in the autocorrelation.
      2. YIN autocorrelation (search range: VOCAL_FREQ_MIN–VOCAL_FREQ_MAX) —
         robust against "missing fundamental" situations common in singing.
      3. FFT-peak fallback (VOCAL_FFT_MIN–VOCAL_FFT_MAX) — used when YIN
         confidence is too low (polyphonic background); returns the loudest
         spectral peak in the vocal fundamental range.
      4. Median smoothing over the last VOCAL_SMOOTH_FRAMES valid frequencies —
         stable tracking of held notes, smooth through slides.

    Limitation: without source-separation, accuracy depends on how prominent
    the vocal is in the mix relative to instruments in the same frequency band.
    """

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._freq_history: deque = deque(maxlen=VOCAL_SMOOTH_FRAMES)

    # ── Public API ────────────────────────────────────────────────────────────

    def process(self, block: np.ndarray) -> List[str]:
        """
        Detect the dominant vocal pitch in `block` (mono float32).

        Returns a one-element list with the note name, or an empty list when
        the signal is silent or no confident vocal pitch is found.
        """
        rms = float(np.sqrt(np.mean(block ** 2)))
        if rms < RMS_SILENCE:
            self._freq_history.clear()
            return []

        # Step 1 — Remove bass fundamental energy (below 150 Hz).
        #  Critical: prevents the bass guitar/kick-drum fundamental from
        #  dominating the YIN autocorrelation and producing a false low pitch.
        filtered = _highpass_fft(block, VOCAL_HP_CUTOFF, self.sample_rate)

        # Step 2 — YIN: robust monophonic pitch estimator.
        #  VOCAL_FREQ_MIN=150 Hz means lag search ≤ 320 samples, so YIN
        #  cannot find the bass fundamental even if HP filter leaks.
        freq, confidence = _yin_pitch(filtered, self.sample_rate)

        if freq is None or confidence < YIN_CONFIDENCE_MIN:
            # Step 3 — FFT-peak fallback (200–700 Hz), with sub-harmonic
            #  rejection to avoid reporting bass harmonics as vocal notes.
            freq = self._fft_vocal_peak(block, filtered)

        if freq is None:
            self._freq_history.append(None)
            valid = [f for f in self._freq_history if f is not None]
            if len(valid) < VOCAL_MIN_VALID:
                return []
            return [freq_to_note(float(np.median(valid)))]

        self._freq_history.append(freq)
        valid = [f for f in self._freq_history if f is not None]
        if len(valid) < VOCAL_MIN_VALID:
            return [freq_to_note(freq)]

        # Step 4 — Median smoothing for stable pitch display.
        return [freq_to_note(float(np.median(valid)))]

    # ── Private helpers ───────────────────────────────────────────────────────

    def _fft_vocal_peak(
        self,
        original_block: np.ndarray,
        filtered_block: np.ndarray,
    ) -> Optional[float]:
        """
        Find the loudest local spectral maximum in [VOCAL_FFT_MIN, VOCAL_FFT_MAX]
        (200–700 Hz) of the HP-filtered signal.

        Sub-harmonic rejection (divisors 2–7): for each candidate frequency f,
        look for energy below VOCAL_HP_CUTOFF at f/d (d = 2..7) in the
        *original* signal.  If such energy is > 2× the energy at f, the
        candidate is almost certainly an upper harmonic of the bass — skip it.

        Note: this check is intentionally conservative.  Vocalists singing a
        note that aligns with a strong bass harmonic (ratio e_sub/e_f close
        to 2×) may be missed.  That is preferable to falsely reporting a bass
        harmonic as the singer's pitch.
        """
        n = len(filtered_block)
        window = np.hanning(n)

        spec_filt = np.abs(np.fft.rfft(filtered_block * window, n=FFT_PAD))
        freqs = np.fft.rfftfreq(FFT_PAD, 1.0 / self.sample_rate)
        peak_filt = spec_filt.max()
        if peak_filt < 1e-10:
            return None
        db = 20.0 * np.log10(np.maximum(spec_filt, 1e-10) / peak_filt)

        mask = (freqs >= VOCAL_FFT_MIN) & (freqs <= VOCAL_FFT_MAX)
        band_db = db[mask]
        band_freq = freqs[mask]

        candidates: List[Tuple[float, float]] = []
        m = len(band_db)
        for i in range(1, m - 1):
            if (band_db[i] > DB_THRESHOLD
                    and band_db[i] > band_db[i - 1]
                    and band_db[i] > band_db[i + 1]):
                candidates.append((band_db[i], band_freq[i]))

        if not candidates:
            return None

        candidates.sort(reverse=True)  # loudest first

        # Original-signal spectrum for sub-harmonic energy queries
        spec_orig = np.abs(np.fft.rfft(original_block * window, n=FFT_PAD))
        bin_hz = self.sample_rate / FFT_PAD   # zero-padded bin spacing (≈5.86 Hz @ 48 kHz)
        # Tolerance for sub-harmonic matching: 0.75 zero-padded bins ≈ 4.4 Hz.
        # True bass harmonics (sin waves at integer multiples of bass_fund) quantize
        # to within ≤ bin_hz*(0.5 + 0.5/n) of the fundamental, which is always
        # below 0.75*bin_hz for n ≥ 3.  Non-harmonics like C5/5 (dist ≈ 5 Hz) or
        # A4/4 (dist ≈ 9 Hz) are comfortably above this, so they are never rejected.
        harm_match_tol = 0.75 * bin_hz

        def _e_at(f_hz: float) -> float:
            idx = max(0, min(int(round(f_hz / bin_hz)), len(spec_orig) - 1))
            return float(spec_orig[idx])

        # Estimate the bass fundamental: strongest component below VOCAL_HP_CUTOFF.
        bass_freqs = freqs[freqs < VOCAL_HP_CUTOFF]
        bass_spec  = spec_orig[:len(bass_freqs)]
        bass_fund  = float(bass_freqs[bass_spec.argmax()]) if bass_spec.size > 0 else 0.0

        for db_val, f in candidates:
            e_f = _e_at(f)
            is_bass_harmonic = False

            if bass_fund >= 40.0:
                # Sub-harmonic check: for each divisor d, compute sub_f = f/d.
                # If sub_f lands within harm_match_tol of the bass fundamental
                # (meaning f ≈ d × bass_fund, i.e. f IS a bass harmonic),
                # and the bass fundamental energy overwhelms f's energy, reject.
                for divisor in range(2, 8):
                    sub_f = f / divisor
                    if sub_f >= VOCAL_HP_CUTOFF:
                        continue
                    # Nearest harmonic of bass_fund to sub_f
                    h_nearest = round(sub_f / bass_fund)
                    nearest_harm = bass_fund * h_nearest
                    if abs(sub_f - nearest_harm) > harm_match_tol:
                        continue  # sub_f is not close to any bass harmonic
                    e_sub = _e_at(nearest_harm)  # energy at the actual harmonic
                    if e_f > 0 and (e_sub / e_f) > 2.0:
                        is_bass_harmonic = True
                        break
            # (No bass detected: don't reject any candidate)

            if not is_bass_harmonic:
                return f

        return None
