# AGENTS.md — voice-dictation 环境记忆

给在这个仓库里干活的 AI agent 看。事实都是实测的，命令都跑过。

## 1. 这台机器是什么

| | |
|---|---|
| 系统 | Kali GNU/Linux Rolling 2026.2，**WSL2**（内核 `6.18.54.1-microsoft-standard-WSL2`） |
| CPU / 内存 | 2 vCPU x86_64 / 3.9 GB，无 swap，无 GPU |
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

## 3. 网络

- **Windows → WSL2**：`127.0.0.1:<端口>`（localhost 转发默认开）。不通就换 WSL2 IP：`hostname -I` 取第一个（这台是 `172.24.246.226`）。
- **WSL2 → Windows**：默认网关 `172.24.240.1`。
- **GitHub**：22 端口被墙，走 `ssh.github.com:443`；`skill-key`（`~/skill-key`）已注册在 GitHub 账号 shixiuhai。仓库里已配好 `core.sshCommand`。
- **代理**：`http://192.168.6.9:7890`（`HTTPS_PROXY` 环境变量用）。GitHub release 大文件仍会限速到 ~130 KB/s。

## 4. 模型

在 `models/`（不入库，`scripts/download_models.sh` 下载）：

| 名字 | 类型 | 体积 | RTF（实测，2 线程） |
|---|---|---|---|
| `small-bilingual`（默认） | 流式 zipformer 中英双语 | 50 MB | 0.233 |
| `bilingual` | 流式 zipformer 中英双语 | 198 MB | 0.383 |
| `zh-14m` | 流式 zipformer 中文 | 25 MB | 0.148 |
| `ctc-zh` | 流式 CTC 中文 | 26 MB | 0.085 |
| `paraformer-zh` | **非**流式（整句重识别） | 82 MB | 0.121 |

复现：`python3 scripts/bench.py`（测试音频集是固定的，不同次跑可横向比较）。

**默认是两段式**：流式模型出实时结果，句末用 `paraformer-zh` 重跑整句。
流式模型会漏字、会偶发空结果，重识别这一遍就是用来兜底的——
去掉重识别（`--refine-model ""`）质量会明显下降。

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
- **端口先绑定、模型后加载**：`main()` 里先 `build_source()`（`ws`/`tcp` 在这里 bind），
  再加载模型。否则浏览器打开得太早会连不上。现在页面还会自己重试，两边都兜住。
- **`--source ws` 用的 WebSocket 服务端是自己手写的最小实现**（`vd/wsserver.py`）：
  HTTP/1.1 升级握手 + 客户端带掩码的二进制帧，没有第三方依赖。
  改协议时注意：客户端→服务端的帧是带掩码的；不支持分片扩展与压缩。
- **浏览器端用 ScriptProcessorNode**（已废弃但 Chrome/Edge 仍支持），
  采样率不是 16 k 时在页面里做线性重采样；接了一个 gain=0 的节点再连 destination，
  否则 onaudioprocess 不会被调度，且会把麦克风声音放出来。

## 6. 改代码前先知道的坑

- **VAD 的「已完成段」比「当前段」短**（尾部静音被裁掉）。段结束时**不要**再补喂音频，
  否则同一段被识别两遍（会出现「昨天是MONDAY，昨天是MONDAY。」）。
  正确做法：用 `current_segment.start` 跟踪当前段。
- **`vad.is_speech_detected` 是方法不是属性**，少写括号 → 拿到 bound method → `if` 恒为真。
- **`sherpa_onnx.Alsa` 打开失败会直接 `exit(-1)`**（C++ 侧行为，Python 拦不住）。
  必须在开设备之前先用 `/proc/asound/pcm` 探测有没有采集设备。
- **带热词必须用 `modified_beam_search`**，sherpa-onnx 会直接抛错（程序里已自动切换）。
- **8 kHz 音频**会在 stderr 打出 `Creating a resampler`，不是报错。

## 7. 提交与推送

- 独立仓库，main 分支，remote 指向 GitHub。
- 提交信息用中文，描述「改了什么 + 为什么」，参考已有 commit 的写法。
- 推送：`git push origin main`（`core.sshCommand` 已配好 443 端口和 key）。
