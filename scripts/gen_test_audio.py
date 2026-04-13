#!/usr/bin/env python3
"""
钢琴测试音频生成器
使用加法合成（基频 + 7次谐波 + ADSR 包络）模拟钢琴音色

输出目录结构：
  audios/
    single/   C3.wav  D3.wav  ...  G5.wav  （12个单音）
    chord/    Cmaj.wav  Gmaj.wav  ...  G7.wav  （6个和弦）
"""

import numpy as np
import wave
from pathlib import Path

SAMPLE_RATE = 44100
DURATION    = 2.0   # 每个音总时长（秒）
RELEASE     = 0.45  # 释音时长（秒）
OUTPUT_DIR  = Path(__file__).parent.parent / "audios"

# ── 音名→半音偏移（支持升号 # 和降号 b）────────────────────────────
_SEMITONES: dict[str, int] = {
    'C': 0,  'C#': 1,  'Db': 1,
    'D': 2,  'D#': 3,  'Eb': 3,
    'E': 4,
    'F': 5,  'F#': 6,  'Gb': 6,
    'G': 7,  'G#': 8,  'Ab': 8,
    'A': 9,  'A#': 10, 'Bb': 10,
    'B': 11,
}


def note_to_freq(note: str) -> float:
    """将音名转为频率，例如 'A4' → 440.0 Hz，'C4' → 261.63 Hz。"""
    # 找到八度数字起始位置（支持负数八度，如 'C-1'）
    for i, ch in enumerate(note):
        if ch.isdigit() or (ch == '-' and i > 0):
            pitch_part = note[:i]
            octave = int(note[i:])
            break
    else:
        raise ValueError(f"无法解析音名: {note!r}")
    semitone = _SEMITONES[pitch_part]
    midi = (octave + 1) * 12 + semitone
    return 440.0 * 2 ** ((midi - 69) / 12)


# ── 钢琴谐波振幅比（基于真实钢琴频谱近似）──────────────────────────
_HARMONICS: list[tuple[int, float]] = [
    (1, 1.00),   # 基频
    (2, 0.60),   # 2次谐波（高八度）
    (3, 0.25),   # 3次
    (4, 0.14),   # 4次
    (5, 0.08),   # 5次
    (6, 0.05),   # 6次
    (7, 0.03),   # 7次
]


def _adsr_envelope(n: int, sr: int, release_n: int) -> np.ndarray:
    """
    构造 ADSR 包络：
      Attack  5ms → 1.0
      Decay   300ms → 0.55（Sustain level）
      Sustain 保持至释音前
      Release release_n 帧线性淡出至 0
    """
    env = np.ones(n, dtype=np.float64)
    atk = max(1, int(0.005 * sr))
    dec = int(0.30  * sr)
    sus = 0.55

    env[:atk] = np.linspace(0.0, 1.0, atk)
    end_dec = atk + dec
    if end_dec < n:
        env[atk:end_dec]  = np.linspace(1.0, sus, dec)
        env[end_dec:]     = sus

    if 0 < release_n <= n:
        env[-release_n:] *= np.linspace(1.0, 0.0, release_n)

    return env


def synth_piano(freq: float,
                duration: float = DURATION,
                sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """合成单个钢琴音（-1.0 ~ 1.0 浮点数组）。"""
    n = int(duration * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate

    sig = np.zeros(n, dtype=np.float64)
    for harmonic, amp in _HARMONICS:
        f = freq * harmonic
        if f < sample_rate / 2:           # 奈奎斯特上限
            sig += amp * np.sin(2 * np.pi * f * t)

    # 归一化后乘包络
    sig /= np.max(np.abs(sig))
    sig *= _adsr_envelope(n, sample_rate, int(RELEASE * sample_rate))
    return sig


def mix_chord(notes: list[str],
              duration: float = DURATION,
              sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """叠加多个音符合成和弦，归一化防止削波。"""
    combined = sum(synth_piano(note_to_freq(n), duration, sample_rate)
                   for n in notes)
    peak = np.max(np.abs(combined))
    if peak > 0:
        combined /= peak
    return combined * 0.90   # 留 6% 余量


def save_wav(sig: np.ndarray, path: Path, sample_rate: int = SAMPLE_RATE) -> None:
    """将浮点信号保存为 16-bit 单声道 WAV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (sig * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


# ── 生成目标 ──────────────────────────────────────────────────────

SINGLE_NOTES: list[str] = [
    'C3', 'D3', 'E3', 'G3', 'A3',
    'C4', 'E4', 'G4', 'A4',
    'C5', 'E5', 'G5',
]

CHORDS: dict[str, list[str]] = {
    'Cmaj':  ['C4', 'E4', 'G4'],
    'Gmaj':  ['G3', 'B3', 'D4'],
    'Am':    ['A3', 'C4', 'E4'],
    'Fmaj':  ['F3', 'A3', 'C4'],
    'Cmaj7': ['C4', 'E4', 'G4', 'B4'],
    'G7':    ['G3', 'B3', 'D4', 'F4'],
}


def main() -> None:
    single_dir = OUTPUT_DIR / "single"
    chord_dir  = OUTPUT_DIR / "chord"

    print(f"输出目录: {OUTPUT_DIR.resolve()}\n")

    # 单音
    print(f"{'── 单音':─<40}")
    for note in SINGLE_NOTES:
        freq = note_to_freq(note)
        sig  = synth_piano(freq)
        out  = single_dir / f"{note}.wav"
        save_wav(sig, out)
        print(f"  {note:<4}  {freq:7.2f} Hz  →  {out.name}")

    # 和弦
    print(f"\n{'── 和弦':─<40}")
    for name, notes in CHORDS.items():
        sig = mix_chord(notes)
        out = chord_dir / f"{name}.wav"
        save_wav(sig, out)
        parts = '  +  '.join(f"{n}({note_to_freq(n):.0f}Hz)" for n in notes)
        print(f"  {name:<6}  {parts}")
        print(f"         → {out.name}")

    total = len(SINGLE_NOTES) + len(CHORDS)
    print(f"\n完成！共生成 {len(SINGLE_NOTES)} 个单音 + {len(CHORDS)} 个和弦（{total} 个文件）")


if __name__ == '__main__':
    main()
