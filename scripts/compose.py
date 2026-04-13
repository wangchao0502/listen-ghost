#!/usr/bin/env python3
"""
乐谱合成器 — 将乐谱数据结构渲染为钢琴 WAV 音频文件

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
数据结构示例
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    score = {
        "tempo":    100,                      # BPM（每分钟拍数）
        "output":   "audios/output/demo.wav", # 输出路径（省略则用默认值）
        "sequence": [                          # 音符/和弦序列
            ("Cmaj", 4),   # (名称, 拍数)  — 和弦，4拍
            ("Am",   4),
            ("Dm",   4),
            ("Gmaj", 4),
            ("C5",   2),   # 单音，2拍
            ("G4",   2),
        ]
    }

名称规则
────────
  单音：  音名 + 八度数，例如  C4  A#3  Bb5  F#4
  和弦：  见下方 CHORD_VOICINGS，例如  Cmaj  Am  G7  Cmaj7  Dm7

  优先级：若名称同时存在于 CHORD_VOICINGS 和音名模式（如 G7），
          一律按和弦处理。

缓存机制
────────
  audios/single/<Note>.wav  和  audios/chord/<Chord>.wav
  是默认时长（2s）的参考缓存。合成器渲染时按乐谱精确时长
  重新合成（ADSR 自适应），同时确保缓存文件存在，缺失时
  自动调用 gen_test_audio 生成。

用法
────
  python scripts/compose.py               # 渲染内置 EXAMPLE_SCORE
  python scripts/compose.py my_score.py   # 渲染外部乐谱文件中的 score 变量

  外部文件格式：定义一个名为 score 的 dict 变量即可，无需其他内容。
"""

import re
import sys
from pathlib import Path

import numpy as np

# ── 将 scripts/ 加入搜索路径，以便导入 gen_test_audio ─────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from gen_test_audio import (          # noqa: E402
    mix_chord as _gen_mix_chord,
    note_to_freq,
    OUTPUT_DIR as _AUDIO_CACHE,
    SAMPLE_RATE,
    save_wav,
    synth_piano,
)

# ══════════════════════════════════════════════════════════════════════
# 和弦音组定义
# 根音放在低音区，覆盖常用三和弦、七和弦、挂留和弦
# ══════════════════════════════════════════════════════════════════════

CHORD_VOICINGS: dict[str, list[str]] = {
    # ── 大三和弦 ──────────────────────────────────────────────────────
    "Cmaj":  ["C4", "E4", "G4"],
    "Dbmaj": ["Db4", "F4", "Ab4"],
    "Dmaj":  ["D4", "F#4", "A4"],
    "Ebmaj": ["Eb4", "G4", "Bb4"],
    "Emaj":  ["E3", "G#3", "B3"],
    "Fmaj":  ["F3", "A3", "C4"],
    "F#maj": ["F#3", "A#3", "C#4"],
    "Gmaj":  ["G3", "B3", "D4"],
    "Abmaj": ["Ab3", "C4", "Eb4"],
    "Amaj":  ["A3", "C#4", "E4"],
    "Bbmaj": ["Bb3", "D4", "F4"],
    "Bmaj":  ["B3", "D#4", "F#4"],
    # ── 小三和弦 ──────────────────────────────────────────────────────
    "Am":    ["A3", "C4", "E4"],
    "Bm":    ["B3", "D4", "F#4"],
    "Cm":    ["C4", "Eb4", "G4"],
    "Dm":    ["D4", "F4", "A4"],
    "Em":    ["E3", "G3", "B3"],
    "Fm":    ["F3", "Ab3", "C4"],
    "Gm":    ["G3", "Bb3", "D4"],
    "F#m":   ["F#3", "A3", "C#4"],
    "C#m":   ["C#4", "E4", "G#4"],
    # ── 大七和弦 ──────────────────────────────────────────────────────
    "Cmaj7":  ["C4", "E4", "G4", "B4"],
    "Fmaj7":  ["F3", "A3", "C4", "E4"],
    "Gmaj7":  ["G3", "B3", "D4", "F#4"],
    "Amaj7":  ["A3", "C#4", "E4", "G#4"],
    "Bbmaj7": ["Bb3", "D4", "F4", "A4"],
    "Dmaj7":  ["D4", "F#4", "A4", "C#5"],
    # ── 属七和弦 ──────────────────────────────────────────────────────
    "G7":    ["G3", "B3", "D4", "F4"],
    "D7":    ["D4", "F#4", "A4", "C5"],
    "A7":    ["A3", "C#4", "E4", "G4"],
    "E7":    ["E3", "G#3", "B3", "D4"],
    "C7":    ["C4", "E4", "G4", "Bb4"],
    "F7":    ["F3", "A3", "C4", "Eb4"],
    "B7":    ["B3", "D#4", "F#4", "A4"],
    # ── 小七和弦 ──────────────────────────────────────────────────────
    "Am7":   ["A3", "C4", "E4", "G4"],
    "Bm7":   ["B3", "D4", "F#4", "A4"],
    "Cm7":   ["C4", "Eb4", "G4", "Bb4"],
    "Dm7":   ["D4", "F4", "A4", "C5"],
    "Em7":   ["E3", "G3", "B3", "D4"],
    "Gm7":   ["G3", "Bb3", "D4", "F4"],
    "F#m7":  ["F#3", "A3", "C#4", "E4"],
    # ── 挂留和弦 ──────────────────────────────────────────────────────
    "Csus2": ["C4", "D4", "G4"],
    "Csus4": ["C4", "F4", "G4"],
    "Gsus4": ["G3", "C4", "D4"],
    "Dsus4": ["D4", "G4", "A4"],
}

# ── 单音识别正则（音名字母 + 可选升降号 + 八度数字）──────────────────
_NOTE_RE = re.compile(r"^[A-G][#b]?\d+$")


# ══════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════

def _classify(name: str) -> str:
    """
    返回 'chord' 或 'note'。
    先查 CHORD_VOICINGS，再尝试正则（避免 G7 等歧义被误判为单音）。
    """
    if name in CHORD_VOICINGS:
        return "chord"
    if _NOTE_RE.match(name):
        return "note"
    raise ValueError(
        f"无法识别 {name!r}：既不在 CHORD_VOICINGS 中，也不符合音名格式（如 C4、A#3）"
    )


def _ensure_cached(name: str, kind: str) -> None:
    """
    检查 audios/single/ 或 audios/chord/ 中的默认缓存文件；
    缺失时调用 gen_test_audio 函数生成（默认 2s）。
    """
    if kind == "note":
        path = _AUDIO_CACHE / "single" / f"{name}.wav"
        if not path.exists():
            print(f"    [缓存] 生成单音 {name} → {path.relative_to(_AUDIO_CACHE.parent)}")
            save_wav(synth_piano(note_to_freq(name)), path)
    else:
        path = _AUDIO_CACHE / "chord" / f"{name}.wav"
        if not path.exists():
            print(f"    [缓存] 生成和弦 {name} → {path.relative_to(_AUDIO_CACHE.parent)}")
            save_wav(_gen_mix_chord(CHORD_VOICINGS[name]), path)


def _synth_segment(name: str, kind: str, duration: float) -> np.ndarray:
    """按精确时长合成单音或和弦（ADSR 随时长自适应，不受缓存文件约束）。"""
    if kind == "note":
        return synth_piano(note_to_freq(name), duration=duration)
    return _gen_mix_chord(CHORD_VOICINGS[name], duration=duration)


# ══════════════════════════════════════════════════════════════════════
# 主渲染函数
# ══════════════════════════════════════════════════════════════════════

def render_score(score: dict) -> np.ndarray:
    """
    将乐谱渲染为 numpy 浮点信号（-1.0 ~ 1.0）。

    Parameters
    ----------
    score : dict
        必须包含 "tempo"（int/float）和 "sequence"（list of (name, beats)）。

    Returns
    -------
    np.ndarray  shape=(N,)  dtype=float64
    """
    tempo    = score["tempo"]
    sequence = score["sequence"]
    beat_dur = 60.0 / tempo

    total_beats = sum(b for _, b in sequence)
    print(f"  速度 {tempo} BPM  |  每拍 {beat_dur:.3f}s  "
          f"|  共 {total_beats} 拍  {total_beats * beat_dur:.2f}s\n")

    segments: list[np.ndarray] = []
    for idx, (name, beats) in enumerate(sequence, 1):
        dur  = beats * beat_dur
        kind = _classify(name)
        _ensure_cached(name, kind)
        seg  = _synth_segment(name, kind, dur)
        segments.append(seg)

        freq_hint = ""
        if kind == "note":
            freq_hint = f"  {note_to_freq(name):.1f} Hz"
        else:
            notes_str = " + ".join(CHORD_VOICINGS[name])
            freq_hint = f"  [{notes_str}]"
        print(f"  [{idx:02d}] {name:<8}  {beats:>4} 拍  {dur:.3f}s{freq_hint}")

    combined = np.concatenate(segments)
    peak = np.max(np.abs(combined))
    if peak > 0:
        combined = combined / peak * 0.92
    return combined


def render_to_file(score: dict) -> Path:
    """渲染乐谱并保存为 WAV，返回输出文件路径。"""
    out = Path(score.get("output", "audios/output/score.wav"))
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"═══ 渲染乐谱 → {out} ═══\n")
    sig = render_score(score)
    save_wav(sig, out)

    dur = len(sig) / SAMPLE_RATE
    print(f"\n  [OK] 输出: {out.resolve()}")
    print(f"       时长: {dur:.2f}s  |  {len(sig)} 帧  |  {SAMPLE_RATE} Hz  |  16-bit mono WAV")
    return out


# ══════════════════════════════════════════════════════════════════════
# 示例乐谱（直接运行时使用）
# ══════════════════════════════════════════════════════════════════════

EXAMPLE_SCORE: dict = {
    "tempo":    100,
    "output":   "audios/output/example.wav",
    "sequence": [
        ("Cmaj", 4),
        ("Am",   4),
        ("Dm",   4),
        ("Gmaj", 4),
        ("C5",   4),
    ],
}

# ─── 自定义乐谱示例（取消注释后替换 EXAMPLE_SCORE 即可） ───────────
# MY_SCORE = {
#     "tempo":  80,
#     "output": "audios/output/my_piece.wav",
#     "sequence": [
#         ("Cmaj7", 4),
#         ("Am7",   4),
#         ("Fmaj7", 4),
#         ("G7",    4),
#         ("C5",    2),
#         ("G4",    2),
#         ("E4",    4),
#     ],
# }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # 加载外部乐谱文件中的 score 变量
        import importlib.util
        spec = importlib.util.spec_from_file_location("_user_score", sys.argv[1])
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        target = getattr(mod, "score", None)
        if target is None:
            print(f"错误：{sys.argv[1]} 中未找到名为 'score' 的变量", file=sys.stderr)
            sys.exit(1)
    else:
        target = EXAMPLE_SCORE

    render_to_file(target)
