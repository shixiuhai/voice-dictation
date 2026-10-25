# 教程：在 WSL2 上用 Windows 麦克风实时听写

本文是本仓库的**核心教程**：在这台 WSL2 机器上跑听写程序，声音从 Windows 的麦克风进来，
识别结果实时出字并按对话保存成 markdown。

---

## 0. 结论速览

| 想要的效果 | 用哪条路 | Windows 侧要装什么 |
|---|---|---|
| 最快跑起来，**什么都不装** | 方案 A：浏览器直连 | 什么都不装（Edge/Chrome 自带） |
| 音质最好、连续说话不中断 | 方案 B：Python 推流 | `pip install sounddevice numpy` |
| 不想在 Windows 装 Python | 方案 C：ffmpeg + PowerShell | `winget install Gyan.FFmpeg` |
| 整条链路都搬到 Windows | 方案 D：Windows 原生运行 | `pip install -r requirements.txt` + 模型 |

推荐先走 **方案 A**，五分钟能出声；需要长时间连续说话再换 B 或 C。

---

## 1. 为什么不能直接开麦

WSL2 不把宿主机的声卡透传进内核，所以这台机器**没有采集设备**，不是配置问题：

```
$ ls /dev/snd
crw-rw---- 1 root audio 116, 33 Oct 25 12:59 timer      # 只有定时器，没有 capture 节点

$ ls /proc/asound
ls: cannot access '/proc/asound': No such file or directory
```

于是 `--source mic` 在这台机器上一定失败。好消息是**识别端完全不用动**——
只要把 Windows 麦克风的音频流送到这台机器，后面的切句、识别、落盘都在本地跑。

---

## 2. 方案 A：浏览器直连（零安装，推荐）

思路：WSL2 起一个 WebSocket 服务并托管一个采集页面 → Windows 的 Edge/Chrome 打开这个页面 →
浏览器用 `getUserMedia` 采麦克风 → 16 kHz 单声道 PCM 通过 WebSocket 推回 WSL2。

### 2.1 WSL2 侧启动

```sh
cd /home/shixiuhahi/work/voice-dictation
python3 dictation.py --source ws --port 8765
```

输出：

```
模型: small-bilingual [transducer] 50 MB — 中英双语小型流式 zipformer，50 MB，2 核机器上约 4.3x 实时，默认模型
整句重识别: paraformer-zh [paraformer_offline] 82 MB — 中文非流式 paraformer，78 MB，适合整句重识别（校正文稿），不是流式模型
音频来源: 浏览器麦克风（http://127.0.0.1:8765/）
在 Windows 的 Edge/Chrome 里打开上面的地址，点「开始说话」即可
开始听写，Ctrl+C 结束
```

### 2.2 Windows 侧

在 Windows 的 Edge 或 Chrome 里打开：

```
http://localhost:8765/
```

> WSL2 的 localhost 转发默认是开的：Windows 的 `localhost:8765` 会转到 WSL2 里的同一个端口。

页面上点 **「开始说话」**，浏览器会弹麦克风授权框，允许即可。
然后正常说话，WSL2 的终端里就会实时出字：

```
[00:00:00 → 00:00:05] 这是第一种，第二种叫呃与always o s什么意思啊？
```

### 2.3 改记录文件名

浏览器页面顶部有「记录文件名」输入框：

```
记录文件名  [ 会议纪要        ]  [改名]
```

填完点「改名」（或回车）即时生效——**磁盘上的文件会跟着改名**，内容不丢。
WSL2 终端也会打印新的路径，页面状态栏显示 `记录文件：会议纪要-20261025-142211.md`。

命名规则固定是 `<名字>-<开始时间>`：

| 方式 | 效果 |
|---|---|
| 什么都不填 | `听写-20261025-142211.md` |
| 浏览器里填「会议纪要」 | `会议纪要-20261025-142211.md` |
| 启动时 `--out-name 周报` | `周报-20261025-142211.md` |

**为什么始终带时间戳**：名字随便起，但带上开始时间就不会撞名、不会覆盖旧记录，
按时间排也天然有序。改名字只换前缀，时间戳不动。

浏览器推过来的名字按外部输入处理：去掉路径分隔符和 `..`、过滤非法字符与控制字符、
限长 60。推 `../../etc/passwd` 只会变成 `passwd`，落在记录目录里。

### 2.4 接着写历史记录

浏览器页面上的「接着写」下拉框会列出输出目录里的历史记录（新的在前）：

```
接着写  [ 第二次评审 · 2026-10-25 14:43 · 1 句          v ]
        [ 第一次会议 · 2026-10-25 14:43 · 4 句          ]
        [ （新建记录）                                  ]
```

选一份，新说的内容就接在它后面：

- 原来那句、名称、开始时间都保留；
- **时间戳接着往下走**（上一份停在 10s，新内容就从 10s 接着记，不会回到 0）；
- `.srt` 序号连续，`.json` 段数累加。

页面状态栏会显示 `正在追加到：xxx.md（时间轴从 10.0s 接着走）`，WSL2 终端同步打印。

命令行等价物：

```sh
python3 dictation.py --list-sessions                   # 看有哪些历史记录
python3 dictation.py --append 第一次会议-20261025-144336 # 接着写这一份
```

> 选历史记录时，程序刚建好但还空着的那份会自动删掉，不在目录里留空壳。

### 2.5 收尾

- 浏览器上点「停止」→ 断开 WebSocket，WSL2 侧提示「浏览器断开，记录已实时保存」；
  或者在 WSL2 终端按 `Ctrl+C` 结束本次听写；
- 听写记录落在 `voice-dictation/transcripts/`，每次听写一组文件（见第 6 节）。

**文件是边说边写的**：每识别出一句，`.md` / `.srt` / `.json` 立刻重写一遍，
所以哪怕进程被 kill、终端被关掉，已经说过的内容都在文件里。
`Ctrl+C` 只是把这一轮收个尾、补上最后一句。

> 默认行为：浏览器断开后服务端**继续等着**，再打开页面就能接着说（同一个记录文件）。
> 想让「浏览器一断开就结束本次听写并退出」，加 `--once`。

```sh
python3 dictation.py --source ws --port 8765 --once
```

### 2.6 连不上怎么办

| 现象 | 处理 |
|---|---|
| Windows 浏览器打不开页面 | 用 WSL2 的 IP 代替 localhost。在 WSL2 里 `hostname -I` 取第一个地址（这台机器是 `172.24.246.226`），Windows 打开 `http://172.24.246.226:8765/` |
| 页面能开但点「开始说话」没反应 | 检查地址栏左侧的锁图标是否被标成「不安全」；`localhost` 属于安全上下文，换成 IP 可能被拦，此时用 `http://localhost:8765/` |
| 浏览器不弹麦克风授权 | 地址栏左侧的「网站权限」→ 麦克风改成「允许」；Edge 还要检查 `edge://settings/content/microphone` |
| 一直连不上，WSL2 端显示 `[WS] 等待…` | Windows 防火墙拦了入站；或者 WSL2 的 `localhostForwarding` 被关了（`%UserProfile%\.wslconfig` 里 `localhostForwarding=false`） |

---

## 3. 方案 B：Windows 装 Python 推流（音质/连续性最好）

适合要连续说很久、或者说话时不想被浏览器页面打断的场合。

### 3.1 Windows 侧装依赖

在 Windows 的 PowerShell 里：

```powershell
py -m pip install --user sounddevice numpy
```

> 没有 Python 就先装一个：`winget install Python.Python.3.12`（或从 python.org 下载，
> 安装时勾上 *Add python.exe to PATH*）。

### 3.2 WSL2 侧启动服务

```sh
cd /home/shixiuhahi/work/voice-dictation
python3 dictation.py --source tcp --port 8765
```

### 3.3 Windows 侧推流

把仓库里的 `scripts/send_mic.py` 拷到 Windows（或直接 `git clone` 一份到 Windows），然后：

```powershell
python scripts\send_mic.py --host 127.0.0.1 --port 8765
```

显示「已连接 …，开始推流」就可以说话了。

指定设备（`--host`、`--port` 之外）：

```powershell
python scripts\send_mic.py --device "麦克风 (Realtek High Definition Audio)"
```

连不上时把 `--host` 换成 WSL2 的 IP。

---

## 4. 方案 C：ffmpeg + PowerShell（Windows 侧不装 Python）

### 4.1 装 ffmpeg

```powershell
winget install Gyan.FFmpeg
```

（装完要重开一个 PowerShell 窗口，`ffmpeg` 才会进 PATH。）

### 4.2 列出麦克风设备

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows_mic_push.ps1 -List
```

### 4.3 推流

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows_mic_push.ps1 -Device "麦克风 (Realtek High Definition Audio)"
```

脚本用 ffmpeg 的 `dshow` 采集，转成 16 kHz 单声道 s16le，通过 TCP 推给 WSL2。
WSL2 侧同样跑 `python3 dictation.py --source tcp --port 8765`。

---

## 5. 方案 D：整条链路搬到 Windows

识别不再经过 WSL2，Windows 端自己跑：

```powershell
git clone https://github.com/shixiuhai/voice-dictation.git
cd voice-dictation
py -m pip install -r requirements.txt
bash scripts/download_models.sh      # Windows 上用 Git Bash；没有的话照着 README 手动下
py dictation.py                      # --source mic 会自动用 sounddevice 后端
```

好处是彻底没有跨机延迟；代价是模型要在 Windows 上再存一份（约 1.3 GB）。

---

## 6. 输出：按对话保存的 markdown

每次听写生成一组同名文件，`transcripts/听写-20261025-140233.*`（名字可配，见 2.3）：

- `.md` — **按对话分段**的听写记录（下面就是它的样子）
- `.txt` — 纯文本，一行一句，带起止时间戳，边说边写
- `.srt` — 字幕，可直接挂到视频上
- `.json` — 结构化数据

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

每一段就是一次「对话」（一句话），带起止时间戳，Markdown 标题层级可以直接生成大纲。

---

## 7. 排查表

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| WSL2 端一直不打印字 | 音频没进来 | 看终端有没有 `[WS] 浏览器已连接` / `[TCP] 已连接` |
| 浏览器页面上一直转圈/反复重连 | 服务端还在加载模型 | 正常，等几秒自动连上（页面会自己重试）；模型加载约 5 秒 |
| 有连接但一句都不出 | 麦克风没声音 / 增益太低 | Windows 声音设置里把输入音量拉高；页面上确认输入设备选的是麦克风而不是扬声器 |
| 识别出的是英文或乱码 | 送的是系统声音不是人声 | 确认浏览器授权的是麦克风；Chrome 的「使用耳机」模式关掉 |
| 中文识别成拼音/大写 | 用了只支持中文的模型 | `--model small-bilingual`（默认就是它） |
| 每句都被截断 | 整句重识别的模型没开 | 默认开着；`--refine-model ""` 才会关 |
| 断句太碎 | VAD 参数太敏感 | 目前 VAD 阈值写死在 `vd/vad.py`（`min_silence_duration=0.3`），可以调大到 0.5~0.8 |
| 听写记录是空的 | 麦克风权限没给 | 浏览器地址栏左侧的网站权限 → 麦克风改成「允许」，再点一次「开始说话」 |

---

## 8. 附录：本机环境事实（实测）

| 项 | 值 |
|---|---|
| WSL2 IP | `172.24.246.226`（`hostname -I` 取第一个） |
| Windows 宿主在 WSL2 视角下的地址 | `172.24.240.1`（默认网关） |
| Windows 版本 | Windows 11，build 22631 |
| Windows 侧已有 | Edge / Chrome、`curl.exe`；**没有** Python、**没有** ffmpeg |
| WSL2 侧已有 | Python 3.13.12、sherpa-onnx 2.0.0、模型全套 |
| 免 root 的 pulse 客户端 | `~/.local/opt/pulse/bin`（`parec` / `pactl`），已加进 `~/.bashrc` 的 PATH |
| WSLg 的 Pulse 采集源 | `RDPSource`（`module-rdp-source`），可用 `--source pulse --device RDPSource` 抓 Windows 会话音频 |

WSL2 ↔ Windows 的连通性：

- **Windows → WSL2**：`127.0.0.1:<端口>`（localhost 转发，默认开）；不行就用 WSL2 IP。
- **WSL2 → Windows**：走默认网关 `172.24.240.1`（本机从 WSL2 连 445 是通的）。
