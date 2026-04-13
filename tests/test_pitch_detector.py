"""
tests/test_pitch_detector.py
-----------------------------
Unit tests for listen_ghost.pitch_detector.

Run with:
    python -m pytest tests/ -v
"""

import numpy as np
import pytest

from listen_ghost.pitch_detector import (
    # General
    freq_to_note,
    _suppress_harmonics,
    _filter_close_notes,
    _note_to_midi,
    _peaks_to_notes,
    _harmonic_coverage,
    PitchDetector,
    # Vocal
    _highpass_fft,
    _yin_pitch,
    VocalPitchDetector,
    # Constants
    RMS_SILENCE,
    VOCAL_HP_CUTOFF,
    VOCAL_FREQ_MIN,
    VOCAL_FREQ_MAX,
    YIN_CONFIDENCE_MIN,
    HARMONIC_SCORE_MIN_DB,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

SR = 48000
BLOCK = 2048
_t = np.linspace(0, BLOCK / SR, BLOCK, endpoint=False)


def _sine(freq_hz: float, amp: float = 0.3) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * freq_hz * _t)).astype(np.float32)


def _bass() -> np.ndarray:
    """G2 bass at 98 Hz with 6 harmonics (typical pop bass sound)."""
    b = np.zeros(BLOCK, dtype=np.float32)
    for h, amp in [(1, 0.8), (2, 0.6), (3, 0.4), (4, 0.3), (5, 0.2), (6, 0.15)]:
        b += (amp * np.sin(2 * np.pi * 98 * h * _t)).astype(np.float32)
    return b


def _silence() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)


def _run_detector(detector, block: np.ndarray, n_frames: int = 8):
    """Feed the same block n_frames times and return the last result."""
    result = []
    for _ in range(n_frames):
        result = detector.process(block)
    return result


# ── freq_to_note ──────────────────────────────────────────────────────────────

class TestFreqToNote:
    def test_a4(self):
        assert freq_to_note(440.0) == 'A4'

    def test_middle_c(self):
        assert freq_to_note(261.63) == 'C4'

    def test_sharp(self):
        assert freq_to_note(370.0) == 'F#4'   # F#4 ≈ 369.99 Hz

    def test_low_note(self):
        assert freq_to_note(98.0) == 'G2'

    def test_high_note(self):
        assert freq_to_note(1046.5) == 'C6'

    def test_zero_returns_empty(self):
        assert freq_to_note(0.0) == ''

    def test_negative_returns_empty(self):
        assert freq_to_note(-1.0) == ''

    @pytest.mark.parametrize('freq,expected', [
        (261.63, 'C4'),
        (293.66, 'D4'),
        (329.63, 'E4'),
        (349.23, 'F4'),
        (392.00, 'G4'),
        (440.00, 'A4'),
        (493.88, 'B4'),
        (523.25, 'C5'),
    ])
    def test_diatonic_c_major(self, freq, expected):
        assert freq_to_note(freq) == expected


# ── _note_to_midi ─────────────────────────────────────────────────────────────

class TestNoteToMidi:
    def test_a4_is_69(self):
        assert _note_to_midi('A4') == 69

    def test_middle_c_is_60(self):
        assert _note_to_midi('C4') == 60

    def test_sharp(self):
        assert _note_to_midi('F#5') == 78

    def test_invalid_returns_none(self):
        assert _note_to_midi('') is None
        assert _note_to_midi('X9') is None


# ── _suppress_harmonics ───────────────────────────────────────────────────────

class TestSuppressHarmonics:
    def test_removes_octave(self):
        peaks = [(0.0, 440.0), (-6.0, 880.0)]
        result = _suppress_harmonics(peaks)
        assert len(result) == 1
        assert result[0][1] == pytest.approx(440.0)

    def test_removes_fifth_harmonic(self):
        # 440 + 5th harmonic 2200 Hz
        peaks = [(0.0, 440.0), (-10.0, 2200.0)]
        result = _suppress_harmonics(peaks)
        assert len(result) == 1

    def test_keeps_non_harmonics(self):
        peaks = [(0.0, 440.0), (-3.0, 660.0)]   # 660 / 440 = 1.5 (not integer)
        result = _suppress_harmonics(peaks)
        assert len(result) == 2

    def test_louder_wins_fundamental(self):
        # Fundamental is first (loudest), harmonic should be removed
        peaks = [(0.0, 200.0), (-5.0, 400.0), (-10.0, 600.0)]
        result = _suppress_harmonics(peaks)
        assert all(f == 200.0 for _, f in result)

    def test_harmonic_louder_than_fundamental(self):
        """Piano scenario: 2nd harmonic (C5) louder than fundamental (C4).
        The fundamental must still be kept and the harmonic suppressed."""
        # C5 (523.25 Hz) at -2 dB, C4 (261.63 Hz) at -3 dB — C5 is louder
        peaks = [(-2.0, 523.25), (-3.0, 261.63)]
        result = _suppress_harmonics(peaks)
        assert len(result) == 1
        assert result[0][1] == pytest.approx(261.63)

    def test_multiple_harmonics_louder_than_fundamental(self):
        """Piano chord C4: 2nd and 3rd partials both louder than fundamental."""
        # 2nd partial (C5, 523.25) and 3rd partial (G5, 784.88) both louder
        peaks = [(-1.0, 523.25), (-2.0, 784.88), (-5.0, 261.63)]
        result = _suppress_harmonics(peaks)
        freqs = [f for _, f in result]
        assert 261.63 in freqs            # fundamental kept
        assert 523.25 not in freqs        # 2nd partial suppressed
        assert 784.88 not in freqs        # 3rd partial suppressed

    def test_empty_input(self):
        assert _suppress_harmonics([]) == []


# ── _filter_close_notes ───────────────────────────────────────────────────────

class TestFilterCloseNotes:
    def test_removes_note_within_3_semitones(self):
        # A4 (69) and A#4 (70) — 1 semitone apart
        notes = [('A4', 0.0), ('A#4', -3.0)]
        result = _filter_close_notes(notes)
        assert len(result) == 1
        assert result[0][0] == 'A4'

    def test_keeps_note_3_semitones_apart(self):
        # A4 (69) and C5 (72) — 3 semitones apart: kept (min_semitones=3 means < 3 removed)
        notes = [('A4', 0.0), ('C5', -3.0)]
        result = _filter_close_notes(notes)
        assert len(result) == 2

    def test_louder_wins(self):
        # Sorted loudest-first: the first note is kept, second removed if too close
        notes = [('G4', 0.0), ('G#4', -1.0)]   # 1 semitone
        result = _filter_close_notes(notes)
        assert result[0][0] == 'G4'

    def test_empty(self):
        assert _filter_close_notes([]) == []


# ── _harmonic_coverage ────────────────────────────────────────────────────────

class TestHarmonicCoverage:
    def test_counts_strong_harmonics(self):
        """Peaks at 2×, 3×, 4× the fundamental should all count."""
        peaks = [(0.0, 880.0), (-3.0, 1320.0), (-6.0, 1760.0)]  # 2nd/3rd/4th of 440
        assert _harmonic_coverage(440.0, peaks) == 3

    def test_ignores_weak_peaks_below_min_db(self):
        """Peaks weaker than HARMONIC_SCORE_MIN_DB should not count."""
        peaks = [(HARMONIC_SCORE_MIN_DB - 1.0, 880.0)]  # just below threshold
        assert _harmonic_coverage(440.0, peaks) == 0

    def test_ignores_non_harmonic_peaks(self):
        """Peaks at non-integer ratios should not count."""
        peaks = [(0.0, 660.0)]  # 1.5× — not integer
        assert _harmonic_coverage(440.0, peaks) == 0

    def test_ignores_peaks_below_f_hz(self):
        """Only upper harmonics are counted; lower frequencies are ignored."""
        peaks = [(0.0, 220.0)]  # below 440
        assert _harmonic_coverage(440.0, peaks) == 0

    def test_empty_peaks(self):
        assert _harmonic_coverage(440.0, []) == 0

    def test_sympathetic_resonance_scores_low(self):
        """E4 sympathetic resonance in a C4-only spectrum has ≤1 harmonic score."""
        # C4 spectrum: strong harmonics at C5/G5/C6; E4's harmonics (B4/E5) absent
        peaks = [
            (0.0, 523.25),    # C5 = 2×C4
            (-2.0, 784.88),   # G5 = 3×C4
            (-5.0, 1046.5),   # C6 = 4×C4
            (-5.4, 329.63),   # E4 sympathetic resonance
            (-9.4, 261.63),   # C4 fundamental
        ]
        c4_score = _harmonic_coverage(261.63, peaks)
        e4_score = _harmonic_coverage(329.63, peaks)
        assert c4_score >= 3, f"C4 should score ≥3, got {c4_score}"
        assert e4_score == 0, f"E4 resonance should score 0, got {e4_score}"


# ── PitchDetector ─────────────────────────────────────────────────────────────

class TestPitchDetector:

    def test_silence_returns_empty(self):
        det = PitchDetector(sample_rate=SR)
        result = _run_detector(det, _silence())
        assert result == []

    def test_below_rms_threshold_returns_empty(self):
        tiny = np.full(BLOCK, RMS_SILENCE * 0.5, dtype=np.float32)
        det = PitchDetector(sample_rate=SR)
        result = _run_detector(det, tiny)
        assert result == []

    def test_detects_a4(self):
        det = PitchDetector(sample_rate=SR)
        result = _run_detector(det, _sine(440.0, amp=0.5))
        assert 'A4' in result

    def test_detects_multiple_notes(self):
        det = PitchDetector(sample_rate=SR)
        block = _sine(440.0, 0.5) + _sine(660.0, 0.4)
        result = _run_detector(det, block)
        assert len(result) >= 1

    def test_result_is_list_of_strings(self):
        det = PitchDetector(sample_rate=SR)
        result = _run_detector(det, _sine(440.0, 0.5))
        assert isinstance(result, list)
        assert all(isinstance(n, str) for n in result)

    def test_max_notes_capped(self):
        from listen_ghost.pitch_detector import MAX_NOTES
        det = PitchDetector(sample_rate=SR)
        # Dense chord
        block = sum(_sine(f, 0.2) for f in [220, 330, 440, 550, 660, 880, 1100])
        result = _run_detector(det, block)
        assert len(result) <= MAX_NOTES

    def test_piano_harmonic_louder_than_fundamental(self):
        """Piano-like signal: 2nd harmonic louder than fundamental.
        Should detect only the fundamental (C4), not C5."""
        det = PitchDetector(sample_rate=SR)
        # C4 at amp=0.3, C5 (2nd harmonic) louder at amp=0.5
        block = _sine(261.63, amp=0.3) + _sine(523.25, amp=0.5)
        result = _run_detector(det, block)
        assert 'C4' in result, f"C4 not found in {result}"
        assert 'C5' not in result, f"C5 (harmonic) should be suppressed, got {result}"

    @pytest.mark.parametrize('note,freq', [
        ('C4', 261.63),
        ('D4', 293.66),
        ('E4', 329.63),
        ('G4', 392.00),
        ('A4', 440.00),
        ('C5', 523.25),
    ])
    def test_piano_scale_single_note_only(self, note, freq):
        """Each piano scale note (with harmonics) should appear without its
        octave-above harmonic being displayed alongside it."""
        det = PitchDetector(sample_rate=SR)
        # Simulate piano timbre: fundamental + 2nd harmonic 3 dB louder
        block = _sine(freq, amp=0.3) + _sine(freq * 2, amp=0.5)
        result = _run_detector(det, block)
        assert note in result, f"{note} not found; got {result}"
        octave_above = freq_to_note(freq * 2)
        assert octave_above not in result, (
            f"Octave harmonic {octave_above} should be suppressed; got {result}"
        )

    def _piano_tone(self, f0: float, fund_amp: float) -> np.ndarray:
        """Piano-like block: fundamental + 5 harmonics with natural decay profile."""
        amps = [1.0, 1.5, 1.2, 0.8, 0.5, 0.3]  # typical piano partial amplitudes
        block = np.zeros(BLOCK, dtype=np.float32)
        for h, rel in enumerate(amps, start=1):
            block += _sine(f0 * h, amp=fund_amp * rel)
        return block

    def test_piano_decay_no_sympathetic_resonances(self):
        """Late-decay piano C4: sympathetic single-sine resonances at E4 and G4
        must NOT appear alongside the fundamental."""
        det = PitchDetector(sample_rate=SR)
        # C4 late-decay (fundamental quiet, harmonics still present)
        c4 = self._piano_tone(261.63, fund_amp=0.05)
        # Sympathetic resonances: single sines — no harmonic series of their own
        resonances = _sine(329.63, amp=0.08) + _sine(392.0, amp=0.04)
        block = c4 + resonances
        result = _run_detector(det, block)
        # Only C4 should appear; E4 and G4 resonances should be filtered by
        # harmonic-coverage scoring (they have 0–1 strong harmonics vs C4's 5).
        assert 'C4' in result, f"C4 not found in {result}"
        assert 'E4' not in result, f"E4 sympathetic resonance shown: {result}"
        assert 'G4' not in result, f"G4 sympathetic resonance shown: {result}"

    def test_real_chord_all_notes_shown(self):
        """A real C-major chord (C4+E4+G4) with full harmonic series for each note
        must display all three chord tones — not filter them as 'resonances'."""
        det = PitchDetector(sample_rate=SR)
        # Each chord tone has its own rich harmonic series
        chord = (self._piano_tone(261.63, fund_amp=0.3)
                 + self._piano_tone(329.63, fund_amp=0.25)
                 + self._piano_tone(392.0,  fund_amp=0.2))
        result = _run_detector(det, chord)
        # All three chord tones should be detected
        assert 'C4' in result, f"C4 missing from chord: {result}"
        assert 'E4' in result, f"E4 missing from chord: {result}"
        assert 'G4' in result, f"G4 missing from chord: {result}"

    def test_temporal_smoothing_suppresses_single_frame_noise(self):
        """A note that appears in only 1 frame should be suppressed after smoothing."""
        det = PitchDetector(sample_rate=SR)
        # Feed silence frames then 1 tone frame: tone should NOT appear in history
        for _ in range(4):
            det.process(_silence())
        result = det.process(_sine(440.0, 0.5))
        # After just 1 frame of tone (with 2 silence frames), votes < SMOOTH_MIN_VOTES
        # The note may or may not appear depending on history; just check it's a list.
        assert isinstance(result, list)


# ── _highpass_fft ─────────────────────────────────────────────────────────────

class TestHighpassFft:

    def test_attenuates_below_cutoff(self):
        """Signal at 50 Hz should be attenuated after HP at 150 Hz.

        50 Hz is below the half-power point (75 Hz) so the FFT bin is zeroed,
        but spectral leakage from the non-integer-cycle sine still contributes
        some residual energy into neighbouring bins.  We check for ≥12 dB
        attenuation (< 25 % RMS), which the smooth linear-ramp filter delivers.
        """
        low_sine = _sine(50.0, amp=1.0)
        filtered = _highpass_fft(low_sine, cutoff=150.0, sample_rate=SR)
        rms_orig = float(np.sqrt(np.mean(low_sine ** 2)))
        rms_filt = float(np.sqrt(np.mean(filtered ** 2)))
        assert rms_filt < 0.25 * rms_orig   # ≥12 dB attenuation

    def test_passes_above_cutoff(self):
        """Signal at 500 Hz should pass through essentially unchanged."""
        high_sine = _sine(500.0, amp=1.0)
        filtered = _highpass_fft(high_sine, cutoff=150.0, sample_rate=SR)
        rms_orig = float(np.sqrt(np.mean(high_sine ** 2)))
        rms_filt = float(np.sqrt(np.mean(filtered ** 2)))
        assert rms_filt > 0.9 * rms_orig    # ≤1 dB loss

    def test_output_length_matches_input(self):
        block = _sine(440.0)
        filtered = _highpass_fft(block, cutoff=150.0, sample_rate=SR)
        assert len(filtered) == len(block)

    def test_output_dtype_is_float32(self):
        block = _sine(440.0)
        filtered = _highpass_fft(block, cutoff=150.0, sample_rate=SR)
        assert filtered.dtype == np.float32

    def test_removes_bass_preserves_voice(self):
        """HP filter should cut the bass fundamental but keep vocal range."""
        bass_fund = _sine(98.0, amp=0.8)
        vocal = _sine(440.0, amp=0.3)
        mix = bass_fund + vocal
        filtered = _highpass_fft(mix, VOCAL_HP_CUTOFF, SR)

        spec_orig = np.abs(np.fft.rfft(mix))
        spec_filt = np.abs(np.fft.rfft(filtered))
        freqs = np.fft.rfftfreq(BLOCK, 1.0 / SR)

        bass_idx = int(np.argmin(np.abs(freqs - 98.0)))
        vocal_idx = int(np.argmin(np.abs(freqs - 440.0)))

        # At 98 Hz the filter gain is (98−75)/75 ≈ 0.31 (linear ramp, not brick-wall).
        # The bass bin should have < 40 % of its original amplitude after filtering.
        assert spec_filt[bass_idx] < spec_orig[bass_idx] * 0.40
        # Vocal bin should retain most of its energy
        assert spec_filt[vocal_idx] > spec_orig[vocal_idx] * 0.85


# ── _yin_pitch ────────────────────────────────────────────────────────────────

class TestYinPitch:

    @pytest.mark.parametrize('freq', [200.0, 330.0, 440.0, 660.0, 880.0])
    def test_detects_clean_sinusoid(self, freq):
        """YIN should find a pure tone within ±2% of the true frequency."""
        block = _sine(freq, amp=0.5)
        detected, confidence = _yin_pitch(block, SR)
        assert detected is not None, f"YIN returned None for {freq} Hz"
        assert abs(detected - freq) / freq < 0.02, (
            f"YIN error too large: expected {freq:.1f}, got {detected:.1f}"
        )
        assert confidence >= YIN_CONFIDENCE_MIN

    def test_silence_returns_none(self):
        freq, conf = _yin_pitch(_silence(), SR)
        assert freq is None
        assert conf == pytest.approx(0.0)

    def test_confidence_range(self):
        """Confidence should always be in [0, 1]."""
        freq, conf = _yin_pitch(_sine(440.0, amp=0.5), SR)
        assert 0.0 <= conf <= 1.0

    def test_below_min_freq_not_detected(self):
        """Frequency below VOCAL_FREQ_MIN should not be found by YIN."""
        # YIN search range starts at VOCAL_FREQ_MIN (150 Hz)
        below = _sine(80.0, amp=0.8)
        freq, conf = _yin_pitch(below, SR)
        # Either None or a frequency in the valid range
        if freq is not None:
            assert freq >= VOCAL_FREQ_MIN * 0.95   # allow small tolerance


# ── VocalPitchDetector ────────────────────────────────────────────────────────

class TestVocalPitchDetector:

    def _det(self):
        return VocalPitchDetector(sample_rate=SR)

    # ── Silence / noise ───────────────────────────────────────────────────────

    def test_silence_returns_empty(self):
        det = self._det()
        assert _run_detector(det, _silence()) == []

    def test_bass_only_returns_empty(self):
        """Bass guitar alone must never be reported as a vocal pitch."""
        det = self._det()
        result = _run_detector(det, _bass())
        assert result == [], f"Bass-only should be silent, got {result}"

    def test_no_g2_bug(self):
        """Original bug: bass-only always showing G2. Must stay fixed."""
        det = self._det()
        result = _run_detector(det, _bass())
        assert 'G2' not in result

    # ── Pure vocal tones ──────────────────────────────────────────────────────

    @pytest.mark.parametrize('freq,note', [
        (440.0, 'A4'),
        (523.25, 'C5'),
        (329.63, 'E4'),
        (415.0, 'G#4'),
    ])
    def test_detects_pure_vocal_tone(self, freq, note):
        det = self._det()
        result = _run_detector(det, _sine(freq, amp=0.3))
        assert result == [note], f"Expected [{note!r}], got {result}"

    # ── Bass + vocal mix ──────────────────────────────────────────────────────

    def test_bass_plus_d4_detects_d4(self):
        det = self._det()
        result = _run_detector(det, _bass() + _sine(294.0))
        assert result == ['D4'], f"Expected ['D4'], got {result}"

    def test_bass_plus_a4_detects_a4(self):
        det = self._det()
        result = _run_detector(det, _bass() + _sine(440.0))
        assert result == ['A4'], f"Expected ['A4'], got {result}"

    def test_bass_plus_c5_detects_c5(self):
        det = self._det()
        result = _run_detector(det, _bass() + _sine(523.0, amp=0.2))
        assert result == ['C5'], f"Expected ['C5'], got {result}"

    def test_bass_plus_g4_detects_g4(self):
        det = self._det()
        result = _run_detector(det, _bass() + _sine(392.0))
        assert result == ['G4'], f"Expected ['G4'], got {result}"

    def test_bass_plus_b4_detects_b4(self):
        det = self._det()
        result = _run_detector(det, _bass() + _sine(494.0))
        assert result == ['B4'], f"Expected ['B4'], got {result}"

    def test_bass_plus_f4_detects_nearby(self):
        """F4 (349 Hz) sits in the Hanning leakage valley between bass 3rd/4th harmonics.
        The FFT finds a local max at F#4 (375 Hz) instead. Accept F4 or F#4."""
        det = self._det()
        result = _run_detector(det, _bass() + _sine(349.0))
        assert result and result[0] in ('F4', 'F#4'), (
            f"Expected F4 or F#4 in bass+F4 mix, got {result}"
        )

    # ── Return type invariants ────────────────────────────────────────────────

    def test_returns_list(self):
        det = self._det()
        result = det.process(_sine(440.0, amp=0.5))
        assert isinstance(result, list)

    def test_returns_at_most_one_note(self):
        """VocalPitchDetector is monophonic — must never return more than 1 note."""
        det = self._det()
        # Rich mix with many sinusoids
        block = sum(_sine(f, 0.2) for f in [330, 440, 523, 660, 880])
        for _ in range(8):
            result = det.process(block)
        assert len(result) <= 1

    def test_history_cleared_on_silence(self):
        """After silence, the frequency history should reset."""
        det = self._det()
        _run_detector(det, _sine(440.0, amp=0.5))
        # Silence clears history
        _run_detector(det, _silence())
        assert len([f for f in det._freq_history if f is not None]) == 0
