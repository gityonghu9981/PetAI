# 桌宠AI（PetAI）

本地大模型桌宠：一只住在桌面角落的白猫，有自己的大脑、记忆和脾气——全部推理在本机完成，对话不上传任何服务器。

## 这是什么

- **本地推理**：llama.cpp 系引擎加载 GGUF 模型，显存/内存本地消化，无任何云端接口
- **桌面形象**：tkinter 手绘白猫气泡壳（`desktop/pet.py`），零额外依赖；气泡显示她的状态、主动搭话与提醒，可拖动、单击开聊天
- **网页聊天**：`http://127.0.0.1:8094` 完整面板（对话、记忆、技能、审计、状态、设置）
- **自主活动**：定时碎碎念、愿望清单、主动搭话（static 台词模式，零模型调用）、等主人回话机制
- **多模态**：支持图片输入（媒体入站走本地处理），视觉模型见下方"模型推荐"
- **守护自愈**：`pet_guard.ps1` 每 30 秒探活引擎/网页/气泡壳/适配器，掉线自动拉起，日志滚动
- **协议适配**：`app/tools/ollama_adapter.py` 提供 Ollama 协议翻译层（:11434），兼容只讲 Ollama 协议的形象引擎

## 快速开始

### 1. 环境

Windows + Python 3.11（气泡壳需要 tkinter，请用系统 Python 跑）：

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. 模型

把 GGUF 模型放到 `models\` 目录（见"模型推荐"），按需修改 `app\core\config.py` 中的模型文件名与路径。

### 3. 启动

```powershell
.\start_all.ps1      # 一键启动：引擎 → 网页 → 守护 → 气泡壳
```

完成后聊天页在 `http://127.0.0.1:8094`，桌面右下角出现白猫气泡。停止用 `一键停止.ps1`（先暂停守护再逐级停，防守护把全家拉回来）。

## 模型推荐

| 档位 | 模型 | 说明 | 下载 |
| --- | --- | --- | --- |
| 轻量 3B（低显存） | Llama-3.2-3B-Instruct-Uncensored | 前期轻量项目验证过的无对齐小模型，约 4GB 显存即可流畅运行，纯文本 | https://huggingface.co/chuanli11/Llama-3.2-3B-Instruct-uncensored |
| 主用 27B（视觉 + 语言） | Qwen3.8-27B（Q4_K_M 量化） | 当前主用：本地 27B 基座 + 视觉投影塔，兼顾能力与显存 | GGUF 量化 https://huggingface.co/bartowski/Qwen3.8-27B-GGUF ；官方仓库 https://huggingface.co/Qwen/Qwen3.8-27B |

**VL 是什么关系**：27B 是"视觉语言模型（VLM）"——它由**语言基座 + mmproj 视觉塔**组成。mmproj 把图片编码成语言模型能读的 token 序列，语言基座负责理解和生成文字，两部分合起来才能"看图说话"。所以下载时除了 GGUF 主文件（Q4_K_M 档），还要把配套的 mmproj 视觉塔文件放进同一目录；纯文本场景（只聊天不发图）可以只加载基座。3B 是纯文本模型，没有视觉塔。

## 目录结构

| 目录 | 职责 |
| --- | --- |
| `app\core` | 引擎封装、配置、LLM 客户端 |
| `app\brain` | 大脑：对话管道、上下文、记忆、技能 |
| `app\growth` | 自主行为：定时任务、愿望、主动搭话 |
| `app\channels` | 通道：本地气泡、等待回复状态机、照片分享 |
| `app\web` | 网页服务与管理面板 |
| `app\tools` | 工具：ComfyUI 出图、Ollama 适配、体检 |
| `desktop` | 桌面气泡壳（tkinter 白猫） |
| `data` | 运行时数据（自动生成，不入库） |
| `models` | 模型文件（自行下载，不入库） |

## 常用命令

```powershell
.\start_all.ps1                 # 一键启动全家
.\一键停止.ps1                   # 一键全关（先暂停守护再逐级停）
powershell -File pet_guard.ps1  # 仅守护（掉线自愈）
python app/tools/doctor.py      # 体检（依赖/端口/模型自检）
```

## 开发说明

- **模块化**：新功能按目录归属（大脑/行为/通道/工具），增量扩展，不整文件重写
- **记忆与技能分离**：记忆 = 知道这件事；技能 = 能做到这件事（可调用、可复跑验证）
- **隐私边界**：本项目默认全本地运行；如需接入外部通道，请在 `app\channels` 层扩展并自行维护凭证
- **架构决策**：各模块 docstring 记录关键决策与修订原因，新增版本在模块内增量记录

## 许可证

MIT，见 [LICENSE](LICENSE)。
