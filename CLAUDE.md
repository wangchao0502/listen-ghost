# CLAUDE.md — listen-ghost AI 协作指南

## 项目概述

Windows 桌面小程序，监听声卡输出（WASAPI Loopback），实时显示音高。
两种检测场景：通用（FFT 多音）和人声（YIN 单音）。

详细技术规格见 `SPEC.md`。

## 运行与测试

```bash
# 创建虚拟环境（项目根目录）
python -m venv .venv

# 激活虚拟环境（PowerShell）
.\.venv\Scripts\Activate.ps1

# 安装依赖
python -m pip install -r requirements.txt

# 运行程序
python main.py

# 运行测试（无需音频硬件）
python -m pytest tests/ -v

# 打包
pyinstaller build.spec
```

## 代码结构速查

| 文件 | 职责 |
|------|------|
| `main.py` | 入口：DPI、soundcard 补丁、依赖检查、日志 |
| `listen_ghost/audio_capture.py` | WASAPI Loopback 捕获，`AudioCapture` 类 |
| `listen_ghost/pitch_detector.py` | 全部检测逻辑：`PitchDetector`、`VocalPitchDetector`、辅助函数 |
| `listen_ghost/threading_bridge.py` | `AudioQueue`：音频线程→UI线程非阻塞队列 |
| `listen_ghost/app.py` | tkinter UI、场景切换、频谱可视化 |
| `tests/test_pitch_detector.py` | 86 个单元测试，覆盖全部检测路径 |
| `tests/test_threading_bridge.py` | `AudioQueue` 单元测试 |

## 重要设计决策

### 通用模式：谐波抑制方向

`_suppress_harmonics()` 按**频率升序**判断整数倍关系，始终保留低频（基频），抑制高频（泛音）——无论哪个更响。

**不要改回按响度降序处理。** 钢琴第 2 泛音（高八度）往往比基频更响，旧方式会把泛音当基频保留，导致同一音符重复显示。

### 通用模式：谐波覆盖度过滤

`_peaks_to_notes()` 在谐波抑制后，对每个幸存基频计算"强泛音数"（高于 `HARMONIC_SCORE_MIN_DB = -20 dB` 的整数倍频峰数量）。若最高分 ≥ 2，则只保留得分 ≥ `max_score − 2`（最低 1）的音。

**目的：** 消除钢琴衰减段（~1s 后）出现的同情共振（sympathetic resonance）。共振弦是被演奏音的泛音激励的单频振动，自身无泛音序列（得分 0–1），被过滤；真正和弦各音有完整谐波列（得分 4+），全部保留。

**不要把 `-20 dB` 阈值调低至 -30 dB。** 噪声峰会被误计为谐波，导致共振弦得分虚高，过滤失效。

### 人声模式次谐波检查容差

`pitch_detector.py` 的 `_fft_vocal_peak()` 中：

```python
harm_match_tol = 0.75 * (self.sample_rate / FFT_PAD)  # ≈4.4 Hz @ 48kHz
```

**不要改大此值。** 曾用过 `1.5 × DFT bin ≈ 35 Hz`，导致 A4、C5 等人声音符被误判为贝斯谐波而静音。4.4 Hz 是基于零填充 FFT 量化误差的数学上界推导出的最小可靠值。

### 人声模式频率下限

`VOCAL_FREQ_MIN = 150 Hz`（YIN 搜索下限）和 `VOCAL_HP_CUTOFF = 150 Hz`（高通截止）**必须保持一致**，且不能低于 150 Hz。

曾用 80 Hz，导致 YIN 以置信度 0.998 锁定贝斯基频（98 Hz），一直显示 G2。

### 无源分离

项目有意不引入 Spleeter/Demucs 等 ML 源分离库，保持纯 numpy + 规则的轻量架构。当人声频率与贝斯谐波完全重叠时，存在漏检属预期行为。

### 线程安全

音频回调（录音线程）调用 `_detector.process()`，检测器实例由主线程通过 `self._detector = ...` 热替换。CPython GIL 保证指针赋值原子性，不需要额外锁。`AudioQueue.put_nowait()` 满则丢弃，绝不阻塞录音线程。

## 修改须知

### 修改检测逻辑

- 任何修改需通过 `python -m pytest tests/ -v` 全部 86 个测试
- 人声模式回归测试集在 `TestVocalPitchDetector` 中，包含贝斯+人声混音场景
- 修改 `harm_match_tol` 前，阅读 `SPEC.md` 中「已知限制」和「已解决的关键问题」

### 修改 UI

- 窗口固定 400×400，不要尝试动态调整大小
- 频谱 Canvas 高度固定 80 px，`_build_spectrum_bars()` 预创建矩形，`_update_spectrum()` 只移动坐标
- 场景按钮状态由 `_refresh_scene_buttons()` 统一管理

### Windows 特定注意事项

- 录音线程必须在线程内调用 `CoInitializeEx`，不能在主线程调用后跨线程使用
- DPI 感知在 `main.py` 最早调用，在任何 tkinter 操作之前
- soundcard 的 `numpy.fromstring` 补丁在 `_patch_soundcard()` 中自动完成

## 测试约定

- 测试不依赖音频硬件，全部使用 numpy 合成信号
- 贝斯信号统一定义为 G2（98 Hz）+ 6 次谐波，振幅梯度 0.8/0.6/0.4/0.3/0.2/0.15
- `_run_detector(det, block, n_frames=8)`：连续送入 8 帧相同块，取最后结果（让平滑窗口 `SMOOTH_FRAMES=5` 稳定）
- F4（349 Hz）测试接受 `'F4'` 或 `'F#4'`（Hanning 泄漏谷频率偏移，属已知限制）
