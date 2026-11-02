# 桌宠AI · 守护看门狗（pet_guard.ps1）
# 职责：每 30 秒探一次 webapp(8094) / 引擎(18123) / 气泡壳 / Ollama 适配器(11434)，
# 掉线自动拉起；日志滚动。共 4 段探活：
#   engine(18123) / web(8094) / shell(desktop\pet.py) /
#   adapter(11434，Ollama 协议适配器)
# 独立实现（学 Bonsai guard 的模式，代码是桌宠自己的）：
# - 只按端口探活 + 按**本项目路径**匹配残留进程（绝不按进程名无差别杀）；
# - 暂停标记 data\pet_guard_paused 存在时不自动拉起（人工维护窗口）；
# - 拉起用 .NET ProcessStart（真脱离当前会话，防"工具会话结束连坐子进程"）。
#
# 用法：powershell -File pet_guard.ps1   （随桌宠启动；关闭桌宠时调一键停止）
#       加 -MaxLoops N 只跑 N 轮后正常退出（实测/验收用，0=默认无限）
param([int]$EnginePort = 18123, [int]$WebPort = 8094, [int]$IntervalSec = 30,
      [int]$MaxLoops = 0)
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot                    # 脚本就在项目根
$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

# ---------- 启动校验：关键路径缺一就大声报错退出，别静默循环 ----------
$required = @(
  (Join-Path $root '.venv\Scripts\python.exe'),
  (Join-Path $root 'app\core\engine.py'),
  (Join-Path $root 'app\web\webapp.py')
)
$missing = @($required | Where-Object { -not (Test-Path $_) })
if ($missing.Count -gt 0) {
  Write-Output ('PET_GUARD_ABORT: 关键路径缺失，守护拒绝启动（修环境或挪目录后重试）：')
  foreach ($m in $missing) { Write-Output ('  - ' + $m) }
  try {
    $ev = Join-Path $root 'data\pet_guard_events.jsonl'
    if (Test-Path (Join-Path $root 'data')) {
      $rec = @{ ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                what = '守护启动失败：关键路径缺失 ' + ($missing -join ' ; ') } |
             ConvertTo-Json -Compress
      Add-Content $ev $rec -Encoding UTF8
    }
  } catch { }
  exit 1
}

$logs = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$guardLog = Join-Path $logs 'pet_guard.log'
$restartCount = @{ web = 0; engine = 0; shell = 0; adapter = 0 }

# ---------- 退避与失败上限 ----------
$MAX_FAIL_STREAK = 5        # 同一目标连续拉起失败 N 次 → 冷却暂停
$COOLDOWN_SEC = 600         # 冷却时长（10 分钟），期间只探活不拉起
$failStreak = @{ web = 0; engine = 0; shell = 0; adapter = 0 }
$cooldownUntil = @{ web = 0; engine = 0; shell = 0; adapter = 0 }
$backoffSec = @{ web = 5; engine = 5; shell = 5; adapter = 5 }   # 指数退避 5→10→20→40→60（封顶 60）

# Ollama 协议适配器：形象侧引擎的"本机模型"口 → 桌宠大脑 8094，端口 11434
$AdapterPort = 11434
$AdapterRoot = $root
$AdapterExe  = Join-Path $root '.venv\Scripts\python.exe'
$AdapterArgs = '-X utf8 "' + (Join-Path $root 'app\tools\ollama_adapter.py') + '"'

# 桌宠壳解释器：壳要 tkinter，优先系统 Python311；不写死路径——
# 依次探 LOCALAPPDATA\Python311 → py 启动器 → PATH
$ShellPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
if (-not (Test-Path $ShellPy)) {
  $pyl = Get-Command py -EA SilentlyContinue
  if ($pyl) { $ShellPy = $pyl.Source }         # py 启动器本体（无版本参数=默认 Python3）
  else {
    $p = Get-Command python -EA SilentlyContinue
    if ($p) { $ShellPy = $p.Source }
    else { throw "桌宠壳需要带 tkinter 的 Python（未找到 Python311/py/python，请安装后重试）" }
  }
}

$eventsFile = Join-Path $root 'data\pet_guard_events.jsonl'
$pidFile = Join-Path $root 'data\pet_guard.pid'

function Write-GuardPid {
  # 把自己的 PID 落盘（每轮刷新）——webapp 心跳按这个文件探活，
  # 彻底绕开"命令行含 pet_guard.ps1 字样"的自我匹配坑。
  try { Set-Content -Path $pidFile -Value $PID -Encoding ASCII -EA Stop } catch { }
}

function AddEvent($what) {
  # 掉线/拉起事件落 jsonl——气泡壳轮询后用气泡播报（故障可见：摔了跤要说）
  try {
    $rec = @{ ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
              what = $what } | ConvertTo-Json -Compress
    Add-Content $eventsFile $rec -Encoding UTF8
  } catch { }
}

# ---------- 日志轮转：logs 总量超上限，从最老开始清 ----------
$LOGS_CAP_MB = 300
function RotateLogs {
  try {
    $logsDir = Join-Path $root 'logs'
    if (-not (Test-Path $logsDir)) { return }
    $files = Get-ChildItem $logsDir -File -EA SilentlyContinue | Sort-Object LastWriteTime
    $total = ($files | Measure-Object Length -Sum).Sum
    if ($total -gt $LOGS_CAP_MB * 1MB) {
      $over = $total - $LOGS_CAP_MB * 1MB
      foreach ($f in $files) {
        if ($over -le 0) { break }
        $over -= $f.Length
        Remove-Item $f.FullName -Force -EA SilentlyContinue
        Log ('LOG_ROTATE: 清理 ' + $f.Name + '（logs 超上限）')
      }
    }
  } catch { }
}

function Log($msg) {
  # 整个写日志包 try——守护绝不能因写日志崩（Add-Content 遇盘满/句柄被占都会抛异常）
  try {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    $line | Add-Content $guardLog -Encoding UTF8
    # 日志滚动：超 2MB 归档为 .1（保留一代）
    if ((Get-Item $guardLog -EA SilentlyContinue).Length -gt 2MB) {
      Move-Item $guardLog ($guardLog + '.1') -Force -EA SilentlyContinue
    }
  } catch { }
}

# ---------- 守护心跳 ----------
$heartbeatLog = Join-Path $logs 'guard_heartbeat.log'
function Write-Heartbeat($engineUp, $webUp, $shellUp) {
  try {
    $llamaPid = ''
    try {
      $llama = Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -EA SilentlyContinue |
        Where-Object { $_.ExecutablePath -like '*llama-server.exe*' } |
        Select-Object -First 1 ProcessId
      if ($llama) { $llamaPid = $llama.ProcessId }
    } catch { }
    $rec = ('{0} engine={1} web={2} shell={3} llama_pid={4}' -f
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),
            $(if ($engineUp) { 'up' } else { 'DOWN' }),
            $(if ($webUp) { 'up' } else { 'DOWN' }),
            $(if ("$shellUp" -eq 'off') { 'off' } elseif ($shellUp) { 'up' } else { 'DOWN' }),
            $llamaPid)
    Add-Content $heartbeatLog $rec -Encoding UTF8
    if ((Get-Item $heartbeatLog -EA SilentlyContinue).Length -gt 2MB) {
      Move-Item $heartbeatLog ($heartbeatLog + '.1') -Force -EA SilentlyContinue
    }
  } catch { }
}

function StartHidden($exe, $argline, $cwd) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $exe
  $psi.Arguments = $argline
  $psi.WorkingDirectory = $cwd
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $null = [System.Diagnostics.Process]::Start($psi)
}

function PortUp($port) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue
  return [bool]$c
}

function ShellUp {
  # 桌宠壳探活：只认 python.exe + '-X utf8 …desktop\pet.py' 的真实壳进程；
  # 排除 -c 内联脚本——探测/测试进程的命令行文本里可能同时含这两个子串，会误报。
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue |
    Where-Object { $_.CommandLine -like '*-X utf8*desktop\pet.py*' -and
                   $_.CommandLine -notmatch '-c ' }).Count -gt 0
}

# 拉起一个目标并按退避/上限记账。$name ∈ 'engine' | 'web' | 'shell' | 'adapter'
function TryRestart($name, $downMsg, $exe, $argline, $cwd) {
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  if ($now -lt $cooldownUntil[$name]) {
    Log ($name.ToUpper() + ' 冷却中（连续失败暂停拉起，剩 ' +
         ($cooldownUntil[$name] - $now) + 's）——只探活不打扰')
    return
  }
  if ($name -eq 'web') {
    $webProcs = @(Get-CimInstance Win32_Process -EA SilentlyContinue |
      Where-Object { $_.CommandLine -match 'webapp\.py' })
    if ($webProcs.Count -gt 0) {
      Log ('web 有 ' + $webProcs.Count + ' 个进程在（可能加载中），不叠加实例，等下一轮')
      return
    }
  }
  $restartCount[$name]++
  Log ($downMsg + ' -> restart (第 ' + $restartCount[$name] + ' 次)')
  AddEvent ($downMsg + '，第 ' + $restartCount[$name] + ' 次自动拉起')
  StartHidden $exe $argline $cwd
  Start-Sleep -Seconds $backoffSec[$name]
  $up = if ($name -eq 'shell') { ShellUp } elseif ($name -eq 'adapter') { PortUp $AdapterPort } else {
    PortUp ($(if ($name -eq 'engine') { $EnginePort } else { $WebPort }))
  }
  if ($up) {
    $failStreak[$name] = 0                    # 拉起来了：连败清零
    $backoffSec[$name] = 5
  } else {
    $failStreak[$name]++
    $backoffSec[$name] = [Math]::Min(60, $backoffSec[$name] * 2)
    if ($failStreak[$name] -ge $MAX_FAIL_STREAK) {
      AddEvent ($name + ' 连续 ' + $MAX_FAIL_STREAK + ' 次拉起失败，' +
                '暂停自动拉起 ' + ($COOLDOWN_SEC / 60) + ' 分钟——请主人检查')
      Log ($name.ToUpper() + '_GIVEUP: 连续 ' + $MAX_FAIL_STREAK +
           ' 次失败，冷却 ' + $COOLDOWN_SEC + 's（人工排查后自动恢复）')
      $cooldownUntil[$name] = $now + $COOLDOWN_SEC
      $failStreak[$name] = 0
    }
  }
}

Log ('PET_GUARD_START engine=' + $EnginePort + ' web=' + $WebPort +
     ' adapter=' + $AdapterPort + ' interval=' + $IntervalSec + ' root=' + $root +
     $(if ($MaxLoops -gt 0) { ' maxloops=' + $MaxLoops + '（实测模式）' }))
Write-GuardPid

$loops = 0
while ($true) {
    $loops++
  try { RotateLogs } catch { }
  try {
    $paused = Test-Path (Join-Path $root 'data\pet_guard_paused')
    # data\pet_shell_off 存在 ⇒ 气泡壳被主人停用：不判活、不拉起、心跳记 off。
    # 每轮现查，删掉开关文件下一轮自动恢复（可逆，未动 desktop\pet.py 源码）。
    $shellOff = Test-Path (Join-Path $root 'data\pet_shell_off')
    if ($paused) {
      Start-Sleep -Seconds $IntervalSec
      continue
    }

    # ---- 引擎（18123）：掉线 → 拉起（显存预算降档由 engine.py 自己决定）----
    if (-not (PortUp $EnginePort)) {
      $py = Join-Path $root '.venv\Scripts\python.exe'
      TryRestart 'engine' '引擎掉线' $py ('-X utf8 "' + (Join-Path $root 'app\core\engine.py') + '" start') $root
    } else {
      $failStreak.engine = 0; $backoffSec.engine = 5
    }

    # ---- webapp（8094）：掉线 → 拉起 ----
    if (-not (PortUp $WebPort)) {
      $py2 = Join-Path $root '.venv\Scripts\python.exe'
      TryRestart 'web' '网页服务掉线' $py2 ('-X utf8 "' + (Join-Path $root 'app\web\webapp.py') + '"') ($root + '\app')
    } else {
      $failStreak.web = 0; $backoffSec.web = 5
    }

    # ---- 气泡壳（desktop\pet.py）：显示层两次静默消失无人管 ----
    if ($shellOff) {
      # 壳已停用：不判活、不拉起
    } elseif (-not (ShellUp)) {
      TryRestart 'shell' '气泡壳掉线' $ShellPy `
        ('-X utf8 "' + (Join-Path $root 'desktop\pet.py') + '"') $root
    } else {
      $failStreak.shell = 0; $backoffSec.shell = 5
    }

    # ---- Ollama 适配器（11434）：掉线 → 拉起 ----
    if (-not (PortUp $AdapterPort)) {
      TryRestart 'adapter' 'Ollama 适配器掉线' $AdapterExe $AdapterArgs $AdapterRoot
    } else {
      $failStreak.adapter = 0; $backoffSec.adapter = 5
    }

    # ---- 心跳：每轮记录组件状态快照 + 刷新 PID 文件 ----
    Write-Heartbeat (PortUp $EnginePort) (PortUp $WebPort) $(if ($shellOff) { 'off' } else { ShellUp })
    Write-GuardPid
  } catch {
    Log ('GUARD_LOOP_ERR ' + $_.Exception.Message)
  }
  if ($MaxLoops -gt 0 -and $loops -ge $MaxLoops) {
    Log ('PET_GUARD_TEST_DONE: 实测模式跑满 ' + $MaxLoops + ' 轮，正常退出')
    break
  }
  Start-Sleep -Seconds $IntervalSec
}
