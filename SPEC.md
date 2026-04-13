# listen-ghost 技术规格说明

## 功能概述

Windows 桌面小程序，监听声卡输出（WASAPI Loopback），实时解析并展示当前播放音乐中的音高。
适用于观察演唱者音高、乐器调音等场景。

## 界面规格

- 窗口尺寸：400 × 400 px，固定，不可缩放
- 位置：桌面右下角（使用 ctypes SPI_GETWORKAREA 获取真实工作区）
- 始终置顶（always-on-top）
- 深色主题：背景 `#1a1a2e`

```
┌─────────────────────────────┐
│  [       START / STOP      ]│  44px 按钮，深蓝紫色
│  场景： [通用]  [人声]        │  场景切换按钮行
├─────────────────────────────┤
│  设备名（状态提示）           │  小字，灰色
│                             │
│     D4   F#5   A3           │  36pt Consolas 粗体，蓝白色
│                             │  多音以空格分隔，自动换行
│  ▁▃▇▅▂▁▁▄▆▃▁▁▁            │  80px 频谱 Canvas
└─────────────────────────────┘
```

## 技术栈

| 组件       | 选型                                          |
|------------|-----------------------------------------------|
| 语言       | Python 3.12+                                  |
| 音频捕获   | soundcard 0.4.x（Windows Media Foundation）   |
| 信号处理   | numpy 2.x（FFT + YIN + 峰值检测）             |
| GUI        | tkinter（Python 内置）                        |
| 打包       | PyInstaller 6.x（可选）                       |

## 文件结构

```
listen-ghost/
├── SPEC.md
├── CLAUDE.md                        # AI 协作指南
├── main.py                          # 入口：DPI 设置 + 自动补丁 + 依赖检查 + 启动
├── requirements.txt
├── build.spec                       # PyInstaller 打包配置
├── audios/
│   ├── single/                      # 单音缓存（由 gen_test_audio.py 生成）
│   ├── chord/                       # 和弦缓存（由 gen_test_audio.py 生成）
│   └── output/                      # compose.py 渲染输出
├── scripts/
│   ├── gen_test_audio.py            # 加法合成钢琴音频生成器（单音 + 和弦缓存）
│   └── compose.py                   # 乐谱渲染器（乐谱数据结构 → WAV）
├── tests/
│   ├── test_pitch_detector.py       # 音高检测单元测试（86 个）
│   └── test_threading_bridge.py     # 队列单元测试
└── listen_ghost/
    ├── __init__.py
    ├── audio_capture.py             # WASAPI Loopback 捕获（soundcard）
    ├── pitch_detector.py            # FFT 通用 + YIN 人声检测 + 频率→音名转换
    ├── threading_bridge.py          # 非阻塞队列（录音线程→UI线程）
    └── app.py                       # tkinter UI + 窗口定位 + 轮询循环
```

## 场景系统

### 通用场景（SCENE_GENERAL）

多音 FFT 检测，适用于乐器、和声等任意有音高的声音。

### 人声场景（SCENE_VOCAL）

单音 YIN 检测，针对流行音乐混音中演唱者音高优化。

场景切换：
- UI 中点击「通用」/「人声」按钮即时切换
- 捕获运行中切换时，`_make_detector()` 热替换检测器实例
- CPython GIL 保证赋值原子性，无需显式锁

## 核心架构

### 线程模型

```
录音线程（daemon thread，soundcard 阻塞式 API）
  CoInitializeEx(COINIT_MULTITHREADED)   ← COM 必须在本线程初始化
  recorder.record(numframes=2048)        ← 阻塞直到 2048 帧就绪 (~43ms)
    → 双声道混单声道 float32
    → detector.process(block)            ← PitchDetector 或 VocalPitchDetector
    → AudioQueue.put_nowait(result)      ← 满则丢弃，绝不阻塞

        ↓  queue.Queue(maxsize=10)

主线程（tkinter 事件循环）
  root.after(33ms, _poll_queue)          ← ~30fps 轮询
    → 取最新结果 → 更新 Label + Canvas
```

### WASAPI Loopback 设备获取

```python
def find_loopback_device():
    # 1. sc.default_speaker() 获取当前默认输出设备
    # 2. sc.get_microphone(id=speaker.name, include_loopback=True)
    # 3. 失败则遍历 sc.all_microphones(include_loopback=True) 取第一个 isloopback=True
```

### FFT 音高检测（通用场景）

```python
class PitchDetector:
    def process(block) -> List[str]:
        # 1. RMS 静音门控：rms < 1e-4 → 返回 []
        # 2. Hann 窗 + rfft（零填充 8192 点，5.4Hz/bin 分辨率 @ 48kHz）
        # 3. 转 dB（相对峰值），限制 80Hz–2000Hz 频带
        # 4. 找局部极大值（>= DB_THRESHOLD = -30dB）
        # 5. suppress_harmonics：按频率升序，去除任意低频的整数倍峰（±5% 容差）
        #    → 无论哪个更响，始终保留低频基频、抑制高频泛音
        # 6. harmonic_coverage 过滤：统计每个幸存基频的强泛音数（>= -20dB）
        #    → 最高分 ≥2 时，只保留得分 ≥ max_score-2 的音
        #    → 过滤同情共振弦（0-1 个泛音）；和弦各音（4+ 个泛音）全部保留
        # 7. filter_close_notes：去除相距 <3 半音的较弱音
        # 8. freq_to_note：频率 → MIDI → 音名 + 八度
        # 9. 5帧多数投票平滑（连续出现 ≥3 帧才显示）
        # 10. 返回最多 MAX_NOTES=6 个音名，按响度降序
```

### YIN 音高检测（人声场景）

基于 de Cheveigné & Kawahara (2002) 的 YIN 算法，专为单音基频估计设计，对失真和缺基频的人声具有良好鲁棒性。

**检测流水线：**

```
1. 静音门控（RMS < 1e-4 → 清空历史，返回 []）
2. 高通滤波（FFT 零相位 HP，截止 150 Hz）
   → 消除贝斯/踢鼓基频能量，防止其主导自相关
3. YIN 自相关（搜索范围：150–1100 Hz）
   a. 差函数 d(τ) = Σ (x[j]−x[j+τ])²
   b. CMNDF：d′(τ) = d(τ)·τ / Σ_{j=1}^{τ} d(j)
   c. 阈值搜索：首个 d′(τ) < 0.15 的局部极小值
   d. 抛物线插值：亚采样精度
   e. 置信度 = 1 − CMNDF(τ_est)，< 0.55 则进入回退
4. FFT 回退（200–700 Hz，置信度不足时使用）
   → 在高通滤波后的频谱中找最强局部极大值
   → 次谐波抑制：仅当 f/d（d=2..7）在 ±0.75 零填充 bin（≈4.4 Hz）
     内落在实际贝斯谐波上，且贝斯谐波能量 > 2× 候选能量时，才拒绝
5. 中位数平滑（最近 5 帧有效频率的中位数）
   → 需至少 2 帧有效才输出，滑音平滑过渡
```

**关键常量：**

**通用模式关键常量：**

| 常量 | 值 | 说明 |
|------|----|------|
| `DB_THRESHOLD` | -30 dB | 峰值候选最低电平（相对帧峰值） |
| `HARMONIC_TOL` | 0.05 | 谐波频率匹配容差 ±5% |
| `HARMONIC_SCORE_MIN_DB` | -20 dB | 计入谐波覆盖度的最低电平 |
| `SMOOTH_FRAMES` | 5 | 多数投票平滑窗口帧数 |
| `SMOOTH_MIN_VOTES` | 3 | 显示所需最少出票帧数 |
| `MAX_NOTES` | 6 | 最多同时显示音符数 |

**人声模式关键常量：**

| 常量 | 值 | 说明 |
|------|----|------|
| `VOCAL_FREQ_MIN` | 150 Hz | YIN 搜索下限（排除贝斯基频） |
| `VOCAL_FREQ_MAX` | 1100 Hz | YIN 搜索上限（高至女高音 C6） |
| `VOCAL_HP_CUTOFF` | 150 Hz | 高通滤波截止频率 |
| `YIN_THRESHOLD` | 0.15 | CMNDF 阈值，越低越严格 |
| `YIN_CONFIDENCE_MIN` | 0.55 | 置信度下限，低于此值转 FFT 回退 |
| `VOCAL_FFT_MIN` | 200 Hz | FFT 回退搜索下限 |
| `VOCAL_FFT_MAX` | 700 Hz | FFT 回退搜索上限 |
| `VOCAL_SMOOTH_FRAMES` | 5 | 中位数平滑窗口帧数 |

### 窗口定位

```python
# ctypes SPI_GETWORKAREA 获取真实工作区（排除任务栏，适配任意任务栏高度）
ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
x = rect.right  - WINDOW_W - PADDING
y = rect.bottom - WINDOW_H - PADDING
```

## 异常处理

| 情况                          | 处理方式                                         |
|-------------------------------|--------------------------------------------------|
| 无 Loopback 设备              | 状态栏提示，不崩溃                               |
| COM 未初始化                  | 录音线程头部调用 CoInitializeEx，finally CoUninitialize |
| soundcard + numpy 2.x 不兼容 | main.py 启动时自动将 fromstring 补丁为 frombuffer |
| 音名闪烁（通用）              | 3帧多数投票平滑                                  |
| 音名抖动（人声）              | 5帧中位数平滑                                    |
| DPI 缩放                      | SetProcessDpiAwareness(2)，回退 SetProcessDPIAware |
| 依赖缺失                      | 弹窗提示 pip install 命令                        |
| 未捕获异常                    | 写入 listen-ghost.log + 弹窗显示末尾 800 字符    |

## 运行方式

```bash
pip install soundcard numpy
python main.py
```

## 运行测试

```bash
python -m pytest tests/ -v
```

## 打包为 .exe

```bash
pip install pyinstaller
pyinstaller build.spec
# 输出：dist/listen-ghost.exe
```

## 验证步骤

1. `python main.py` — 窗口出现在桌面右下角
2. 播放 `audios/11.mp3`，点击 START，选择「人声」场景，观察演唱者音高实时更新
3. 停止播放后音名变为 `·`
4. 切换到「通用」场景，可看到多个和声音符同时显示
5. 日志写入 `listen-ghost.log`

## 已知限制

- **人声场景**：无源分离能力，在人声与贝斯频率直接重叠（如演唱者唱 D4 而贝斯正好在 G2 的 3 倍频处）时，可能漏检。
- **F4 附近频率**：349 Hz 落在贝斯三、四次谐波之间的 Hanning 泄漏谷，FFT 回退的频率估计可能偏移约半音（显示 F#4 而非 F4）。
- **通用模式高音区**（E5 以上）：FREQ_MAX=2000 Hz 内可容纳的泛音数量 ≤1，谐波覆盖度过滤进入 fallback 模式（显示所有幸存基频），同情共振抑制效果减弱。

## 开发历程

### 已解决的关键问题

| 问题 | 根因 | 解决方案 |
|------|------|----------|
| `WasapiSettings` 无 `loopback` 参数 | sounddevice 从未支持此参数 | 改用 `soundcard` 库，原生支持 WASAPI Loopback |
| `CO_E_NOTINITIALIZED (0x800401f0)` | Windows COM 未在录音线程初始化 | 线程启动时调 `CoInitializeEx(None, 0)` |
| `numpy.fromstring` 已移除 | soundcard 0.4.x 兼容 numpy 1.x，numpy 2.0 删除了二进制模式 | main.py 启动时自动检测并补丁 |
| 谐波被检测为独立音（通用） | FFT 对基频整数倍同样产生峰值 | `suppress_harmonics()`：按频率升序，±5% 容差过滤整数倍频，始终保留低频基频 |
| 泛音比基频响时基频丢失（通用） | 钢琴第 2 泛音（高八度）常比基频更响，旧响度优先方式把泛音当基频 | 改为频率升序处理，无论响度高低，整数倍高频一律抑制 |
| 钢琴衰减段（~1s 后）出现多余音（通用） | 同情共振弦随主音衰减变得相对更响，超过 -30 dB 阈值被误识 | `harmonic_coverage` 过滤：只保留有 ≥2 个强泛音（>-20 dB）的基频 |
| 相邻半音误检（通用） | FFT bin 间距在高频区接近半音宽度 | `filter_close_notes()`：3 半音最小间距 |
| 人声模式一直显示 G2 | `VOCAL_FREQ_MIN=80 Hz` 让 YIN 搜索范围覆盖贝斯基频（98 Hz），贝斯自相关置信度 0.998 压倒一切 | 提高 `VOCAL_FREQ_MIN` 至 150 Hz + 高通滤波 |
| A4/C5 等音符被误判为贝斯谐波 | 次谐波检查容差 ±35 Hz（1.5 × DFT bin）过宽，落入 Hanning 泄漏区，导致假阳性 | 收紧容差至 ±4.4 Hz（0.75 × 零填充 bin），仅匹配真实贝斯谐波的量化位置 |
