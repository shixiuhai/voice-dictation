# voice-dictation — 实时语音听写记录

麦克风进来的音频 → VAD 切句 → 流式识别实时出字 → 落盘成 txt / md / srt / json。

全程本地推理，不联网、不上传音频。模型用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
（k2-fsa 的 ONNX 推理运行时），Python 包就一个 `pip install sherpa-onnx`，不带 torch。

---

## 一、先回答两个问题

### 1. 这台机器能访问麦克风吗？

**不能。** 这是 WSL2（`6.18.54.1-microsoft-standard-WSL2`），内核里没有音频驱动：

```
$ ls /dev/snd
crw-rw---- 1 root audio 116, 33 Oct 25 12:59 timer      # 只有定时器，没有采集节点

$ ls /proc/asound
ls: cannot access '/proc/asound': No such file or directory
```

WSLg 的 PulseAudio 桥是通的（`/mnt/wslg/runtime-dir/pulse/native` 有 socket，
`pulseaudio.log` 里 rdp-sink 已连接），但它只提供**输出**：
Linux 侧放声音给 Windows，没有把 Windows 麦克风送进 Linux 的通道。
所以 `--source mic` 在这台机器上必然失败——程序会直接告诉你原因，而不是抛一屏 ALSA 报错。

### 2. 那这程序怎么用？

三种绕开本机麦克风的办法，都实测跑通了：

| 方式 | 场景 | 命令 |
|---|---|---|
| `--source file` | 听写已有音频 | `python3 dictation.py --file a.wav` |
| `--source stdin` | 接任意采集程序 | `arecord -t raw -f S16_LE -r 16000 -c 1 \| python3 dictation.py --source stdin` |
| `--source tcp` | 麦克风在别的机器上 | 服务端 `python3 dictation.py --source tcp --port 8765`，客户端 `python3 scripts/send_mic.py --host <IP> --port 8765` |

把这套程序搬到有声卡的机器上，`python3 dictation.py` 直接就是麦克风听写。

---

## 二、实测环境

| | |
|---|---|
| 系统 | Kali GNU/Linux Rolling 2026.2，WSL2 内核 6.18.54.1 |
| CPU | 2 vCPU（x86_64） |
| 内存 | 3.9 GB，无 swap |
| GPU | 无 |
| 音频 | 无 ALSA 驱动、无采集设备 |
| Python | 3.13.12，`pip install --user sherpa-onnx`（2.0.0，cp313 轮子） |

没有 GPU、只有 2 个核，所以模型选型的唯一标准就是 **RTF（实时率）能不能压住**。

## 三、模型选型（全部实测）

RTF = 推理耗时 ÷ 音频时长。< 1 才能边听边出字，0.23 就是比实时快 4 倍。
测试音频固定取双语模型自带的 4 段中英混说音频（约 28 s），2 线程，剔除首次推理的预热开销。

| 模型 | 类型 | 体积 | RTF | 倍速 | 中英混说表现 |
|---|---|---|---|---|---|
| **`small-bilingual`**（默认） | 流式 zipformer 双语 | 50 MB | **0.233** | **4.3x** | 中英都能出，英文词形基本正确 |
| `bilingual` | 流式 zipformer 双语 | 198 MB | 0.383 | 2.6x | 中英都能出，英文全大写、偶有重复 |
| `zh-14m` | 流式 zipformer 14M 中文 | 25 MB | 0.148 | 6.8x | 中文可用，英文基本认不出 |
| `ctc-zh` | 流式 zipformer2 CTC 中文 | 26 MB | 0.085 | 11.7x | 最快，英文输出 `<unk>` |
| `paraformer-zh` | **非**流式 paraformer 中文 | 82 MB | 0.121 | 8.3x | 中文最准，英文能写成小写拉丁词 |

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
   重识别耗时只有一句的量级（RTF 0.12）。

代价是每句结尾多等零点几秒。对「边说边记」这个场景，这笔买卖很划算。
另外流式模型偶尔会对某一段给出**空结果**（整段无声），重识别这一遍正好把它捞回来。

只要模型目录里有对应文件就能换：

```sh
python3 dictation.py --model bilingual --refine-model paraformer-zh
python3 dictation.py --model zh-14m --refine-model ""      # 关掉重识别，只留实时结果
```

---

## 四、用法

```sh
# 1. 装运行库（只需要 sherpa-onnx，自带 onnxruntime，不要 torch）
pip install --user -r requirements.txt

# 2. 下模型（走代理：HTTPS_PROXY=http://192.168.6.9:7890 bash scripts/download_models.sh）
bash scripts/download_models.sh

# 3. 开听
python3 dictation.py                                   # 麦克风
python3 dictation.py --file samples/1.wav               # 文件（仓库自带两段测试音频）
python3 dictation.py --source tcp --port 8765          # 收别的机器推来的音频
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--model` | 流式模型名或模型目录（`small-bilingual` / `bilingual` / `zh-14m` / `ctc-zh` / `paraformer-zh`） |
| `--refine-model` | 整句重识别用的非流式模型，空串关闭 |
| `--source` | `mic` / `file` / `stdin` / `tcp` |
| `--file` `--realtime` | 听写文件；`--realtime` 按 1 倍速喂，用来验证实时链路 |
| `--device` | ALSA 设备名，默认 `default`；多麦机器用 `plughw:1,0` |
| `--out-dir` `--formats` | 输出目录与格式（`txt,md,srt,json`） |
| `--no-punct` / `--no-vad` | 关标点 / 关 VAD（改用识别器端点检测切句） |
| `--hotwords` | 热词文件，每行一个词或短语，提升专有名词准确率 |
| `--threads` | 推理线程数，默认 2 |
| `--list-devices` / `--list-models` | 列录音设备 / 列已下载模型 |

终端上正在说的半句是灰色原地刷新，说完的句子带时间戳定格：

```
[00:00:00 → 00:00:05] 这是第一种，第二种叫呃与always o s什么意思啊？
```

## 五、输出

每次听写生成一组同名文件（`transcripts/20261025-131531.*`）：

- `.txt` — 纯文本，一行一句，带起止时间戳，边说边写，Ctrl+C 不丢
- `.md` — 带抬头信息（模型、来源、句数、语音时长）的听写记录
- `.srt` — 字幕，可直接挂到视频上
- `.json` — 结构化：`{"session": {...}, "segments": [{"start", "end", "text"}]}`

## 六、程序结构

```
voice-dictation/
├── dictation.py            CLI 主程序：参数、主循环、实时显示
├── vd/
│   ├── audio.py            音频源：ALSA 麦克风 / WAV / stdin / TCP
│   ├── vad.py              Silero VAD 封装，切句
│   ├── asr.py              流式 + 非流式识别封装
│   ├── punct.py            标点恢复（CT-Transformer）
│   ├── models.py           模型登记表与模型文件自动识别
│   └── session.py          听写记录落盘（txt/md/srt/json）
├── scripts/
│   ├── download_models.sh  模型下载
│   ├── send_mic.py         另一台机器把麦克风推过来
│   └── bench.py            实测各模型 RTF
├── samples/                两段测试音频（Apache-2.0，来自 sherpa-onnx）
├── models/                 模型目录（不入库）
│   ├── asr/                各 ASR 模型
│   ├── vad/ silero_vad.onnx
│   ├── punct/              CT-Transformer 标点模型
│   └── _archives/          下载的原始压缩包（GitHub 下载很慢，留着备用）
└── transcripts/            听写记录（不入库）
```

音频源统一输出 16 kHz / 单声道 / float32，切句交给 VAD，
最终文本 = 流式结果 →（可选）非流式重识别 →（可选）标点。

## 七、坑

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

## 八、许可与来源

- 本仓库代码：Apache-2.0（见 `LICENSE`）。
- 识别模型、VAD、标点模型：来自 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
  及其 [预训练模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases)，各自 Apache-2.0；
  `scripts/download_models.sh` 只负责下载，不改动模型内容。
- `samples/` 里的测试音频同样来自 sherpa-onnx 模型包，Apache-2.0。
- 中文模型的训练数据与许可请见各模型目录里的 `README.md`。
