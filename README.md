# voice-dictation — 实时语音听写记录

麦克风进来的音频 → VAD 切句 → 流式识别实时出字 → 落盘成 txt / md / srt / json。

全程本地推理，不联网、不上传音频。模型用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
（k2-fsa 的 ONNX 推理运行时），Python 包就一个 `pip install sherpa-onnx`，不带 torch。

> 📖 **核心教程：[`docs/windows-mic-wsl2.md`](docs/windows-mic-wsl2.md)**
> 在这台 WSL2 上用 **Windows 麦克风**实时听写、保存成 markdown 的完整步骤。

---

## 一、现在就能用（当前这台 WSL2）

**Windows 麦克风 → 这台机器识别 → 按对话存成 markdown**，Windows 侧零安装：

```sh
cd /home/shixiuhahi/work/voice-dictation
python3 dictation.py --source ws --port 8765
```

然后在 **Windows 的 Edge/Chrome** 打开 `http://localhost:8765/`，点「开始说话」就出声了
（浏览器采麦克风 → WebSocket 推回这台机器 → 实时出字 → 落盘）。
详见 [`docs/windows-mic-wsl2.md`](docs/windows-mic-wsl2.md)。

> **必须用 `localhost` 或 `127.0.0.1`**，不能用 WSL2 的 IP（`172.24.x.x`）。
> 浏览器的 `getUserMedia` 只在「安全上下文」下可用，IP 地址不是安全上下文，
> 页面能打开但**麦克风开不起来**。页面上有「麦克风可用 / 不可用」标签会提前告诉你，
> 点「开始说话」后状态条也会直接写明卡在哪一步，不会「点了没反应」。

页面本身就是个控制台，不用碰命令行：

- **实时文字**：识别结果直接在页面上出（灰的是正在说的半句，定稿后带时间戳）
- **改文件名 / 接着写**：改名即时生效，磁盘上的文件跟着改；能挑历史记录续写
- **选麦克风、调输入增益**
- **深色 / 浅色主题**：右上角一键切换，选择记在浏览器里；没手动选过时跟随系统
- **下载**：`.md` / `.txt` / `.srt` / `.json` 一键存到 Windows

> 文件是**边说边写**的：每识别出一句就重写一遍 `.md` / `.srt` / `.json`，
> 进程被 kill 也不丢内容。`Ctrl+C` 只是收尾并补上最后一句。
> 浏览器断开后服务端会继续等（再打开页面可接着说）；想「断开即退出」加 `--once`。
> **页面不用等模型加载完**：端口一绑上就能打开（实测 2 毫秒），页面上自己轮询模型状态。

其它几种玩法：

```sh
python3 dictation.py --file samples/1.wav                # 听写仓库自带的测试音频
python3 dictation.py --source pulse --device RDPSource   # 抓 Windows 会话的音频
python3 dictation.py --source tcp --port 8765             # 收别的机器推来的音频
python3 scripts/capture_audio.py --port 8770             # 采音存 WAV + 电平分析（诊断用）
```

> **效果不好先量电平。** `scripts/capture_audio.py` 起一个采音页，
> 浏览器用 `getDisplayMedia` **数字直采**正在播放的音频（或麦克风），
> 存成 WAV 并打印电平分布、静音占比、VAD 覆盖率。
> 实测「识别效果差」绝大多数是音源太轻：用户真实录的电视剧音频 RMS 只有
> **−47.6 dBFS**（正常语音 −25 左右），VAD 覆盖率只剩 **9%**。

听写记录落在 `transcripts/`，实时输出打在终端上（灰色 `…` 是正在说的半句）。

### 为什么没有「直接开麦」这一步

**这台机器没有麦克风设备**，这是内核层面的事，开关是打不开的：

```
$ ls /dev/snd
crw-rw---- 1 root audio 116, 33 Oct 25 12:59 timer      # 只有定时器，没有采集节点

$ ls /proc/asound
ls: cannot access '/proc/asound': No such file or directory
```

WSL2 不把宿主机的声卡透传进内核，所以没有 ALSA 采集设备，也没有 `/dev/snd` 的 `capture` 节点。
WSLg 的 PulseAudio 桥是通的，但**只解决输出**（Linux 放声音给 Windows）。

不过 WSLg 的 Pulse 服务端其实带了一个**采集源** `RDPSource`（`module-rdp-source`），
能抓到 Windows 会话的音频流（Windows 侧开麦重定向的话，麦克风也在里面）：

```
$ pactl list short sources
1	RDPSink.monitor	module-rdp-sink.c	s16le 2ch 44100Hz	SUSPENDED
2	RDPSource	module-rdp-source.c	s16le 1ch 44100Hz	SUSPENDED
```

实测能录到连续音频并识别出中文内容。

> 这台机器没装 `pulseaudio-utils`（要 root），所以我在 `~/.local/opt/pulse/` 放了一份免 root 的
> `parec` / `pactl`，并已把 `~/.local/opt/pulse/bin` 加进 `~/.bashrc` 的 PATH。
> 想要系统级安装就 `sudo apt install pulseaudio-utils`。

### 音频来源一览

| `--source` | 说明 |
|---|---|
| `ws` | **浏览器当麦克风**：托管采集页面，浏览器用 `getUserMedia` 采音频经 WebSocket 推回来。Windows 侧零安装，首选 |
| `mic` | 本机麦克风。Linux 有 ALSA 采集设备时走 ALSA，否则自动回退到 `sounddevice` 后端（Windows/macOS 用这个） |
| `file` | 听写音频文件，`--realtime` 可按 1 倍速喂 |
| `stdin` | 从标准输入读 16 kHz / 单声道 / s16le PCM |
| `pulse` | 抓 PulseAudio 采集源，`--device` 指定源名（`default` / `RDPSource`） |
| `tcp` | 监听 TCP，收别的机器推来的 PCM（裸 16 kHz s16le 或整个 WAV 文件都行）。客户端断开后继续等下一个，不用重启服务端 |

---

## 二、识别原理与交互流程

### 识别方案：两段式（流式出字 + 句末重识别）

一路音频要经过 6 个环节，**每个环节用的模型都不一样**：

```
音频 16kHz/单声道/float32
   │
   ├─① 电平归一化  LevelNormalizer（纯 numpy，无模型）
   │     只补不削：最近 5 秒内出现过够响的内容就按位透传，
   │     整体偏轻才补增益（默认最多 64 倍 = +36 dB）
   │
   ├─② VAD 切句    Silero VAD（630 KB ONNX）
   │     判「是不是人在说话」，决定一句从哪到哪
   │     阈值 0.35 / 静音 0.3s / 单句最长 8s（兜底强制切）
   │
   ├─③ 流式识别    small-bilingual zipformer transducer（50 MB）
   │     边走边出字，屏幕上灰色「…」就是它，负责「实时」
   │
   ├─④ 句末重识别  paraformer-zh 非流式（82 MB）
   │     一句说完后把整句重跑一遍，负责「准确」
   │
   ├─⑤ 标点恢复    CT-Transformer（138 MB）
   │     加逗号句号问号
   │
   └─⑥ 落盘        txt / md / srt / json，每出一句就重写
```

**为什么必须两段式**：流式模型为了赶实时会牺牲准确率、甚至漏字。同一段音频：

```
small-bilingual 单独跑：   [00:00:22 → 00:00:27] 第一句是加。                            ← 只认出一半
small-bilingual + 重识别： [00:00:22 → 00:00:27] 第一句是个什么时态加了es是一般现代时，对，…  ← 完整
```

所以 ③ 负责**延迟**（立刻看到字），④ 负责**质量**（几秒后把这一句修正）。
每句结尾多等零点几秒，对「边说边记」很划算。流式模型偶尔整段给空结果，重识别也正好捞回来。

### 实测：能不能实时

唯一硬指标是 **RTF**（推理耗时 ÷ 音频时长，< 1 才跟得上）。

**用真实电视剧音频跑完整链路（60 秒，2 核，预热后）**：

| 环节 | 耗时 | RTF |
|---|---|---|
| 流式识别 | 21.95s | 0.366 |
| + 整句重识别 + 标点 | 26.37s | **0.439** |
| 合计 | | **✓ 跟得上（约 2.3x 实时）** |

各模型单独实测（`scripts/bench.py`，2 线程，剔除预热）：

| 模型 | 类型 | 体积 | RTF | 倍速 |
|---|---|---|---|---|
| **`small-bilingual`**（默认流式） | zipformer 双语 | 50 MB | **0.233~0.388** | 2.6~4.3x |
| `bilingual` | zipformer 双语 | 198 MB | 1.565 | **0.6x ✗ 跟不上了** |
| `zh-14m` | zipformer 14M 中文 | 25 MB | 0.234 | 4.3x |
| `ctc-zh` | zipformer2 CTC 中文 | 26 MB | 0.361 | 2.8x |
| `paraformer-zh`（整句重识别） | 非流式 paraformer | 82 MB | 0.461 | 2.2x |

> **首次推理要预热**：第一个 chunk 包含 ONNX 图初始化和内存分配，单独算会得出
> 「RTF 1.2、跟不上」的假结论。`scripts/bench.py` 和实际运行都跑在长流上，
> 所以看长音频的稳态值才准。
>
> **`bilingual`（198 MB）在这台机器上已经跟不上实时**（RTF 1.57），别当默认用——
> 它体积是 `small-bilingual` 的 4 倍，但实测识别结果并不更好。

### 交互流程

**浏览器那端的完整链路**（`--source ws`）：

```
浏览器                          服务端（WSL2）
  │                                │
  │ ① GET /  ──────────────────────│  HTTP 线程一绑端口就起，页面秒开（不等模型）
  │ ② 轮询 GET /api/status ────────│  模型没好 → 页面显示「服务端正在加载模型」
  │                                │  模型就绪 → WS 握手才接受（之前返回 503）
  │ ③ GET /ws（WebSocket 升级）────│
  │ ④ ← {"cmd":"hello", ...} ──────│  连上就推历史内容 + 会话清单 + 状态
  │                                │
  │   点「开始说话」                │
  │ ⑤ getUserMedia 采麦克风         │  只在安全上下文（localhost/https）可用
  │ ⑥ 16kHz s16le PCM 二进制帧 ───→│  先到的客户端一直占着麦克风，后来的收 superseded
  │                                │  ⑦ 归一化 → VAD → 流式识别
  │ ⑧ ← {"cmd":"partial","text"} ──│  半句，灰字原地刷新
  │ ⑨ ← {"cmd":"segment",...} ─────│  定稿句（重识别+标点后的），带时间戳
  │                                │  同时写进 transcripts/*.md/.srt/.json
  │   点「停止」                    │
  │ ⑩ 关 WebSocket ───────────────→│  服务端收尾，记录已实时落盘
  │                                │  （默认继续等下一个客户端；--once 才退出）
```

**命令行那端**（`--file` / `--source mic` / `pulse` / `tcp`）流程一样，
只是 ①~⑤ 换成了本地取流，没有 ⑧⑨ 的推送，字直接打在终端上。

控制指令走同一条 WebSocket 的文本帧：
`{"cmd":"name","name":"会议"}` 改名、`{"cmd":"append","stem":"..."}` 接着写历史记录。

### 怎么知道自己跟不跟得上

```sh
python3 scripts/bench.py            # 各模型单独 RTF
python3 dictation.py --file x.wav   # 看落盘时间戳 vs 音频时长
```

实时运行时如果 CPU 吃满、字越出越慢，就是 RTF > 1 了，换更小的模型或
`--refine-model ""` 关掉重识别。

---

## 三、实测环境

| | |
|---|---|
| 系统 | Kali GNU/Linux Rolling 2026.2，WSL2 内核 6.18.54.1 |
| CPU | 2 vCPU（x86_64） |
| 内存 | 3.9 GB，无 swap |
| GPU | 无 |
| 音频 | 无 ALSA 驱动、无采集设备 |
| Python | 3.13.12，`pip install --user sherpa-onnx`（2.0.0，cp313 轮子） |

没有 GPU、只有 2 个核，所以模型选型的唯一标准就是 **RTF（实时率）能不能压住**。

## 四、模型选型（全部实测）

RTF = 推理耗时 ÷ 音频时长。< 1 才能边听边出字，0.23 就是比实时快 4 倍。
测试音频固定取双语模型自带的 4 段中英混说音频（约 28 s），2 线程，剔除首次推理的预热开销。

| 模型 | 类型 | 体积 | RTF | 倍速 | 中英混说表现 |
|---|---|---|---|---|---|
| **`small-bilingual`**（默认） | 流式 zipformer 双语 | 50 MB | **0.388** | **2.6x** | 中英都能出，英文词形基本正确 |
| `bilingual` | 流式 zipformer 双语 | 198 MB | 1.565 | **0.6x ✗** | 中英都能出，英文全大写、偶有重复 |
| `zh-14m` | 流式 zipformer 14M 中文 | 25 MB | 0.234 | 4.3x | 中文可用，英文基本认不出 |
| `ctc-zh` | 流式 zipformer2 CTC 中文 | 26 MB | 0.361 | 2.8x | 英文输出 `<unk>` |
| `paraformer-zh` | **非**流式 paraformer 中文 | 82 MB | 0.461 | 2.2x | 中文最准，英文能写成小写拉丁词 |
| `sense-voice` | **非**流式 SenseVoice 多语种 | 165 MB | 待测 | — | 中英日韩粤，**抗背景噪声/音乐最强** |

> **别用 `bilingual`（198 MB）当默认**：它在这台机器上 RTF 1.565，**已经跟不上实时**，
> 而且实测识别结果并不比 50 MB 的 `small-bilingual` 更好。体积大 ≠ 更准。

复现：

```sh
python3 scripts/bench.py
```

### 结论：两段式

流式模型为了赶实时会牺牲准确率，甚至会**漏字**——同一段音频：

```
small-bilingual 单独跑：  [00:00:22 → 00:00:27] 第一句是加。          ← 只认出一半
small-bilingual + 重识别： [00:00:22 → 00:00:27] 第一句是个什么时态加了es是一般现代时，对，…  ← 完整
```

所以程序默认走**两段式**：

1. **流式模型**（默认 `small-bilingual`）负责实时出字，屏幕上灰色 `…` 不断刷新；
2. **句末**用非流式 `paraformer-zh` 把整句重跑一遍，准确率明显更好，
   重识别耗时只有一句的量级（RTF 0.46）。

代价是每句结尾多等零点几秒。对「边说边记」这个场景，这笔买卖很划算。
另外流式模型偶尔会对某一段给出**空结果**（整段无声），重识别这一遍正好把它捞回来。

只要模型目录里有对应文件就能换：

```sh
python3 dictation.py --model bilingual --refine-model paraformer-zh
python3 dictation.py --model zh-14m --refine-model ""      # 关掉重识别，只留实时结果
python3 dictation.py --refine-model sense-voice            # 换 SenseVoice 做整句重识别
```

---

## 五、用法

```sh
# 1. 装运行库（只需要 sherpa-onnx，自带 onnxruntime，不要 torch）
pip install --user -r requirements.txt

# 2. 下模型（走代理：HTTPS_PROXY=http://192.168.6.9:7890 bash scripts/download_models.sh）
bash scripts/download_models.sh

# 3. 开听
python3 dictation.py                                    # 麦克风（无采集设备时会说明原因）
python3 dictation.py --source ws --port 8765             # 浏览器当麦克风（Windows 侧零安装）
python3 dictation.py --file samples/1.wav                # 文件（仓库自带两段测试音频）
python3 dictation.py --source pulse --device RDPSource   # 抓 PulseAudio 采集源
python3 dictation.py --source tcp --port 8765            # 收别的机器推来的音频
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--model` | 流式模型名或模型目录（`small-bilingual` / `bilingual` / `zh-14m` / `ctc-zh` / `paraformer-zh`） |
| `--refine-model` | 整句重识别用的非流式模型，空串关闭 |
| `--source` | `ws` / `mic` / `file` / `stdin` / `pulse` / `tcp` |
| `--file` `--realtime` | 听写文件；`--realtime` 按 1 倍速喂，用来验证实时链路 |
| `--device` | `mic` 用 ALSA 设备名（`default`，多麦机器用 `plughw:1,0`）；`pulse` 用 PulseAudio 源名（`default` / `RDPSource`） |
| `--host` `--port` | `tcp` / `ws` 服务的监听地址与端口，默认 `0.0.0.0:8765` |
| `--once` | 只服务一个客户端：客户端断开就结束本次听写并退出（`ws` / `tcp` 默认继续等下一个） |
| `--out-dir` `--formats` | 输出目录与格式（`txt,md,srt,json`） |
| `--out-name` | 记录文件名前缀，最终文件名是 `<前缀>-<开始时间>.md`（默认「听写」） |
| `--no-punct` / `--no-vad` | 关标点 / 关 VAD（改用识别器端点检测切句） |
| `--agc` / `--no-agc` | 电平归一化开关（**默认开**）。输入偏轻时自动补增益，远场/电视声必需 |
| `--agc-max-gain` | 最多补多少倍，默认 64（+36 dB） |
| `--vad-threshold` | VAD 语音判定阈值，默认 0.35（调低更容易切出句子） |
| `--vad-silence` | 多长静音算一句说完，默认 0.3s |
| `--vad-max-speech` | 单句最长多少秒强制切一刀，**默认 8s**。电视这种句间没静音的音频全靠它 |
| `--hotwords` | 热词文件，每行一个词或短语，提升专有名词准确率 |
| `--threads` | 推理线程数，默认 2 |
| `--list-devices` / `--list-models` | 列采集设备（ALSA / Pulse / sounddevice）/ 列已下载模型 |

终端上正在说的半句是灰色原地刷新，说完的句子带时间戳定格：

```
[00:00:00 → 00:00:05] 这是第二种，第二种叫呃与always o s什么意思啊？
```

## 六、输出

每次听写生成一组同名文件（`transcripts/听写-20261025-142211.*`）：

- `.md` — **按对话分段**的听写记录：抬头是会话信息，正文每一句一个 `##` 小节，带起止时间戳
- `.txt` — 纯文本，一行一句，带起止时间戳，边说边写，Ctrl+C 不丢
- `.srt` — 字幕，可直接挂到视频上
- `.json` — 结构化：`{"session": {...}, "segments": [{"start", "end", "text"}]}`

### 文件名怎么定

命名规则固定是 **`<名字>-<开始时间>`**，比如 `会议纪要-20261025-142211.md`。
名字可以三处配，效果一样：

| 方式 | 用法 |
|---|---|
| 默认 | 什么都不配 → `听写-20261025-142211.md` |
| 启动时定 | `python3 dictation.py --out-name 会议纪要` |
| 浏览器里改 | 页面上的「记录文件名」输入框 → 改名即时生效，磁盘上的文件跟着改 |

**为什么始终带时间戳**：名字可以随便起（会议、周报、访谈……），但带上开始时间就不会撞名、
不会覆盖旧记录，按时间排也天然有序。改名字时只换前缀，时间戳不变。

> 浏览器推过来的名字按**外部输入**处理：去掉路径分隔符和 `..`、过滤非法字符与控制字符、
> 限长 60，所以推 `../../etc/passwd` 只会变成 `passwd` 落在记录目录里。

### 接着写历史记录

浏览器页面上有「接着写」下拉框，列出输出目录里的历史记录（新的在前）。
选一份，新说的内容就**接在它后面**：

- 原来那句、名称、开始时间都保留；
- 时间戳接着往下走（上一份记录停在 10s，新内容就从 10s 接着记）；
- `.srt` 的序号连续，`.json` 的段数累加。

命令行等价物：

```sh
python3 dictation.py --list-sessions                 # 看有哪些历史记录
python3 dictation.py --append 会议纪要-20261025-142211 # 接着写这一份
```

> 选历史记录时，程序刚建好但还空着的那份会自动删掉，不留空壳。

`.md` 长这样：

```markdown
# 语音听写记录

- 名称：听写
- 开始时间：2026-10-25 14:02:33
- 结束时间：2026-10-25 14:05:12
- 句数：14
- 语音时长：142.6 s
- 模型：small-bilingual
- 整句重识别：paraformer-zh
- 来源：浏览器麦克风（http://127.0.0.1:8765/）

---

## 00:00:00 → 00:00:05

这是第一种，第二种叫呃与always o s什么意思啊？

## 00:00:06 → 00:00:11

第一句是个什么时态加了es是一般现代时，对，然后把它试测一下商品。
```

### 下载

浏览器页面「记录」区有四个下载按钮，存到 Windows 的下载目录；
命令行也可以直接拉：

```sh
curl -O http://127.0.0.1:8765/download/听写-20261025-142211.md
```

服务端只允许下载记录目录下的 `.md` / `.txt` / `.srt` / `.json`，
路径穿越（`../`）会被挡掉。

### 不开浏览器怎么测

`scripts/ws_probe.py` 模拟浏览器（手搓 WebSocket 帧，不依赖第三方库），
可以拿来验证服务端链路，也可以在没麦克风的机器上出字：

```sh
python3 dictation.py --source ws --port 8765          # 另一个终端
python3 scripts/ws_probe.py --file samples/1.wav --realtime
```

它会打印服务端推回来的每一句（`[segment]`）和半句（`[partial]`）。

## 七、程序结构

```
voice-dictation/
├── dictation.py            CLI 主程序：参数、主循环、实时显示
├── vd/
│   ├── audio.py            音频源：浏览器 WS / ALSA / sounddevice / WAV / stdin / Pulse / TCP
│   ├── wsserver.py         最小 WebSocket + HTTP 服务端（浏览器麦克风用，无第三方依赖）
│   ├── mic_client.html     浏览器控制台（getUserMedia → 16k s16le + 实时文字 + 配置）
│   ├── capture_client.html 浏览器采音页（getDisplayMedia 系统音频 / 麦克风，分析用）
│   ├── vad.py              Silero VAD 封装，切句
│   ├── asr.py              流式 + 非流式识别封装（含 SenseVoice）
│   ├── punct.py            标点恢复（CT-Transformer）
│   ├── models.py           模型登记表与模型文件自动识别
│   └── session.py          听写记录落盘（txt/md/srt/json）
├── scripts/
│   ├── download_models.sh  模型下载
│   ├── send_mic.py         另一台机器把麦克风推过来（Python）
│   ├── capture_audio.py    浏览器采音存 WAV + 电平/VAD 分析（诊断用）
│   ├── ws_probe.py         模拟浏览器的 ws 客户端（联调/自测用）
│   ├── windows_mic_push.ps1  Windows 侧推流（只要 ffmpeg，不要 Python）
│   └── bench.py            实测各模型 RTF
├── docs/
│   └── windows-mic-wsl2.md 核心教程：WSL2 + Windows 麦克风实时听写
├── samples/                两段测试音频（Apache-2.0，来自 sherpa-onnx）
├── models/                 模型目录（不入库）
│   ├── asr/                各 ASR 模型
│   ├── vad/ silero_vad.onnx
│   ├── punct/              CT-Transformer 标点模型
│   └── _archives/          下载的原始压缩包（GitHub 下载很慢，留着备用）
└── transcripts/            听写记录（不入库）
```

音频源统一输出 16 kHz / 单声道 / float32，
然后 **电平归一化 → VAD 切句 → 流式识别 →（可选）非流式重识别 →（可选）标点**。
详见[第二节「识别原理与交互流程」](#二识别原理与交互流程)。

### ws 源的服务端结构

`vd/wsserver.py` 一个端口同时提供两样东西，**各跑各的线程，互不阻塞**：

| 路径 | 作用 |
|---|---|
| `GET /` | 控制台页面 |
| `GET /api/status` | 状态：模型就绪没、当前记录、句数、已识别内容 |
| `GET /api/sessions` | 历史记录清单 |
| `GET /download/<文件>` | 下载 `.md` / `.txt` / `.srt` / `.json` |
| `GET /ws` | WebSocket：浏览器推 16 kHz s16le PCM，接收控制指令和识别结果 |

页面在**模型加载完之前**就能打开（HTTP 线程在构造时就起来了），模型就绪后
WebSocket 才开始接受握手（没就绪时返回 503，页面自己等）。
这样「端口先绑定、模型后加载」就不会让浏览器干等。

## 八、坑

- **VAD 的「当前段」比「已完成段」长**：已完成段是把尾部静音裁掉之后的版本。
  如果按「已完成段比已喂的多就补喂」去写兜底分支，同一段音频会被识别两遍，
  输出变成「昨天是MONDAY，昨天是MONDAY。」。正确做法是按段的起始采样偏移
  （`current_segment.start`）跟踪当前段，段结束时不再补喂。
- **流式模型会漏字、会给空结果**：见上文两段式。
- **`sherpa_onnx.Alsa` 打开失败会直接 `exit(-1)`**：这是 C++ 侧的行为，Python 拦不住，
  所以必须在打开设备之前先用 `/proc/asound/pcm` 探测有没有采集设备。
- **ALSA 缺 `alsa.conf`**：这台机器没装 alsa-utils，`/usr/share/alsa/alsa.conf` 不存在，
  ALSA 库即使静态链接进来也开不了设备。
- **8 kHz 音频**：`sherpa-onnx` 会在内部重采样，但会往 stderr 打 `Creating a resampler`，
  看着像报错，其实不是。
- **GitHub release 下载会被限速**：458 MB 的双语小模型在这台机器上跑了 25 分钟
  （约 130 KB/s），中途 TLS 还会断；脚本里带 `--retry` 和断点续传。
- **免 root 装 pulseaudio-utils**：`apt-get download` 下 deb 再 `dpkg-deb -x` 解到
  `~/.local/opt/pulse/`，但 `pactl`/`parec` 会依次缺 `libpulsecommon` / `libsndfile` /
  `libFLAC` / `libX11-xcb`…，得把依赖一次性下齐（`apt-cache depends --recurse` 列全）。
  解出来的 `parec`/`pactl` 要自己包一层 `LD_LIBRARY_PATH` 和 `PULSE_SERVER`。
- **`is_speech_detected` 是方法不是属性**：sherpa-onnx 的 Python 绑定里
  `vad.is_speech_detected` 少了括号会拿到 bound method，`if` 恒为真，行为会很怪。
- **`RDPSource` 录到的是 Windows 会话的音频**：是不是包含麦克风取决于 Windows 那边的
  录音重定向设置，WSL2 侧看不出来。
- **浏览器端要接一个 gain=0 的节点再连 destination**：`ScriptProcessorNode` 不接到
  destination 不会被调度；直接接会把自己的麦克风声音放出来。
- **浏览器的安全上下文**：`getUserMedia` 只在 `https` 或 `localhost`/`127.0.0.1` 下可用。
  用 WSL2 的 IP（`http://172.24.x.x:8765/`）打开页面能看能点，但麦克风**开不起来**
  ——这是浏览器的规则，不是本程序的限制。
- **音源太轻是「识别不出来」的头号原因**：Silero VAD 的阈值判的是「语音概率」而不是
  电平，输入一轻概率整体偏低、阈值跨不过去，表现是**一句都不出**。
  用户真实录的电视剧音频 RMS −47.6 / 峰值 −27.6 dBFS，VAD 覆盖率只有 9%；
  归一化后覆盖 59.7s/60s。默认已开电平归一化（`--no-agc` 关）。
- **连续有声的音频（电视/直播）VAD 切不出句**：这类音频句间没有真正的静音，
  `min_silence_duration` 调了没用（16000 Hz 下 Silero 窗口只有 32 ms，0.15s 不是有效档位），
  只能靠 `--vad-max-speech` 兜底强制切分。默认 30s 时 60 秒只剩 2 段、要等 30 秒才出第一句，
  看起来就像「不识别」；改成 **8s** 后首句 8 秒出来，准确率无损失。
- **首次推理必须预热**：第一个 chunk 含 ONNX 图初始化，单独计时会得出
  「流式 RTF 1.2、跟不上实时」的假结论。真实稳态是 **0.37**，整条链路（含重识别+标点）**0.44**。
- **GTCRN 流式降噪在这台机器上不能用**：合成音频上把相似度 0.58→0.88 很惊艳，
  但真实音频上 **RTF 1.29（比实时慢）**，而且降完电平没变，VAD 覆盖率反而 13%→5%。
- **手写的 WebSocket 服务端**（`vd/wsserver.py`）只实现了最小子集：
  HTTP/1.1 升级握手 + 客户端带掩码的二进制帧。别指望它支持分片扩展、压缩、ping/pong 之外的
  控制帧。
- **WS 源里 HTTP 和 WS 必须分线程**：早期版本把两者放在同一个 accept 循环里，
  一个 WS 连接占着循环时，第二个标签页连页面都打不开（HTTP 请求排在 accept 队列里没人应答）。
  现在 HTTP 每个连接一个线程，WS 也一个，多标签页互不阻塞。
- **麦克风不抢**：先连上的那个客户端占着麦克风，后来的只收到提示，不会两个页面互相抢音频。
- **`_reply` 的 body 必须是 bytes**：曾经把 str 传进去，异常被连接处理器吞掉，
  表现是下载接口直接断连接（curl 看到 000 而不是 400）。

## 九、许可与来源

- 本仓库代码：Apache-2.0（见 `LICENSE`）。
- 识别模型、VAD、标点模型：来自 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
  及其 [预训练模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases)，各自 Apache-2.0；
  `scripts/download_models.sh` 只负责下载，不改动模型内容。
- `samples/` 里的测试音频同样来自 sherpa-onnx 模型包，Apache-2.0。
- 中文模型的训练数据与许可请见各模型目录里的 `README.md`。
