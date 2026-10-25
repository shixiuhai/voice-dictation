<#
.SYNOPSIS
    在 Windows 上把麦克风音频推到 WSL2 里的听写程序（不需要装 Python）。

.DESCRIPTION
    用 ffmpeg 的 dshow 设备采集麦克风，转成 16 kHz 单声道 s16le，
    通过 TCP 送到 WSL2 的听写服务（python3 dictation.py --source tcp --port 8765）。

    WSL2 的 localhost 转发默认开着，所以 Server 一般用 127.0.0.1；
    如果连不上，用 WSL2 的 IP（在 WSL2 里执行 `hostname -I` 取第一个）。

.PARAMETER Server
    听写服务地址，默认 127.0.0.1。

.PARAMETER Port
    听写服务端口，默认 8765。

.PARAMETER Device
    ffmpeg dshow 里的设备名，例如 "麦克风 (Realtek High Definition Audio)"。
    不填就只列设备。

.PARAMETER List
    列出所有可用的音频输入设备后退出。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows_mic_push.ps1 -List

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows_mic_push.ps1 -Device "麦克风 (Realtek High Definition Audio)"
#>
param(
    [string]$Server = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Device = "",
    [switch]$List
)

$ErrorActionPreference = "Stop"

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) {
    Write-Host "找不到 ffmpeg。装一个：winget install Gyan.FFmpeg" -ForegroundColor Red
    Write-Host "（或从 https://www.gyan.dev/ffmpeg/builds/ 下载后把 bin 目录加进 PATH）"
    exit 1
}

if ($List -or -not $Device) {
    Write-Host "== 可用的音频输入设备 ==" -ForegroundColor Cyan
    $out = & $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1
    $names = $out | Select-String -Pattern '"(.+)"' | ForEach-Object {
        if ($_ -match '"(.+)"') { $Matches[1] }
    }
    $names | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "用法：加 -Device `"上面的名字`" 即可，例如" -ForegroundColor DarkGray
    Write-Host '  .\windows_mic_push.ps1 -Device "麦克风 (Realtek High Definition Audio)"' -ForegroundColor DarkGray
    if (-not $Device) { exit 0 }
}

Write-Host "正在连接 $Server`:$Port ..." -ForegroundColor Cyan
$client = New-Object System.Net.Sockets.TcpClient
$client.Connect($Server, $Port)
$netStream = $client.GetStream()
Write-Host "已连接，开始推流（Ctrl+C 停止）" -ForegroundColor Green

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $ffmpeg
$psi.Arguments = "-hide_banner -loglevel error -f dshow -i audio=`"$Device`" -ar 16000 -ac 1 -f s16le pipe:1"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true

$proc = [System.Diagnostics.Process]::Start($psi)
$buf = New-Object byte[] 32000
try {
    while ($true) {
        $n = $proc.StandardOutput.BaseStream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $netStream.Write($buf, 0, $n)
        $netStream.Flush()
    }
}
finally {
    if (-not $proc.HasExited) { $proc.Kill() }
    $client.Close()
}
