# 桌宠AI · 整体框架（ARCHITECTURE）

> 定位：**本地大模型 + 自定义技能 + 自主行为**的桌面宠物 AI。
> 本文档是全局地图：分层、模块、数据流与扩展路径。实时状态跑
> `python app/tools/doctor.py` 看。模块 docstring 记录关键决策与修订原因。

## 1. 分层总图

```
┌─────────────────────────────────────────────────────────┐
│  形象层   desktop/pet.py（tkinter 白猫气泡壳，零依赖）      │  ✅
│  网页层   webapp.py（FastAPI :8094）— 对话/设置/记忆/技能/审计 │  ✅
├─────────────────────────────────────────────────────────┤
│  L2 编排   pipeline（路由→检索→生成→后处理） router skills   │  ✅
│  L3 长期   memory（SQLite·三路RRF·核心集·每日巩固）          │  ✅
│           media_in（图/视频入站） vision（视觉塔） video     │  ✅/🔌
├─────────────────────────────────────────────────────────┤
│  L4 调度   scheduler（优先级/抢占红线/切片/checkpoint）      │  ✅
│  L5 技能   skillbook（建档/人工闸门/配额/metrics/retro）     │  ✅
├─────────────────────────────────────────────────────────┤
│  L0 基础   config（真源） lexicon（词表） vram（显存预算）    │  ✅
│  引擎     llama-server :18123（ngl/ctx 自适应）             │  ✅
│  守护     pet_guard.ps1 + start_all.ps1 一键整栈            │  ✅
└─────────────────────────────────────────────────────────┘
```

分层铁律：**单向依赖**。L(n) 只准依赖 L(n-1)；memory 不 import llm（巩固的提炼
函数由 webapp 注入）；桌面壳不 import webapp。

## 2. 模块清单与状态

| 模块 | 层 | 职责 | 状态 | 验收 |
|---|---|---|---|---|
| config | L0 | 配置真源，settings.json + schema 校验 | ✅ | smoke |
| lexicon | L0 | 词表+编译正则（TIME/CORRECT/FACT/WRITING） | ✅ | smoke |
| vram | L0 | 显存预算：fit/降档/预留政策 | ✅ | smoke |
| engine | L1 | 引擎进程管理（幂等启停/按路径清理/预算闸） | ✅ | smoke |
| llm | L1 | 对话客户端（溢出裁剪/空答降温/瞬断重试） | ✅ | 链路实测 |
| context | L1 | 上下文分区配额（记忆块独立预算不砍） | ✅ | smoke |
| pipeline | L2 | 编排全链路（answer_once/answer_stream） | ✅ | 链路实测 |
| router/skills | L2 | 意图判定（规则强制+兜底） | ✅ | route_cases |
| memory | L3 | 三路 RRF 召回/核心集限额/每日巩固/迁移链 | ✅ | l3 测试 |
| proactive | L3 | 主动搭话（static 台词零模型调用） | ✅ | smoke |
| scheduler | L4 | 优先级队列/抢占红线/切片/checkpoint | ✅ | l4 测试 |
| skillbook | L5 | 建档/人工闸门/配额/metrics/retro | ✅ | l5 测试 |
| vision | L2 | 看图（视觉塔，`_VERIFIED` 开关） | 🔌 | 框架自检 |
| vector | L3 | 向量召回第三腿（复用引擎 `/embeddings`，零新增显存） | ✅ | doctor |
| api_line | 外挂 | API 线深度推理外挂 | 🔌 默认关 | 框架自检 |

## 3. 接口占位的调用契约（升级时怎么填）

填实现的人只需要遵守签名，**调用方一行不用改**：

| 接口 | 签名 | 填好后自动发生什么 |
|---|---|---|
| `vector.encode/best_match` | `best_match(query, candidates, top_k) -> [(idx, score)]` | `memory._vector_scores` 三路 RRF 自动生效 |
| `vision.describe_image` | `describe_image(path, question) -> str` | `_VERIFIED` 翻 True；看图能力启用 |
| `api_line.chat` | `chat(messages) -> {"content"} or None` | pipeline 的深度思考分支自动路由；None=无感降级本地线 |

**显存纪律**（接任何常驻模型前必读）：先过 `vram.fit()`——向量腿走**路线 A
（复用聊天引擎的 `/embeddings`，不新占显存）**；宁可砍功能不砍红线。

## 4. 运行时拓扑

- 引擎 :18123（只绑回环）· webapp :8094 · 聊天页 http://127.0.0.1:8094
- 气泡壳 desktop/pet.py（tkinter，读 `data/status.json` 显示状态）
- Ollama 协议适配器 :11434（`app/tools/ollama_adapter.py`，桥接只讲
  Ollama 协议的形象引擎 → 桌宠大脑）
- 守护 `pet_guard.ps1`：按端口探活 + 按本项目路径匹配进程，掉线自动拉起
  （engine/web/shell/adapter 四段），带指数退避与失败冷却
- `start_all.ps1` 一键整栈（幂等）；`一键停止.ps1` 先暂停守护再逐级停
- 端口真源在 `app/core/config.py`（FORBIDDEN_PORTS 防撞其它项目）

## 5. 验收与自检

```powershell
python app\tools\doctor.py                 # 框架自检（人类可读）
python app\tools\doctor.py --json          # 机读（= GET /api/framework）
```

## 6. 升级路径（按实际情况加和升级的顺序建议）

1. **日常用起来**（现在就能跑）——记忆库自动积累
2. **第一个技能**——`POST /api/skills/create` 建档 → 填 skill.md → publish
3. **视觉链路实测**——`vision._VERIFIED` 翻 True 的前置
4. **API 线**（想要时）——settings 三件套 + 总开关，零代码
5. **新通道**——在 `app/channels` 层新增模块，遵守 waiter/notify 的返回契约
