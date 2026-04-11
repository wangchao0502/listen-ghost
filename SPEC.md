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
| 语言       | Python 3.14+                                  |
| 音频捕获   | soundcard 0.4.x（Windows Media Foundation）   |
| 信号处理   | numpy 2.x（FFT + 峰值检测）                   |
| GUI        | tkinter（Python 内置）                        |
| 打包       | PyInstaller 6.x（可选）                       |

## 文件结构

```
listen-ghost/
├── SPEC.md
├── main.py                      # 入口：DPI 设置 + 自动补丁 + 依赖检查 + 启动
├── requirements.txt
├── build.spec                   # PyInstaller 打包配置
├── audios/                      # 测试音频文件
└── listen_ghost/
    ├── __init__.py
    ├── audio_capture.py         # WASAPI Loopback 捕获（soundcard）
    ├── pitch_detector.py        # FFT 音高检测 + 频率→音名转换
    ├── threading_bridge.py      # 非阻塞队列（录音线程→UI线程）
    └── app.py                   # tkinter UI + 窗口定位 + 轮询循环
```

## 核心架构

### 线程模型

```
录音线程（daemon thread，soundcard 阻塞式 API）
  CoInitializeEx(COINIT_MULTITHREADED)   ← COM 必须在本线程初始化
  recorder.record(numframes=2048)        ← 阻塞直到 2048 帧就绪 (~43ms)
    → 双声道混单声道 float32
    → PitchDetector.process(block)       ← FFT ~1ms
    → AudioQueue.put_nowait(notes)       ← 满则丢弃，绝不阻塞

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

### FFT 音高检测

```python
class PitchDetector:
    def process(block, sample_rate) -> List[str]:
        # 1. RMS 静音门控：rms < 1e-4 → 返回 []
        # 2. Hann 窗 + rfft（零填充 8192 点，5.4Hz/bin 分辨率 @ 48kHz）
        # 3. 转 dB（相对峰值），限制 80Hz–2000Hz 频带
        # 4. 找局部极大值（>= DB_THRESHOLD = -30dB）
        # 5. suppress_harmonics：去除整数倍谐波（±5% 容差）
        # 6. filter_close_notes：去除相距 <3 半音的较弱音
        # 7. freq_to_note：频率 → MIDI → 音名 + 八度
        # 8. 3帧多数投票平滑（连续出现 ≥2 帧才显示）
        # 9. 返回最多 MAX_NOTES=6 个音名，按响度降序
```

音名格式（科学音高记谱法）：`C4`, `F#5`, `A3`, `D#2` 等。

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
| 音名闪烁                      | 3帧多数投票平滑                                  |
| DPI 缩放                      | SetProcessDpiAwareness(2)，回退 SetProcessDPIAware |
| 依赖缺失                      | 弹窗提示 pip install 命令                        |
| 未捕获异常                    | 写入 listen-ghost.log + 弹窗显示末尾 800 字符    |

## 运行方式

```bash
pip install soundcard numpy
python main.py
```

## 打包为 .exe

```bash
pip install pyinstaller
pyinstaller build.spec
# 输出：dist/listen-ghost.exe
```

## 验证步骤

1. `python main.py` — 窗口出现在桌面右下角
2. 播放 `audios/11.mp3`，点击 START，观察音名实时更新（如 `D4   G3   C#6`）
3. 停止播放后音名变为 `·`
4. 日志写入 `listen-ghost.log`

## 开发历程

### 已解决的关键问题

| 问题 | 根因 | 解决方案 |
|------|------|----------|
| `WasapiSettings` 无 `loopback` 参数 | sounddevice 从未支持此参数，文档有误 | 改用 `soundcard` 库，原生支持 WASAPI Loopback |
| `CO_E_NOTINITIALIZED (0x800401f0)` | Windows COM 未在录音线程初始化 | 线程启动时调 `CoInitializeEx(None, 0)` |
| `numpy.fromstring` 已移除 | soundcard 0.4.x 兼容 numpy 1.x，numpy 2.0 删除了二进制模式 | main.py 启动时自动检测并补丁 |
| 谐波被检测为独立音 | FFT 对基频整数倍同样产生峰值 | `suppress_harmonics()`：±5% 容差过滤整数倍频 |
| 相邻半音误检 | FFT bin 间距在高频区接近半音宽度 | `filter_close_notes()`：3 半音最小间距 |
