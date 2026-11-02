# 桌宠AI · 一键启动整栈
# 顺序：引擎(18123) → webapp(8094) → 守护 → 气泡壳 → Ollama 适配器(11434)
# 全部幂等：已在跑的组件自动跳过。
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$ps = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

function PortUp($p) {
  [bool](Get-NetTCPConnection -LocalPort $p -State Listen -EA SilentlyContinue)
}
# 按进程数判断（不止端口）——根治"并发双开 webapp"
function ProcCount($pattern) {
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

# 1) 引擎
if (PortUp 18123) { Write-Output "① 引擎 18123 已在跑，跳过" }
else {
  Write-Output "① 启动引擎 18123…"
  StartHidden $py ('-X utf8 "' + (Join-Path $root 'app\core\engine.py') + '" start') $root
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    if (PortUp 18123) { Write-Output "   引擎就绪（$(($i+1)*3)s）"; break }
  }
  if (-not (PortUp 18123)) { Write-Output "   ⚠ 引擎 120s 未就绪（显存不足？看 logs/engine_err.log）" }
}

# 2) webapp：只要 webapp.py 进程在就不重复拉（含加载中/卡死，守护同款语义）
if ((ProcCount 'webapp\.py') -gt 0) {
  if (PortUp 8094) { Write-Output "② webapp 8094 已在跑，跳过" }
  else { Write-Output "⚠ webapp.py 进程已存在但 8094 未监听（加载中或卡死），不重复拉起——看日志" }
}
else {
  Write-Output "② 启动 webapp 8094…"
  StartHidden $py ('-X utf8 "' + (Join-Path $root 'app\web\webapp.py') + '"') (Join-Path $root 'app')
  Start-Sleep -Seconds 4
  Write-Output "   $(if (PortUp 8094) { 'webapp 就绪' } else { '⚠ webapp 未起，看 logs/webapp_err.log' })"
}

# 3) 守护：进程已在就跳过
$guard = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'pet_guard\.ps1' }
if ($guard) { Write-Output "③ 守护已在跑（PID $($guard.ProcessId -join ','))，跳过" }
elseif ((ProcCount 'pet_guard\.ps1') -gt 0) {
  Write-Output "⚠ 守护进程存在但枚举异常，不重复拉起"
}
else {
  Write-Output "③ 启动守护…"
  StartHidden $ps ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
                   (Join-Path $root 'pet_guard.ps1') + '"') $root
  Start-Sleep -Seconds 2
  Write-Output "   守护已拉起"
}

# 4) 气泡壳（系统 Python，标准库零依赖）
# data\pet_shell_off 存在 ⇒ 壳被主人停用，本段整体跳过；删除开关文件即恢复。
if (Test-Path (Join-Path $root 'data\pet_shell_off')) {
  Write-Output "④ 气泡壳已被 pet_shell_off 关闭，跳过（删 data\pet_shell_off 恢复）"
}
else {
$desk = Get-CimInstance Win32_Process -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'desktop\\pet\.py' }
if ($desk) { Write-Output "④ 气泡壳已在桌面（PID $($desk.ProcessId -join ','))，跳过" }
else {
  Write-Output "④ 启动气泡壳…"
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
  StartHidden "$shellPy" `
    ('-X utf8 "' + (Join-Path $root 'desktop\pet.py') + '"') $root
  Start-Sleep -Seconds 2
  Write-Output "   白猫已蹲在桌面上"
}
}

# 5) Ollama 适配器（11434）：形象侧"本机模型"口 → 桌宠大脑 8094 的翻译层。
if (PortUp 11434) { Write-Output "⑤ Ollama 适配器 11434 已在跑，跳过" }
else {
  Write-Output "⑤ 启动 Ollama 适配器 11434…"
  StartHidden $py ('-X utf8 "' + (Join-Path $root 'app\tools\ollama_adapter.py') + '"') $root
  Start-Sleep -Seconds 3
  Write-Output "   $(if (PortUp 11434) { '适配器就绪' } else { '⚠ 适配器未起（守护 30s 内会再拉一次，看 data\logs\pet_guard.log）' })"
}

Write-Output ""
Write-Output "== 全栈就绪：聊天页 http://127.0.0.1:8094 · 气泡壳右键有菜单 =="
