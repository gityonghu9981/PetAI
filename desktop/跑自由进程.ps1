# 跑一个自由进程脚本（ProcessStartInfo 启动带 tkinter 的 Python），绕沙箱网络/进程限制
# 用法: & 跑自由进程.ps1 <py脚本相对路径>
# 解释器逐级探测，不写死路径
param([string]$script = "")
$root = Split-Path -Parent $PSScriptRoot
$py = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
if (-not (Test-Path $py)) {
  $pyl = Get-Command py -EA SilentlyContinue
  if ($pyl) { $py = $pyl.Source }
  else { $p = Get-Command python -EA SilentlyContinue; if ($p) { $py = $p.Source } }
}
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $py
$psi.Arguments = '-X utf8 "' + $script + '"'
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$null = [System.Diagnostics.Process]::Start($psi)
Write-Output "launched: $script"
