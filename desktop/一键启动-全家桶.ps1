# 桌宠AI · 一键启动全家桶（桌面快捷方式「启动桌宠」指向这里）
# 组件：① 引擎 18123 ② webapp 8094 ③ 守护 ④ 气泡壳 ⑤ Ollama 适配器 11434
# 幂等：全部复用 start_all.ps1 的既验逻辑（端口+进程双判防双开、$PSScriptRoot
# 推导根目录不写死盘符）；已在跑的组件自动跳过。
$root = Split-Path -Parent $PSScriptRoot        # 本脚本在 desktop\ 下，项目根是上一级

# 清掉上一次"没停干净"留下的守护暂停标记（一键启动语义=全家照常拉起）
$pauseMark = Join-Path $root 'data\pet_guard_paused'
if (Test-Path $pauseMark) {
  Remove-Item $pauseMark -Force
  Write-Output "⓪ 已清守护暂停标记（上次停止残留）"
}

Write-Output "== 一键启动全家桶（引擎→webapp→守护→气泡壳→适配器） =="
& (Join-Path $root 'start_all.ps1')

Write-Output ""
Write-Output "== 全家桶就绪：聊天页 http://127.0.0.1:8094 · 气泡壳右键有菜单 =="
