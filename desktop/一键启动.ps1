# 桌宠AI · 一键启动（标准入口：桌面快捷方式指向这里）
# 顺序：气泡壳 → 引擎 → webapp → 守护。全幂等，失败不静默。
# 红线：不做任何开机自启/计划任务/注册表 Run——这个脚本只被双击触发。
# - webapp/守护启动前查【进程】而非只查端口（防加载中叠加第二个实例）；
# - 启动前清 pet_guard_paused 暂停标记（与一键停止配对：停止时写、启动时清）。
$root = Split-Path -Parent $PSScriptRoot        # 本脚本在 desktop\ 下，项目根是上一级
$py = Join-Path $root ".venv\Scripts\python.exe"
$eventsFile = Join-Path $root "data\pet_guard_events.jsonl"

function AddEvent($what) {
  try {
    @{ ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); what = $what } |
      ConvertTo-Json -Compress | Add-Content $eventsFile -Encoding UTF8
  } catch { }
}
function PortUp($p) {
  [bool](Get-NetTCPConnection -LocalPort $p -State Listen -EA SilentlyContinue)
}
function ProcAlive($pattern) {
  # 按命令行子串查进程数（只匹配本项目特征，绝不按进程名无差别算）
  @(Get-CimInstance Win32_Process -EA SilentlyContinue |
    Where-Object { $_.CommandLine -match $pattern }).Count
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

# ⓪ 清除守护暂停标记（一键停止写入；有残留会让拉起的守护只睡不干活）
Remove-Item (Join-Path $root 'data\pet_guard_paused') -Force -EA SilentlyContinue

# ① 气泡壳（秒起，先让桌宠出现在桌面）
# data\pet_shell_off 存在 ⇒ 壳被主人停用，跳过不启动。
if (Test-Path (Join-Path $root 'data\pet_shell_off')) { Write-Output "① 气泡壳已被 pet_shell_off 关闭，跳过（删 data\pet_shell_off 恢复）" }
elseif ((Get-CimInstance Win32_Process -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'desktop\\pet\.py' })) { Write-Output "① 桌宠已在桌面，跳过" }
else {
  # 壳要 tkinter：优先系统 Python311，逐级探测（不写死路径）
  $shellPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
  if (-not (Test-Path $shellPy)) {
    $pyl = Get-Command py -EA SilentlyContinue
    if ($pyl) { $shellPy = $pyl.Source }
    else {
      $p = Get-Command python -EA SilentlyContinue
      if ($p) { $shellPy = $p.Source }
      else { Write-Output "   ⚠ 未找到带 tkinter 的 Python，壳起不来（装 Python311 或加 PATH）" }
    }
  }
  if (Test-Path $shellPy -EA SilentlyContinue) {
    StartHidden $shellPy `
      ('-X utf8 "' + (Join-Path $root 'desktop\pet.py') + '"') $root
  }
  Write-Output "① 白猫已蹲上桌面"
}

# ② 引擎
if (PortUp 18123) { Write-Output "② 引擎已在跑，跳过" }
else {
  Write-Output "② 启动引擎（约 30~90 秒加载）…"
  StartHidden $py ('-X utf8 "' + (Join-Path $root 'app\core\engine.py') + '" start') $root
  $up = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    if (PortUp 18123) { $up = $true; Write-Output "   引擎就绪（$(($i+1)*3)s）"; break }
  }
  if (-not $up) {
    $msg = "启动桌宠时引擎 120 秒未就绪（看 logs\engine_stderr.log，常见是显存被占）"
    Write-Output "   ⚠ $msg"
    AddEvent $msg                       # 不静默：面板/气泡可见
  }
}

# ③ webapp（幂等加固：进程在但端口没监听 = 加载中，不叠加实例）
if (PortUp 8094) { Write-Output "③ webapp 已在跑，跳过" }
elseif ((ProcAlive 'app\\web\\webapp\.py') -gt 0) {
  Write-Output "③ webapp 进程已存在（加载中），不叠加实例——等下一轮/看日志"
}
else {
  Write-Output "③ 启动 webapp…"
  StartHidden $py ('-X utf8 "' + (Join-Path $root 'app\web\webapp.py') + '"') (Join-Path $root 'app')
  Start-Sleep -Seconds 4
  if (PortUp 8094) { Write-Output "   webapp 就绪：http://127.0.0.1:8094" }
  else {
    $msg = "启动桌宠时 webapp 未能起来（看 logs\webapp_err.log）"
    Write-Output "   ⚠ $msg"
    AddEvent $msg
  }
}

# ④ 守护（掉线自愈；PID 文件在 = 守护循环活着，命令行匹配兜底旧版）
$guardUp = $false
try {
  $gpid = (Get-Content (Join-Path $root 'data\pet_guard.pid') -EA SilentlyContinue) -as [int]
  if ($gpid) {
    $gp = Get-Process -Id $gpid -EA SilentlyContinue
    if ($gp -and $gp.ProcessName -eq 'powershell') { $guardUp = $true }
  }
} catch { }
if (-not $guardUp) {
  $guardUp = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and
                   $_.CommandLine -like '*-File*pet_guard.ps1*' -and
                   $_.CommandLine -notmatch '-Command' }).Count -gt 0
}
if ($guardUp) { Write-Output "④ 守护已在跑，跳过" }
else {
  StartHidden 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
    ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
     (Join-Path $root 'pet_guard.ps1') + '"') $root
  Write-Output "④ 守护已拉起（掉线自愈中）"
}

Write-Output ""
Write-Output "== 完成：点桌宠开聊天页 http://127.0.0.1:8094 =="
