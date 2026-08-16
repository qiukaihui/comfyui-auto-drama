# ComfyUI 自动化短剧生成系统

基于 **ComfyUI + MiniMax H3** 的自动化短剧/漫剧生产流水线：剧本生成 → 改写 → 规范提示词 → 参考资产（角色锚点图/场景图/分镜图）→ 批量视频生成 → 自动质检 → 一键合成成片。

控制台本体**零第三方依赖**（Python 标准库），生成走远程 ComfyUI（H3 ref2va），文本走本地 oMLX / LM Studio / 云端 API（可切换），图片走远程 ComfyUI MiniMax H3 Still / 本地 Boogu / 云端 API（可切换）。

> 本项目为演示/教学用途。模型输出质量受提示词与工作流配置影响，请勿用于任何违法违规内容。

## 系统架构

```mermaid
flowchart TB
  subgraph WEB["Web 控制台（console/ · Python 标准库 + 原生前端）"]
    W1["剧本生成 / 改写"]
    W2["规范提示词<br/>（三段式 + R2V 自动六段式）"]
    W3["参考资产<br/>角色四视图 / 场景图 / 分镜图"]
    W4["任务提交<br/>版本化 ID + 链式衔接"]
    W5["状态监控 / 自动下载"]
    W6["一键合成成片<br/>字幕 / CTA / 去开头杂音"]
    W1 --> W2 --> W3 --> W4 --> W5 --> W6
  end

  subgraph LOCAL["本地服务"]
    L1["语言模型<br/>oMLX(8001) / LM Studio / 云端 API"]
    L2["文生图<br/>H3 Still(远程 ComfyUI) / Boogu-Image / 云端 API"]
    L3["视觉质检<br/>多模态模型"]
  end

  subgraph REMOTE["远程 ComfyUI（Windows/Linux）"]
    R1["H3 工作流<br/>T2V / I2V / R2V(Ref2VA)"]
    R2["扩散权重<br/>ref2va + fl2va"]
    R3["文本编码器<br/>官方 nvfp4 / 无审查 CLIP"]
    R4["加速<br/>TurboLoRA + SageAttention"]
    R1 --> R2
    R1 --> R3
    R1 --> R4
  end

  subgraph DATA["数据与配置"]
    D1["console.db（SQLite 状态）"]
    D2["素材 / 输出目录"]
    D3["config.json（可配置化）"]
  end

  W1 --> L1
  W2 --> L1
  W3 --> L2
  W3 --> L3
  W4 --> R1
  R1 --> W5
  W4 --> D1
  W5 --> D1
  W5 --> D2
  W2 --> D3
  W3 --> D2
```

**链路一句话**：控制台把「剧本 → 提示词 → 参考资产」编排好，提交到远程 ComfyUI 用 H3 生成带原生立体声的视频，自动下载回本地合成成片；语言模型 / 文生图 / 质检支持本地、远程 ComfyUI 与云端切换。

## 目录结构

```text
comfyui项目/
├── console/                  # Web 控制台（Python 标准库 + 原生前端，零第三方依赖）
│   ├── batch_console.py      # 主服务：项目/剧本/提示词/资产/提交/监控/合成
│   ├── chain_daemon.py       # 链式生成守护进程（自动推进下一段）
│   ├── index.html            # 前端界面（单文件）
│   ├── rules/                # AI 规则（剧本/扩写/生图提示词，可编辑热加载）
│   └── start_daemons.py      # 一键启动 web + daemon（跨平台）
├── workflows/                # ComfyUI 工作流模板与构建/合成脚本
├── scripts/                  # 环境检查 / Boogu-Image 部署 / 备份
├── config.example.json       # 配置示例（复制为 config.json）
├── CONFIG.md                 # 配置说明
├── requirements.txt          # Python 依赖（可选，用于部分脚本）
└── LICENSE                   # MIT
```

## 功能一览

- 📜 **剧本流水线**：粘贴剧本片段 / 导入剧本 JSON → AI 生成或改写分镜剧本
- ✍️ **规范提示词**：按 MiniMax H3 官方规范生成提示词（对白 `(S1)/(S2)` 说话人 ID、音色锁定、防穿帮负向约束）；提交带参考图的 R2V 任务时自动包装官方 Ref2VA 六段式（subject_definitions / summary / retention_analysis …）
- 🖼️ **参考资产**：一键生成角色四视图（正/脸/侧/背）/ 场景图 / 分镜图，自动质检（人数、服装、穿帮），支持对话修改提示词后重新生成
- 🚀 **批量生成**：T2V / I2V / R2V（多参考）模式，链式衔接（上段末帧接下段首帧），步数 4-50 可调（≤8 自动加速）
- 🎬 **合成成片**：自动拼接 + 自动裁剪 H3 开头"起始音节"（消除每段开头的杂音）
- 🩺 **失败码体系**：生成失败自动打码定位（F-SUBMIT-API / F-TIMEOUT / F-COUNT …）

## 快速开始

### 1. 环境要求

- Python 3.10+（控制台零第三方依赖；工作流演示脚本需要 `pip install -r requirements.txt` 的 Pillow）
- ffmpeg（视频处理）
- 远程或本机 ComfyUI（需安装 MiniMax H3 工作流，见下方"远程依赖"）
- 文本模型：本地 LM Studio / Ollama（OpenAI 兼容）或任意云端 API
- 生图服务：远程 ComfyUI MiniMax H3 Still（`image_gen.provider: comfyui`，推荐）、
  本地 Boogu-Image（`http://127.0.0.1:8081`）或云端 API（三者可切换）

### 1.5 一键环境检查

```bash
python3 scripts/check_env.py
```

脚本会检查：Python 版本、ffmpeg、config.json、远程 ComfyUI、语言模型（本地/云端）、文生图（本地/云端）、视觉质检，逐项输出 ✅/❌ 并给出修复建议。

### 各平台安装命令

**macOS（Apple Silicon）**

```bash
# ffmpeg
brew install ffmpeg
# 演示脚本依赖（控制台本体零依赖）
python3 -m pip install -r requirements.txt
# 本地生图 Boogu（Apple Silicon）
bash scripts/deploy_boogu.sh
```

**Windows**

```bat
winget install ffmpeg
winget install Python.Python.3.12
python -m pip install -r requirements.txt
```

> Windows 无法本地跑 Boogu（MLX 仅 Apple Silicon），生图用云端：`config.json -> image_gen.provider: cloud`
> 推荐：Windows / Linux 直接改用远程 ComfyUI 的 H3 Still 生图（`image_gen.provider: comfyui`），
> 分镜图默认"场景+角色参考"模式，实测能稳定出"恰好 2 人"，且角色/场景与出片一致。

**Linux**

```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-pip
python3 -m pip install -r requirements.txt
```

> Linux 同样无法本地跑 Boogu，用云端生图。

### 1.6 文生图：远程 ComfyUI MiniMax H3 Still（推荐）

角色锚点图 / 场景图 / 分镜图都可以走远程 ComfyUI 的 H3 Still 官方节点
（`MiniMaxH3StillConditioningT8` + `MiniMaxH3StillDecodeT8`），配置：

```json
{
  "image_gen": {
    "provider": "comfyui",
    "comfyui": {
      "server": "http://192.168.1.23:8188",
      "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
      "clip": "qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors",
      "vae": "minimax_h3_video_vae_fp16.safetensors",
      "size": "768x1024",
      "steps": 20,
      "target_mode": "micro_video_5_frames",
      "reference_strength": 0.75,
      "story_mode": "refedit"
    }
  }
}
```

- **分镜图默认 `refedit` 模式**：场景图作 Picture 1、出场角色锚点作 Picture 2+，
  参考图把"人数 / 形象 / 环境"锁死，解决 Boogu 双人场景多画第三人的问题
  （实测单张约 45 秒，质检通过率显著高于纯文生图）
- 服务器需装 H3 官方节点包（含 T8 Still 节点）与 `minimax_h3_ref2va_pruned_int8_convrot.safetensors`、
  `minimax_h3_video_vae_fp16.safetensors`、Qwen3-VL H3 CLIP（可换未审查版）
- 纯文字生图（场景图等）自动用 `t2i` 模式：纯灰占位图 + 低参考强度
- 速度档位：`steps: 12` 更快、`20` 质量更好；`target_mode` 可选
  `direct_1_frame`（最快）/ `micro_video_5_frames`（推荐）/ `short_video_22_frames`（更稳）

### 2. 配置

```bash
cp config.example.json config.json
```

按 [CONFIG.md](CONFIG.md) 填写（服务器地址、模型、云端 API Key）。**所有路径写相对路径，禁止绝对路径。**

### 3. 启动控制台

```bash
cd console
python3 start_daemons.py        # 后台常驻（web + 链式守护）
# 或前台运行
python3 batch_console.py 8890
```

浏览器打开 <http://127.0.0.1:8890>。

## 使用流程（8 步）

1. **创建项目**（名称唯一，项目库显示在下方，支持继续）
2. **脚本生成**：填剧情梗概 AI 生成，或粘贴剧本片段 / 导入剧本 JSON
3. **改写脚本**：AI 从编剧角度理顺人物动作、情节流畅性（改写前/后对照）
4. **生成规范提示词**：逐段扩写成 H3 提示词（可手动编辑，可继续改写；R2V 提交时自动转六段式）
5. **参考资产**：资产状态表（身份/发型/服装/批准）→ 生成锚点图/场景图/分镜图 → 自动质检
6. **预览确认**：检查名称/时长/参考图/提示词完整性
7. **提交生成**：模式 / 分辨率 / 步数 / 链式衔接 / 成片质量可选，实时状态监控
8. **记录导出**：生成记录（版本批次）、失败码速查、CSV 导出、一键合成

演示数据见 [examples/demo_script.json](examples/demo_script.json)（可直接在第 2 步"导入剧本 JSON"）。

## 远程依赖（ComfyUI 需安装）

| 组件 | 说明 |
|---|---|
| MiniMax H3 节点 | `Comfy-Org/MiniMax-H3`（ref2va 工作流） |
| H3 模型 | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` 等 |
| 加速节点 | `MiniMaxH3TurboLoRA` + `MiniMaxH3MemoryEfficientSageAttentionPatch`（步数 ≤8 自动启用） |
| CLIP | 官方 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`（开源默认）；个人本地可换未审查版 |

> **R2V 权重说明**：`r2v.unet` 必须是官方 Ref2VA 权重（与 T2V/I2V 的 FL2VA 是两套模型）。
> 系统提交 R2V 时自动检测服务器上是否已加载该权重，缺失则回退 FL2VA 并在界面提示（身份锁定弱）。
> 模型文件放远程 `models/diffusion_models/` 后重启 ComfyUI 生效。

工作流模板与构建脚本在 `workflows/`。

## 本地服务（可选）

- **LM Studio**：`http://127.0.0.1:1234`，加载一个 OpenAI 兼容模型（如 `qwen3.6-27b-abliterated-mlx`）
- **Boogu-Image**：`http://127.0.0.1:8081` 本地生图（Apple Silicon / MLX）
- **视觉质检**：OpenAI 兼容视觉服务（如通义 qwen-vl），配置在 `config.json -> vision`

### Boogu-Image 本地部署（Apple Silicon）

```bash
bash scripts/deploy_boogu.sh          # 装依赖 + 克隆管线 + 下载模型 + 准备服务
BOOGU_MODEL=~/.boogu/models/Boogu-Image-0.1-Turbo-8bit \
BOOGU_QWEN=~/.boogu/models/Qwen3-VL-8B-Instruct-4bit \
python3 scripts/boogu_server.py       # 启动（默认 8081）
```

- 依赖：`mlx` / `mlx-vlm` / `fastapi` / `uvicorn` / `pillow`，管线来自 [xocialize/boogu-image-mlx](https://github.com/xocialize/boogu-image-mlx)
- 模型：`mlx-community/Boogu-Image-0.1-Turbo-8bit`（约 10GB）+ `Qwen3-VL-8B-Instruct-4bit`（prompt 理解辅助）
- 非 Apple Silicon 无法本地跑 → 直接切**云端生图**（见下）

### 云端模型切换（LLM / 文生图）

语言模型与文生图都支持本地 ↔ 云端切换（`config.json` 或设置抽屉）：

```json
{
  "llm": {
    "provider": "cloud",
    "cloud": { "base_url": "https://api.deepseek.com/v1", "api_key": "sk-...", "model": "deepseek-chat" }
  },
  "image_gen": {
    "provider": "cloud",
    "cloud": { "base_url": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-image-1" }
  }
}
```

- 云端接口需兼容 OpenAI：LLM 走 `/v1/chat/completions`，文生图走 `/v1/images/generations`（支持 `b64_json` 或 `url` 返回）
- 主端点失败会自动降级到另一侧（本地↔云端互备）
- **不兼容 OpenAI 的服务用适配器**：LLM 支持 `provider_type: openai / claude / dashscope`（Claude 用 Messages API、通义用原生 DashScope，自动转换格式）；文生图支持 `openai / dashscope`（通义万相异步任务自动轮询）。设置抽屉里可选接口格式。

## Windows 部署

本项目完全兼容 Windows。与 macOS 的差异仅在于**本地生图**（Boogu-Image MLX 仅支持 Apple Silicon）和**后台常驻方式**：

### 1. 安装依赖

```bat
python -m pip install -r requirements.txt     :: 控制台本体零依赖，仅为演示脚本装 Pillow
```

另外安装 **ffmpeg**（加入 PATH）：视频转码/抽帧/合成都需要。推荐 winget：

```bat
winget install ffmpeg
```

### 2. 模型配置

- **语言模型**：Windows 可装 LM Studio（[lmstudio.ai](https://lmstudio.ai)，跨平台）加载本地模型；也可直接用云端（`config.json -> llm.provider: cloud`）
- **文生图**：本地 Boogu MLX **无法在 Windows 运行**，请用云端生图：

```json
"image_gen": {
  "provider": "cloud",
  "cloud": { "base_url": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-image-1" }
}
```

- **ComfyUI**：Windows 上可直接跑远程或本机 ComfyUI（H3 节点），参考 `run_nvidia_gpu.bat`

### 3. 启动控制台

```bat
scripts\start_windows.bat          :: 前台运行（简单）
cd console && python start_daemons.py   :: 后台常驻（已做 Windows 兼容）
```

浏览器打开 <http://127.0.0.1:8890>。

> 说明：`start_daemons.py` 已跨平台（macOS/Linux 用 setsid，Windows 用 CREATE_NEW_PROCESS_GROUP）；
> 代码中所有路径均使用 `os.path` 拼接，Windows 中文路径/文件名正常。

## 目录结构

```text
├── console/          # 控制台（batch_console.py / index.html / rules / chain_daemon）
├── workflows/        # 工作流构建脚本与 H3 API 模板
├── scripts/          # 辅助脚本（ComfyUI 备份等）
├── examples/         # 演示剧本
├── config.example.json / CONFIG.md
├── requirements.txt / LICENSE
```

## 常见问题

- **生成视频开头有杂音**：H3 每段开头自带约 0.12-0.16s "起始音节"，一键合成时已自动裁剪；单段预览保留原始开头
- **角色音色不分**：对白必须带 `(S1)/(S2)` 说话人 ID（规则已强制），并配合音色锁定描述
- **衣服不对**：检查资产状态表的服装描述与锚点图是否一致，参考图是 H3 的主要依据

## 致谢

提示词工程方法论参考 Higgsfield 开源长片《Hell Grind》的结构化生产经验（角色资产化 / 七层提示词 / 镜头契约 / 失败诊断）。
