# AGENTS.md — voice-dictation 环境记忆

给在这个仓库里干活的 AI agent 看。事实都是实测的，命令都跑过。

## 1. 这台机器是什么

| | |
|---|---|
| 系统 | Kali GNU/Linux Rolling 2026.2，**WSL2**（内核 `6.18.54.1-microsoft-standard-WSL2`） |
| CPU / 内存 | 2 vCPU x86_64 / 3.9 GB，无 swap，无 GPU |
| **内存预算（实测）** | 全部组件在线（VAD+AGC+流式ASR+重识别+标点）进程 RSS 324 MB，加 GTCRN 降噪 333 MB；系统可用还剩 2.1 GB。**内存完全不是瓶颈，卡的是 2 核 CPU —— RTF 才是唯一硬指标** |
| Windows 宿主 | Windows 11 build 22631，`/mnt/c` 已挂载 |
| Python | 3.13.12；sherpa-onnx 2.0.0 装在 `~/.local/lib/python3.13/site-packages`（`pip install --user --break-system-packages`） |
| 仓库位置 | `/home/shixiuhahi/work/voice-dictation`（独立 git 仓库，remote: `ssh://git@ssh.github.com:443/shixiuhai/voice-dictation.git`） |

## 2. 没有麦克风，别浪费时间找

```
$ ls /dev/snd        → 只有 timer，没有 capture 节点
$ ls /proc/asound    → 不存在
```

WSL2 不透传宿主机声卡。`--source mic` 在这台机器上必然失败，
**不要试图用 `aplay`/`arecord`/改配置去打开它**——那是内核层面的事。

能用的取流方式（都已实测）：

| 方式 | 命令 |
|---|---|
| 浏览器麦克风（Windows 侧零安装，首选） | `python3 dictation.py --source ws --port 8765`，Windows 打开 `http://localhost:8765/` |
| Windows 麦克风推流 | WSL2 `python3 dictation.py --source tcp --port 8765`；Windows `python scripts/send_mic.py --host 127.0.0.1 --port 8765` |
| Windows 会话音频 | `python3 dictation.py --source pulse --device RDPSource`（WSLg 的 RDP 采集源） |
| 文件 | `python3 dictation.py --file samples/1.wav` |

> **WSL2 的 IP 只能看页面，不能录音**：`getUserMedia` 只在安全上下文
> （`https` 或 `localhost`/`127.0.0.1`）下可用。`http://172.24.x.x:8765/`
> 能打开页面但麦克风开不起来——这是浏览器规则，不是本程序限制。
> 录音必须走 `localhost` / `127.0.0.1`。

## 3. 网络

- **Windows → WSL2**：`127.0.0.1:<端口>`（localhost 转发默认开）。不通就换 WSL2 IP：`hostname -I` 取第一个（这台是 `172.24.246.226`）。
- **WSL2 → Windows**：默认网关 `172.24.240.1`。
- **GitHub**：22 端口被墙，走 `ssh.github.com:443`；`skill-key`（`~/skill-key`）已注册在 GitHub 账号 shixiuhai。仓库里已配好 `core.sshCommand`。
- **代理**：`http://192.168.6.9:7890`（`HTTPS_PROXY` 环境变量用）。GitHub release 大文件仍会限速到 ~130 KB/s。

## 4. 模型

在 `models/`（不入库，`scripts/download_models.sh` 下载）：

| 名字 | 类型 | 体积 | RTF（实测，2 线程） |
|---|---|---|---|
| `small-bilingual`（默认） | 流式 zipformer 中英双语 | 50 MB | 0.388 |
| `bilingual` | 流式 zipformer 中英双语 | 198 MB | **1.565（跟不上实时）** |
| `zh-14m` | 流式 zipformer 中文 | 25 MB | 0.234 |
| `ctc-zh` | 流式 CTC 中文 | 26 MB | 0.361 |
| `paraformer-zh` | **非**流式（整句重识别） | 82 MB | 0.461 |
| `sense-voice` | **非**流式 SenseVoice 多语种（中英日韩粤） | 165 MB | 待测 |
| `x-asr` | 流式 zipformer2 中英双语（480ms chunk）**自带标点/大小写** | 155 MB | 短音频 0.90；223s 真实电视剧音频 **1.15（略慢）** |

复现：`python3 scripts/bench.py`（测试音频集是固定的，不同次跑可横向比较）。

**整条链路的实时性**（60s 真实电视剧音频，2 核，**预热后**）：
流式单独 0.366、含重识别+标点 **0.439 → 约 2.3x 实时，跟得上**。
⚠️ **首次推理要预热**（含 ONNX 图初始化），单个 chunk 冷启动能测出 RTF 1.2 的假结论。

**别把 `bilingual`（198 MB）当默认**：RTF 1.565 跟不上实时，而且实测识别结果
并不比 50 MB 的 `small-bilingual` 更好。体积大 ≠ 更准。

`sense-voice` 值得关注：调研显示它**在背景有音乐/噪声的环境下掉点最小**，
正是「电视/远场」场景该试的模型。用它做重识别：`--refine-model sense-voice`
（`vd/asr.py` 会剥掉输出里的 `<|zh|><|NEUTRAL|>` 标记）。

**默认是两段式**：流式模型出实时结果，句末用 `paraformer-zh` 重跑整句。
流式模型会漏字、会偶发空结果，重识别这一遍就是用来兜底的——
去掉重识别（`--refine-model ""`）质量会明显下降。

**另一条路：单模型 final 模式**（`--commit final` + `--model x-asr`）。
只用一条流式识别流吃连续音频、永不切段重置，靠「假设文本的稳定前缀」判断
定型（`vd/final.py`），定型就落盘、不再改。好处：每句都有左上下文（VAD 的
段边界会丢字）、不用第二个模型、标点是模型词表自带的。模型的 `tokens.txt`
里带标点就算「自带标点」(`models.py:_tokens_have_punct`)，main 里据此不再挂
标点模型。RTF 见上表（长音频略超 1，是这条路的主要代价）。

## 5. 免 root 装的东西

- `~/.local/opt/pulse/`：`parec` / `pactl` / `pacat`（apt 下 deb 再 `dpkg-deb -x` 解出来的），
  包装脚本写死了 `LD_LIBRARY_PATH` 和 `PULSE_SERVER=unix:/mnt/wslg/runtime-dir/pulse/native`。
  `~/.local/opt/pulse/bin` 已加进 `~/.bashrc` 的 PATH。
  系统级替代：`sudo apt install pulseaudio-utils`。

## 5. 行为约定（改代码前要知道）

- **输出是实时落盘的**：每加一句就重写一遍 `.md` / `.srt` / `.json`（`.txt` 是追加）。
  进程被 kill 也不丢内容。`Ctrl+C` 只是收尾并补上最后一句。
- **文件名 = `<名字>-<开始时间>`**（`听写-20261025-142211.md`）。名字有三处来源，
  优先级：浏览器页面输入框（`{"cmd":"name","name":"..."}` 文本帧）> `--out-name` > 默认「听写」。
  改名只换前缀、时间戳不动，所以不会撞名。
  **浏览器推来的名字是外部输入**，`vd/session.py:sanitize_name()` 负责消毒
  （去路径分隔符与 `..`、过滤非法字符与控制字符、限长 60）——别绕过它。
- **可以追加到历史记录**：浏览器下拉框（`{"cmd":"append","stem":"..."}`）或 `--append <stem>`。
  `TranscriptWriter.attach()` 会把旧内容读回来（句子/名称/开始时间），
  并返回**时间偏移**（旧记录最后一句的结束时间），引擎把它加到所有新时间戳上
  （`engine._offset`）——所以接续记录的时间轴是连续的，不会回到 0。
  `.srt` 序号、`.json` 段数由内存里的完整段列表决定，因此也是连续的。
  旧记录读不回来时会退化到解析 `.txt` / `.md`（`load_session()` 三级兜底）。
- **`--source ws` 断开后继续等下一个客户端**（`--once` 才退出）。这是有意的：
  浏览器关掉再打开能接着说，同一个记录文件。
- **`--source tcp` 也一样**：客户端断开后继续等下一个（`--once` 才退出），
  客户端崩了不用重启服务端重新加载模型。客户端断开时服务端会**补 0.5 秒静音**，
  让 VAD 把当前这句结掉（下一句从头开始），不然最后一句永远出不来。
- **TCP 源当字节流看，不假设客户端送来的一定对齐**：多出来的半个采样先攒着，
  凑成整采样再送进识别器；流结束时剩下那半个采样丢掉（以前直接 `np.frombuffer`
  报 `buffer size must be a multiple of element size`，整个听写进程崩掉）。
  `stdin` 源有同样的坑，也修了。
- **TCP 源能直接收 WAV 文件**（`cat a.wav | nc`）：开头是 RIFF 头就跳过，
  采样率不是 16 kHz 顺手线性重采样，立体声降混。头没收全就继续攒字节。
- **端口先绑定、模型后加载**：`main()` 里先 `build_source()`（`ws`/`tcp` 在这里 bind），
  再加载模型。否则浏览器打开得太早会连不上。现在页面还会自己重试，两边都兜住。
- **ws 源的 HTTP 与 WS 是分开的线程**（`vd/wsserver.py`）：
  HTTP 线程在 `MicWebServer.__init__` 就起来了，所以**页面秒开**（实测 2 ms，
  不用等 13~16 秒的模型加载），页面自己轮询 `/api/status` 看模型就绪没；
  就绪前 WS 握手返回 503。HTTP 和 WS 各占一个连接线程，多标签页互不阻塞。
  改协议时注意：客户端→服务端的帧是带掩码的；不支持分片扩展与压缩。
- **WS 源上服务端也会往浏览器推消息**：半句 `{"cmd":"partial"}`、
  定稿句 `{"cmd":"segment"}`、状态 `{"cmd":"status"}`、连上时的
  `{"cmd":"hello"}`（带历史 transcript 和会话清单）。引擎侧由
  `DictationEngine._push()` → `AudioSource.push_event()` 转发。
- **麦克风不抢**：`wsserver._push_audio()` 里先到的客户端一直占着麦克风，
  后来的只收到 `{"cmd":"superseded"}`，不会两个页面来回抢音频。
- **浏览器端用 ScriptProcessorNode**（已废弃但 Chrome/Edge 仍支持），
  采样率不是 16 k 时在页面里做线性重采样；接了一个 gain=0 的节点再连 destination，
  否则 onaudioprocess 不会被调度，且会把麦克风声音放出来。
- **页面的状态条是唯一的对外口径**（`vd/mic_client.html` 的 `updateStatus()`）：
  所有提示都从这一个函数出去，临时提示走 `say()`（带过期时间，几秒后自动回落到
  常态文案）。**「开始说话」按钮任何时候都可点**，不再 `disabled`——
  点了必须要有回音（模型没就绪 / 正在连服务 / 正在等授权 / 地址不对 / 权限被拒），
  「点了没反应」是这个页面最严重的回归。
- **页面自己判断能不能用麦克风**：`window.isSecureContext` 为 false 时
  `navigator.mediaDevices` 整个是 `undefined`，`getUserMedia` 会**同步抛异常**，
  异常会逃出事件处理函数、被浏览器吞掉，表现为「点了没反应」。
  所以 `startMic()` 里 `getUserMedia` 必须用 `try/catch` 包住，
  并在页面上用「麦克风可用 / 不可用」标签提前提示（IP 地址访问就是这样）。
- **AudioContext 可能是 suspended**：`ws.onopen` 回调不是用户手势，
  在那里创建的 `AudioContext` 可能停在 `suspended`，那样会一直在采空气、
  一句都不出。`setupAudio()` 里要显式 `ctx.resume()`。
- **音源太轻是「识别不出来」的头号原因，实测过两次**：
  Silero VAD 的 `threshold` 判的是「语音概率」而不是电平，输入一轻，
  概率整体偏低、阈值跨不过去，表现是**一句都不出**。实测 `samples/1.wav`
  逐级衰减：-55 dBFS 开始漏字，-61 dBFS 起 0 句；补上增益后到 -79 dBFS 仍正常。
  用户真实录的电视剧音频（`/tmp/vd_tv_system.wav`，示意）RMS -47.6 / 峰值 -27.6 dBFS，
  VAD 覆盖率只有 **9%**；归一化后覆盖 59.7s/60s。
- **电平归一化默认开着**（`vd/audio.py:LevelNormalizer`，`--no-agc` 关，`--agc-max-gain` 默认 64 = +36 dB）：
  只做两件事：**只在整体偏轻时才补**（最近 5 秒内出现过 ≥0.1 的峰值就锁死在 1.0，
  语音帧逐采样不变），**只补不削**（增益夹在 [1, max_gain]）。
  降增益立刻生效（防削顶）、升增益每帧最多 2 倍（防抽吸）。
  别改成「瞬时电平对目标值归一化」——那样正常语音句间的停顿会被补上近 20 倍
  增益，`today is 礼拜二` 会识别成 `today is li 巴 a`（踩过这个坑）。
- **连续有声的音频（电视/直播）VAD 切不出句，只能靠 `max_speech_duration` 兜底**：
  实测电视剧音频上 `min_silence_duration` 从 0.3 调到 0.15 **完全没作用**（16000 Hz
  下 Silero VAD 的窗口只有 32ms，0.15s 根本不是有效档位），segment 数一个不变。
  真正起作用的是 `--vad-max-speech`：默认 30s 时 60 秒只剩 2 段、每段 29.8s
  （要等 30 秒才出第一句，看起来就像「不识别」）；调到 8s 出 7 段，
  首句约 8 秒就出来，识别准确率没有损失（实测 8s/12s 的 CER 都在 5~7%）。
  所以默认改成 **8 秒**。
- **GTCRN 流式降噪在这台机器上不能用**：合成音频上它把相似度从 0.58 拉到 0.88，
  但真实电视剧音频上 RTF = **1.29（比实时还慢）**，而且降完电平没变
  （降噪模型会调整音量，但 `healthy_peak` 是绝对阈值，归一化器只看原始电平），
  VAD 覆盖率反而 13% → 5%。**用户要真打算上降噪，得先解决 RTF**：
  要么把降噪放到独立进程/错峰，要么只对 VAD 判定用降噪后的音频。
- **VAD 切句比「当前段」短，连续音频会丢 12~20%**：实测 45s 连续语音+底噪，
  定稿段只覆盖 80~88% 的音频。VAD 门槛一低（`min_silence_duration` 0.6）
  丢得少一些，但切句变粗。这是 VAD 的固有行为，不是 bug。
- **VAD 默认值**（`vd/vad.py`）：threshold 0.35、min_silence 0.3s、
  max_speech **8s**，命令行可调（`--vad-threshold` / `--vad-silence` / `--vad-max-speech`）。
  阈值 0.35 比 sherpa 默认的 0.5 更容易切出句子，代价是底噪多时可能多切几句。
- **`finalize()` 会先补 100 ms 静音**：输入突然断掉时识别器还欠着音频没吐字。
  注意这段静音也要过一遍归一化器（否则静音被放大成假语音）。
- **页面的主题是 CSS 变量**（`vd/mic_client.html`）：`:root` 是深色，
  `[data-theme="light"]` 覆盖同一批变量（30 个，两边一一对应）。
  规则里**不要再写死颜色**——加新颜色先加变量，两套主题都要给值。
  `<head>` 里有段小脚本在渲染前把 `data-theme` 定下来（读 `localStorage`，
  没有就跟随 `prefers-color-scheme`），所以刷新时不会闪一下白；
  用户手动选过之后就跟系统无关了。
- **`scripts/ws_probe.py`** 是不开浏览器时模拟浏览器的客户端（手搓 WS 帧），
  用来验证服务端链路。推流末尾要补静音，否则 VAD 不切句、看不到定稿句。
- **final 模式（`--commit final`，`FinalEngine` + `vd/final.py`）**：
  - **定型判定 = 局部一致性**：拿当前假设和 `margin`（默认 1s）**之前**的假设
    比公共前缀，别拿「当前 vs 上一次」比——流式假设是单调追加的，上一次的公共
    前缀永远等于上一次全长，那样永远判不出定型（实测连续 30s 只出 1 句，
    其余全堆到 finalize）。跟历史比，才能露出中间被改写的部分。
  - **靠模型自带标点断句**：`x-asr` 的词表带标点，`spec.has_punct` 为真时
    main 不再挂标点模型（再挂会重复加标点）。句末标点断的句 `uncertain=False`，
    靠停顿/超时断的 `uncertain=True`。
  - **晚一句落盘**：断句常落在标点前面，`FinalEngine._emit` 暂存上一句，等新句
    开头粘的标点挪回来再写。`.txt`/`.md`/`.srt`/`.json` 保持内容一致。
  - **换客户端要重置**：ws/tcp 断开后还能接下一个，`FinalCommitter` 那条流不能
    跨连接复用（否则上一条连接的残留文本会串台）。`AudioSource.session` 每换一个
    麦克风客户端 +1，`FinalEngine.run` 检测到变化就 `_reset()`——先 finalize 收尾
    落盘（但 `_mute_push=True`，不推给新客户端），再重建 committer。
  - **断连补静音**：ws 客户端断开时服务端补 0.5s 静音（`wsserver._TAIL_SILENCE`），
    逼出最后一句，跟 TCP 源一个道理。

## 6. 改代码前先知道的坑

- **VAD 的「已完成段」比「当前段」短**（尾部静音被裁掉）。段结束时**不要**再补喂音频，
  否则同一段被识别两遍（会出现「昨天是MONDAY，昨天是MONDAY。」）。
  正确做法：用 `current_segment.start` 跟踪当前段。
- **`vad.is_speech_detected` 是方法不是属性**，少写括号 → 拿到 bound method → `if` 恒为真。
- **VAD 一旦 `flush()` 过就不再认新音频**（sherpa-onnx 的行为，实测）：
  之后再喂多少音频，`is_speech_detected()` 都是 False，`current_segment.start` 永远是 -1。
  `reset()` 能救回来，但**采样计数器会归零**（段起点又从 0 开始算，时间戳就乱了）。
  所以中途要收尾就**补一段静音**（0.5 秒就够，VAD 的 `min_silence_duration` 是 0.3），
  让它自然结段，别 flush / reset。
- **`sherpa_onnx.Alsa` 打开失败会直接 `exit(-1)`**（C++ 侧行为，Python 拦不住）。
  必须在开设备之前先用 `/proc/asound/pcm` 探测有没有采集设备。
- **带热词必须用 `modified_beam_search`**，sherpa-onnx 会直接抛错（程序里已自动切换）。
- **8 kHz 音频**会在 stderr 打出 `Creating a resampler`，不是报错。
- **ws 源的错误分支要小心**：`wsserver._reply()` 的 body 必须是 bytes。
  传了 str 会抛 `TypeError`，而连接处理器会把异常吞掉——表现是下载接口直接断连接
  （curl 看到 000 而不是 400），排查起来很绕。
- **`OnlineRecognizer.from_transducer` 的 `model_type` 不能写 `"transducer"`**：
  那会打一行 `Invalid model_type: transducer` 的警告（能跑，但每次加载都刷）。
  普通 zipformer 传空串让 sherpa-onnx 自己推断，只有 X-ASR 系（zipformer2 布局）
  才显式传 `"zipformer2"`。`ModelSpec.model_type` 默认就是空串。
- **X-ASR 系是 BPE 词表 + zipformer2 布局**：`models.py` 若发现目录里有
  `bpe.model` 就把 `modeling_unit` 设成 `bpe` 并带上 `bpe_vocab`；目录名含
  `x-asr` 时 `model_type="zipformer2"`。它的输出汉字之间带空格（`昨 天 是`），
  `final.normalize_cjk_spacing()` 负责去空格、并把中英文标点前后的空格也收干净。

## 7. 提交与推送

- 独立仓库，main 分支，remote 指向 GitHub。
- 提交信息用中文，描述「改了什么 + 为什么」，参考已有 commit 的写法。
- 推送：`git push origin main`（`core.sshCommand` 已配好 443 端口和 key）。
