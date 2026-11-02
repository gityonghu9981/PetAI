# 桌宠AI · 一键全关（标准停止入口：桌面快捷方式指向这里）
# 顺序：写暂停标记 → 守护 → webapp → 引擎 → 气泡壳 → 适配器 → 检索浏览器 → 二次清扫 → 摘标记
# 幂等：没在跑的自动跳过。
# 安全红线：全程只按【本项目特征】匹配（pet_guard.ps1 / app\webapp.py /
# models 路径 / desktop\pet.py / browser-profile / ollama_adapter.py），绝不按进程名无差别杀。
# 互守护闭环：先杀守护再杀 webapp 的间隙里心跳可能拉起新守护——先写
# data\pet_guard_paused 暂停标记（守护看到标记只睡不拉起），全部停完做二次清扫，
# 确认零守护存活后才摘标记。
$root = Split-Path -Parent $PSScriptRoot        # 本脚本在 desktop\ 下，项目根是上一级
$pauseMark = Join-Path $root 'data\pet_guard_paused'

function Kill-Guards {
  # 只杀 -File 方式拉起的守护；排除 -Command 进程。$PID 排除自身。
  $g = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and
                   $_.CommandLine -like '*-File*pet_guard.ps1*' -and
                   $_.CommandLine -notmatch '-Command' }
  if ($g) { $g | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue } }
  return @($g).Count
}

# ⓪ 先写暂停标记：这之后无论谁拉起新守护（webapp 心跳/竞态），它都只睡不干活
New-Item -ItemType Directory -Force -Path (Split-Path $pauseMark) | Out-Null
Set-Content -Path $pauseMark -Value ("stopped " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding ASCII
Write-Output "⓪ 守护暂停标记已写（data\pet_guard_paused）"

# ① 守护
$n = Kill-Guards
Write-Output $(if ($n -gt 0) { "① 守护已停 ×$n（否则它会拉回引擎）" } else { "① 守护本来就没跑" })

# ② webapp（按本项目 webapp.py 进程匹配，连带覆盖"端口没监听但进程在"的加载中实例）
$wp = Get-CimInstance Win32_Process -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'app\\web\\webapp\.py' }
if ($wp) {
  $wp | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
  Write-Output "② webapp 已停（$(@($wp).Count) 个进程，含 venv 启动器壳对）"
} else { Write-Output "② webapp 本来就没跑" }

# ③ 引擎（按本项目模型路径匹配，不误杀其它项目的 llama-server）
$models = Join-Path $root 'models'
$killed = $false
Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -EA SilentlyContinue |
  Where-Object { $_.CommandLine -like "*$models*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue; $killed = $true }
Write-Output ("③ 引擎 " + $(if ($killed) { "已停" } else { "本来就没跑" }))

# ④ 气泡壳
$desk = Get-CimInstance Win32_Process -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'desktop\\pet\.py' }
if ($desk) {
  $desk | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
  Write-Output "④ 白猫下班了"
} else { Write-Output "④ 气泡壳本来就没跑" }

# ⑤ 检索用浏览器 profile（她的"出门鞋"）
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'browser-profile' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
Write-Output "⑤ 检索浏览器已收"

# ⑥ OpenAI 兼容适配器（11434，app\tools\ollama_adapter.py）
$a = Get-CimInstance Win32_Process -EA SilentlyContinue |
  Where-Object { $_.CommandLine -match 'app\\tools\\ollama_adapter\.py' }
if ($a) {
  $a | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
  Write-Output ("⑥ 兼容适配器已收（11434，{0} 个进程）" -f @($a).Count)
} else { Write-Output "⑥ 兼容适配器本来就没跑" }

# ⑦ 二次清扫：杀②的间隙里 webapp 心跳可能拉起了新守护（有暂停标记它只睡不干活，
#    但进程还活着）——全部停完后扫尾，确保没有任何守护存活
Start-Sleep -Seconds 2
$n2 = Kill-Guards
Write-Output $(if ($n2 -gt 0) { "⑦ 二次清扫：收掉竞态拉回的守护 ×$n2" } else { "⑦ 二次清扫：无残留守护" })

# ⑧ 摘暂停标记（只在确认零守护存活后；有漏网就保留标记并大声说）
$left = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
  Where-Object { $_.ProcessId -ne $PID -and
                 $_.CommandLine -like '*-File*pet_guard.ps1*' -and
                 $_.CommandLine -notmatch '-Command' }
if ($left) {
  Write-Output "⑧ ⚠ 仍有守护存活，暂停标记保留（下次一键启动会清）——请人工检查"
} else {
  Remove-Item $pauseMark -Force -EA SilentlyContinue
  Write-Output "⑧ 暂停标记已摘（下次一键启动正常拉守护）"
}
Write-Output "== 全部下班 =="
