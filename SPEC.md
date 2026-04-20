# listen-ghost 规格说明

## Purpose

Windows 桌面小程序，监听声卡输出（WASAPI Loopback），实时解析并展示当前播放音乐中的音高。适用于观察演唱者音高、乐器调音等场景。

---

## 技术栈

| 组件     | 选型                                        |
|----------|---------------------------------------------|
| 语言     | Python 3.12+                                |
| 音频捕获 | soundcard 0.4.x（Windows Media Foundation） |
| 信号处理 | numpy 2.x（FFT + YIN + 峰值检测）           |
| GUI      | tkinter（Python 内置）                      |
| 打包     | PyInstaller 6.x（可选）                     |

---

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
    ├── threading_bridge.py          # 非阻塞队列（录音线程→UI 线程）
    └── app.py                       # tkinter UI + 窗口定位 + 轮询循环
```

---

## UI

### Requirement: 窗口布局
窗口尺寸 MUST 固定为 400 × 400 px，不可缩放，深色主题背景 `#1a1a2e`。

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

#### Scenario: 程序启动定位
- GIVEN 操作系统正在运行
- WHEN 用户执行 `python main.py`
- THEN 窗口出现在桌面右下角（通过 `SPI_GETWORKAREA` 获取真实工作区，适配任意任务栏高度）
- AND 窗口始终置顶（always-on-top）

#### Scenario: 高 DPI 显示器
- GIVEN 系统显示缩放比 > 100%
- WHEN 程序启动
- THEN 窗口尺寸和字体不被系统缩放扭曲（优先 `SetProcessDpiAwareness(2)`，失败时回退 `SetProcessDPIAware`）

### Requirement: 音符显示
无音高时 MUST 显示 `·`；检测到音高时 MUST 以 36pt Consolas 粗体、蓝白色显示音名（如 `D4 F#5 A3`），多音以空格分隔，最多显示 6 个。

#### Scenario: 静音状态
- GIVEN 捕获已启动
- WHEN 当前帧 RMS < 1e-4
- THEN 显示 `·`

#### Scenario: 检测到音高
- GIVEN 捕获已启动且音频中存在有音高的声音
- WHEN 检测器输出音名列表
- THEN 界面以空格分隔展示所有音名，最多 6 个

### Requirement: 频谱可视化
频谱 Canvas 高度 MUST 固定为 80 px；矩形数量在启动时预创建，每帧仅更新坐标，不重建对象。

#### Scenario: 实时频谱刷新
- GIVEN 捕获已启动
- WHEN 每帧音频处理完成
- THEN Canvas 以约 30fps 刷新，反映当前频谱能量分布

### Requirement: 场景按钮状态
场景按钮（「通用」/「人声」）的视觉激活状态 MUST 由 `_refresh_scene_buttons()` 统一管理，始终准确反映当前激活场景。

---

## 音频捕获

### Requirement: WASAPI Loopback 设备获取
系统 MUST 优先使用当前默认扬声器的 Loopback 设备；若失败，SHALL 遍历全部 Loopback 设备取第一个可用项。

```
1. sc.default_speaker() 获取当前默认输出设备
2. sc.get_microphone(id=speaker.name, include_loopback=True)
3. 失败则遍历 sc.all_microphones(include_loopback=True) 取第一个 isloopback=True
```

#### Scenario: 默认设备可用
- GIVEN 系统存在默认扬声器
- WHEN 捕获启动
- THEN 使用默认扬声器对应的 Loopback 设备

#### Scenario: 默认设备 Loopback 不可用
- GIVEN 默认扬声器无法获取 Loopback
- WHEN 捕获启动
- THEN 自动回退至第一个可用的 Loopback 设备

#### Scenario: 无任何 Loopback 设备
- GIVEN 系统无任何 Loopback 设备
- WHEN 捕获启动
- THEN 状态栏显示提示信息，程序不崩溃

### Requirement: 音频线程模型
录音线程（daemon thread）MUST 在线程内部调用 `CoInitializeEx(COINIT_MULTITHREADED)` 初始化 COM；以 2048 帧/块（~43ms @ 48kHz）阻塞式读取；将双声道混为单声道 float32 后，通过非阻塞队列传递至主线程（约 30fps 轮询）。

```
录音线程（daemon）
  CoInitializeEx(COINIT_MULTITHREADED)
  recorder.record(numframes=2048)  ← 阻塞直到 2048 帧就绪
    → 双声道混单声道 float32
    → detector.process(block)
    → AudioQueue.put_nowait(result)  ← 满则丢弃，绝不阻塞

        ↓  queue.Queue(maxsize=10)

主线程（tkinter 事件循环）
  root.after(33ms, _poll_queue)   ← ~30fps 轮询
    → 取最新结果 → 更新 Label + Canvas
```

#### Scenario: COM 初始化
- GIVEN 录音线程启动
- WHEN COM 尚未在本线程初始化
- THEN 线程头部自动调用 `CoInitializeEx`，`finally` 块调用 `CoUninitialize`

#### Scenario: 队列满载
- GIVEN UI 线程处理延迟，队列（maxsize=10）已满
- WHEN 录音线程尝试写入新帧
- THEN 本帧被丢弃，录音线程不阻塞

---

## 通用检测（FFT）

通用场景使用多音 FFT 检测，适用于乐器、和声等任意有音高的声音。

### Requirement: 多音检测流水线
系统 MUST 按以下 10 步流水线处理每帧音频，返回最多 `MAX_NOTES = 6` 个按响度降序排列的音名：

```
1.  静音门控：RMS < 1e-4 → 返回 []
2.  Hann 窗 + rfft（零填充 8192 点，5.4 Hz/bin @ 48kHz）
3.  转 dB（相对帧峰值），限制 80–2000 Hz 频带
4.  峰值检测：找局部极大值（≥ DB_THRESHOLD = -30 dB）
5.  谐波抑制：_suppress_harmonics()
6.  谐波覆盖度过滤：_peaks_to_notes() 中 harmonic coverage 过滤
7.  近邻音过滤：_filter_close_notes()，3 半音最小间距
8.  频率→音名：freq → MIDI → 音名 + 八度
9.  平滑：5 帧多数投票（≥ 3 帧出现才显示）
10. 截断：返回最多 6 个音名
```

**关键常量：**

| 常量                    | 值      | 说明                       |
|-------------------------|---------|----------------------------|
| `DB_THRESHOLD`          | -30 dB  | 峰值候选最低电平（相对帧峰值） |
| `HARMONIC_TOL`          | 0.05    | 谐波频率匹配容差 ±5%        |
| `HARMONIC_SCORE_MIN_DB` | -20 dB  | 计入谐波覆盖度的最低电平    |
| `SMOOTH_FRAMES`         | 5       | 多数投票平滑窗口帧数        |
| `SMOOTH_MIN_VOTES`      | 3       | 显示所需最少出票帧数        |
| `MAX_NOTES`             | 6       | 最多同时显示音符数          |

#### Scenario: 静音帧
- GIVEN 音频帧 RMS < 1e-4
- WHEN `process()` 被调用
- THEN 返回空列表 `[]`

#### Scenario: 单音检测
- GIVEN 包含单一基频（如 C4）的音频帧
- WHEN 通过 FFT 流水线处理
- THEN 返回对应音名（如 `['C4']`）

#### Scenario: 和弦检测
- GIVEN 同时包含多个基频（如 C4、E4、G4）的音频帧
- WHEN 通过流水线处理
- THEN 返回所有幸存基频的音名

### Requirement: 谐波抑制
`_suppress_harmonics()` MUST 按**频率升序**处理所有峰值，以 ±5% 容差判断整数倍关系，始终保留低频基频、抑制高频泛音，无论哪个更响。

> **理由：** 钢琴第 2 泛音（高八度）往往比基频更响。若按响度降序处理，会把泛音当基频保留，导致同一音符重复显示。

#### Scenario: 泛音响度高于基频
- GIVEN 钢琴音符，第 2 泛音能量强于基频
- WHEN 谐波抑制处理
- THEN 基频保留，第 2 泛音被抑制

### Requirement: 谐波覆盖度过滤
对每个幸存基频，统计高于 `HARMONIC_SCORE_MIN_DB = -20 dB` 的整数倍频峰数量（"强泛音数"）。若最高分 ≥ 2，MUST 只保留得分 ≥ `max_score − 2`（最低 1）的音。`HARMONIC_SCORE_MIN_DB` MUST NOT 低于 -30 dB。

> **理由：** 同情共振弦是单频振动，自身无泛音（得分 0–1），应被过滤；真正的和弦各音有完整谐波列（得分 4+），应全部保留。若阈值降至 -30 dB，噪声峰会被误计为谐波，导致共振弦得分虚高，过滤失效。

#### Scenario: 衰减段同情共振抑制
- GIVEN 钢琴单音在衰减段（约 1s 后），同情共振弦振动
- WHEN 谐波覆盖度过滤处理
- THEN 共振弦（得分 0–1）被过滤，演奏音（得分 4+）保留

#### Scenario: 和弦全部保留
- GIVEN 同时演奏的和弦，各音均有完整谐波列（得分 4+）
- WHEN 谐波覆盖度过滤处理
- THEN 所有和弦音均保留

---

## 人声检测（YIN）

人声场景使用 YIN 算法（de Cheveigné & Kawahara, 2002），专为单音基频估计设计，针对流行音乐混音中演唱者音高优化，对失真和缺基频的人声具有良好鲁棒性。

### Requirement: 人声检测流水线
系统 MUST 按以下 5 步流水线处理每帧音频：

```
1. 静音门控（RMS < 1e-4 → 清空历史，返回 []）
2. 高通滤波（FFT 零相位 HP，截止 VOCAL_HP_CUTOFF = 150 Hz）
   → 消除贝斯/踢鼓基频能量，防止其主导自相关
3. YIN 自相关（搜索范围：VOCAL_FREQ_MIN–VOCAL_FREQ_MAX = 150–1100 Hz）
   a. 差函数 d(τ) = Σ (x[j]−x[j+τ])²
   b. CMNDF：d′(τ) = d(τ)·τ / Σ_{j=1}^{τ} d(j)
   c. 阈值搜索：首个 d′(τ) < YIN_THRESHOLD = 0.15 的局部极小值
   d. 抛物线插值：亚采样精度
   e. 置信度 = 1 − CMNDF(τ_est)，< YIN_CONFIDENCE_MIN = 0.55 则进入回退
4. FFT 回退（VOCAL_FFT_MIN–VOCAL_FFT_MAX = 200–700 Hz，置信度不足时使用）
   → 在高通滤波后的频谱中找最强局部极大值
   → 次谐波抑制：仅当 f/d（d=2..7）在 ±harm_match_tol（≈4.4 Hz）内落在实际
     贝斯谐波上，且贝斯谐波能量 > 2× 候选能量时，才拒绝该候选
5. 中位数平滑（最近 VOCAL_SMOOTH_FRAMES = 5 帧有效频率的中位数）
   → 需至少 2 帧有效才输出，滑音平滑过渡
```

**关键常量：**

| 常量                  | 值      | 说明                             |
|-----------------------|---------|----------------------------------|
| `VOCAL_FREQ_MIN`      | 150 Hz  | YIN 搜索下限（排除贝斯基频）      |
| `VOCAL_FREQ_MAX`      | 1100 Hz | YIN 搜索上限（高至女高音 C6）     |
| `VOCAL_HP_CUTOFF`     | 150 Hz  | 高通滤波截止频率                  |
| `YIN_THRESHOLD`       | 0.15    | CMNDF 阈值，越低越严格            |
| `YIN_CONFIDENCE_MIN`  | 0.55    | 置信度下限，低于此值转 FFT 回退   |
| `VOCAL_FFT_MIN`       | 200 Hz  | FFT 回退搜索下限                  |
| `VOCAL_FFT_MAX`       | 700 Hz  | FFT 回退搜索上限                  |
| `VOCAL_SMOOTH_FRAMES` | 5       | 中位数平滑窗口帧数                |

`VOCAL_FREQ_MIN` 与 `VOCAL_HP_CUTOFF` MUST 保持一致，且 MUST NOT 低于 150 Hz。次谐波检查容差（`harm_match_tol`）MUST NOT 超过 `0.75 × (sample_rate / FFT_PAD)`（≈ 4.4 Hz @ 48kHz）。

> **理由（VOCAL_FREQ_MIN）：** 曾用 80 Hz，导致 YIN 以置信度 0.998 锁定贝斯基频（G2，98 Hz），一直显示 G2。
>
> **理由（harm_match_tol）：** 曾用 1.5 × DFT bin ≈ 35 Hz，导致 A4、C5 等人声音符被误判为贝斯谐波而静音。4.4 Hz 是基于零填充 FFT 量化误差的数学上界推导出的最小可靠值。

#### Scenario: 人声音高检测
- GIVEN 流行音乐混音，包含贝斯（G2，98 Hz）和演唱者（150–1100 Hz 范围内）
- WHEN 人声场景处理
- THEN 返回演唱者当前音高的音名

#### Scenario: 贝斯基频屏蔽
- GIVEN 贝斯基频（如 G2，98 Hz）存在于混音
- WHEN 人声场景处理
- THEN 贝斯基频被高通滤波屏蔽，不被当作人声音高输出

#### Scenario: 低置信度自动回退
- GIVEN YIN 置信度 < 0.55
- WHEN 当前帧处理
- THEN 自动切换至 FFT 回退（200–700 Hz）

#### Scenario: 次谐波候选拒绝
- GIVEN FFT 回退候选频率 f，其 f/d（d=2..7）与实际贝斯谐波偏差 ≤ 4.4 Hz
- WHEN 贝斯谐波能量 > 2× 候选能量
- THEN 候选被拒绝，不输出

---

## 场景管理

### Requirement: 场景热切换
系统 MUST 支持通用（FFT）和人声（YIN）两种场景，用户点击场景按钮即时生效。捕获运行中切换时，检测器实例 SHALL 通过主线程赋值热替换（依赖 CPython GIL 保证指针赋值原子性），无需显式锁。

#### Scenario: 静止时切换
- GIVEN 捕获未启动
- WHEN 用户点击场景按钮
- THEN 下次启动捕获时使用新场景的检测器实例

#### Scenario: 运行时切换
- GIVEN 捕获已启动
- WHEN 用户点击场景按钮
- THEN 检测器实例立即被新场景的检测器替换，无丢帧、无崩溃

---

## 异常处理

### Requirement: 无 Loopback 设备
- GIVEN 系统无任何可用 Loopback 设备
- WHEN 捕获启动
- THEN 状态栏显示提示信息，程序继续运行不崩溃

### Requirement: soundcard + numpy 2.x 兼容性
`numpy.fromstring`（二进制模式）在 numpy 2.0 中已移除。系统 MUST 在 `main.py` 启动时自动检测并将 `fromstring` 补丁为 `frombuffer`。

#### Scenario: numpy 2.x 环境
- GIVEN numpy >= 2.0 已安装
- WHEN 程序启动
- THEN 补丁自动应用，音频捕获正常工作

### Requirement: 音名抖动平滑
- 通用场景：5 帧多数投票（连续出现 ≥ 3 帧才显示），消除闪烁
- 人声场景：5 帧中位数平滑，消除抖动

### Requirement: 未捕获异常
- GIVEN 运行时发生未捕获异常
- WHEN 异常传播至顶层
- THEN 写入 `listen-ghost.log`，并弹窗显示末尾 800 字符

### Requirement: 依赖缺失
- GIVEN 必要依赖（soundcard、numpy 等）未安装
- WHEN 程序启动
- THEN 弹窗提示所需 `pip install` 命令

---

## 运维

### Requirement: 标准启动

#### Scenario: 首次运行
- GIVEN 虚拟环境已激活，依赖已安装
- WHEN 执行 `python main.py`
- THEN 窗口出现在桌面右下角，日志写入 `listen-ghost.log`

### Requirement: 打包为独立可执行文件

#### Scenario: PyInstaller 打包
- GIVEN PyInstaller 6.x 已安装
- WHEN 执行 `pyinstaller build.spec`
- THEN 输出 `dist/listen-ghost.exe`，可在无 Python 环境的 Windows 上独立运行

### Requirement: 端到端验收

#### Scenario: 人声场景验收
- GIVEN 窗口已启动
- WHEN 播放含人声的音频，点击 START，选择「人声」场景
- THEN 演唱者音高实时更新；停止播放后显示 `·`

#### Scenario: 通用场景验收
- GIVEN 窗口已启动，已切换到「通用」场景
- WHEN 播放含和声的音频
- THEN 多个和声音符同时显示

---

## 已知限制

- **人声场景——频率重叠漏检：** 无源分离能力，当演唱者音高与贝斯谐波直接重叠（如唱 D4 而贝斯正好在 G2 三倍频处）时，可能漏检。此为预期行为——项目有意不引入 Spleeter/Demucs 等 ML 源分离库，保持纯 numpy + 规则的轻量架构。
- **F4 附近频率偏移：** 349 Hz 落在贝斯三、四次谐波之间的 Hanning 泄漏谷，FFT 回退的频率估计可能偏移约半音（显示 F#4 而非 F4）。
- **通用模式高音区（E5 以上）：** `FREQ_MAX = 2000 Hz` 内可容纳的泛音数量 ≤ 1，谐波覆盖度过滤进入 fallback 模式（显示所有幸存基频），同情共振抑制效果减弱。
