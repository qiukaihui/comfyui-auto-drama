# 配置说明（CONFIG）

复制 `config.example.json` 为 `config.json`（同一目录），按下面说明填写。
**所有路径均为相对项目根目录的相对路径**，禁止写绝对路径（如 `/Users/xxx/...`）。
所有支持云端 API 的配置项，本地服务不可用时自动降级到云端。

配好后运行 `python3 scripts/check_env.py` 一键检查环境是否就绪。

---

## comfyui（远程生成服务器）

| 字段 | 说明 | 示例 |
|---|---|---|
| `server` | ComfyUI 服务器地址（局域网远程机或本机） | `http://192.168.1.23:8188` |
| `workflow_dir` | 工作流脚本目录（`build_api_graphs.py`、`*_api_template.json` 所在处） | `workflows` |

**说明**：远程 ComfyUI 需要安装 MiniMax H3 节点与模型（ref2va 工作流、turbo LoRA、SageAttention、无审查 CLIP），详见 `README.md` 的"远程依赖"章节。

## storage（存储路径）

| 字段 | 说明 | 示例 |
|---|---|---|
| `output_dir` | 生成视频/图片的下载目录 | `comfyui_backup/outputs` |
| `asset_dirs` | 参考素材目录列表（角色锚点图、场景图、分镜图存放处） | `["素材", "出镜素材"]` |

## llm（语言模型：剧本生成 / 改写 / 提示词扩写）

| 字段 | 说明 | 示例 |
|---|---|---|
| `provider` | 当前使用本地还是云端：`local` 或 `cloud` | `local` |
| `provider_type` | 云端接口格式：`openai` / `claude` / `dashscope` | `openai` |
| `local.url` | 本地 OpenAI 兼容服务地址（LM Studio / Ollama） | `http://127.0.0.1:1234` |
| `local.model` | 本地模型名（需已在 LM Studio 加载） | `qwen3.6-27b-abliterated-mlx` |
| `local.token` | 本地服务鉴权 token（无鉴权留空） | `sk-lm-xxx` |
| `cloud.enabled` | 是否启用云端 API（本地不可用时自动降级） | `false` |
| `cloud.base_url` | 云端 OpenAI 兼容地址 | `https://api.openai.com/v1` |
| `cloud.api_key` | 云端 API Key（**不要提交进 git**） | `sk-xxx` |
| `cloud.model` | 云端模型名 | `gpt-4o-mini` |

**说明**：
- 只要接口兼容 OpenAI `/v1/chat/completions` 即可，DeepSeek、通义、Moonshot、OpenRouter 等都可用（填各自 base_url / api_key / model）。
- 不兼容 OpenAI 的服务用适配器：`claude`（Anthropic Messages）、`dashscope`（通义原生）；
  适配器自动转换请求/响应格式，主流程无感知。
- 云端不可用且本地离线时，控制台自动回退内置规则扩写（效果差一些，但能跑）。

## image_gen（文生图：角色锚点图 / 场景图 / 分镜图）

| 字段 | 说明 | 示例 |
|---|---|---|
| `provider` | `comfyui` / `local` / `cloud` | `comfyui` |
| `provider_type` | 云端接口格式：`openai` / `dashscope` | `openai` |
| `local.url` | 本地生图服务（Boogu-Image）地址 | `http://127.0.0.1:8081` |
| `cloud.enabled` | 是否启用云端文生图 | `false` |
| `cloud.base_url` | 云端 OpenAI 兼容图片接口 | `https://api.openai.com/v1` |
| `cloud.api_key` | 云端 Key | `sk-xxx` |
| `cloud.model` | 图片模型名 | `gpt-image-1` |
| `comfyui.server` | 远程 ComfyUI（MiniMax H3 Still）地址 | `http://192.168.1.23:8188` |
| `comfyui.unet` | H3 Ref2VA 权重（Still 文生图必需） | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| `comfyui.clip` | 文本编码器（可换未审查版） | `qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors` |
| `comfyui.vae` | H3 视频 VAE | `minimax_h3_video_vae_fp16.safetensors` |
| `comfyui.size` | 输出尺寸（宽x高，须为 32 的倍数） | `768x1024` |
| `comfyui.steps` | 采样步数（20=质量 / 12=速度） | `20` |
| `comfyui.target_mode` | 生成帧数档位：`direct_1_frame` / `micro_video_5_frames` / `short_video_22_frames` | `micro_video_5_frames` |
| `comfyui.reference_strength` | 参考编辑模式强度（0-1，高=贴参考图） | `0.75` |
| `comfyui.t2i_reference_strength` | 纯文生图模式参考强度（低=忽略占位图） | `0.2` |
| `comfyui.story_mode` | 分镜图模式：`refedit`（场景+角色参考）/ `t2i`（纯文字） | `refedit` |
| `comfyui.timeout` | 单张生成超时（秒） | `900` |

**说明**：
- `provider: comfyui` 走远程 ComfyUI 的 MiniMax H3 Still 官方节点（Ref2VA），
  分镜图默认 `refedit` 模式：场景图做 Picture 1、出场角色锚点做 Picture 2+，
  把"恰好 2 人 / 形象一致"用参考图锁死（实测 1 张约 45 秒，解决 Boogu 多画第三人问题）
- `provider: local` 走本地 Boogu-Image（Apple Silicon / MLX，一键脚本 `scripts/deploy_boogu.sh`）
- 云端接口按 OpenAI `/v1/images/generations` 兼容实现（支持 `b64_json` 或 `url` 返回）；
  `provider: cloud` 时走云端，主端点失败自动降级本地
- 通义万相用 `provider_type: dashscope`（异步任务 + 自动轮询）

## vision（图片质检：检查穿帮 / 服装一致性 / 人数）

| 字段 | 说明 | 示例 |
|---|---|---|
| `base_url` | OpenAI 兼容视觉服务（本地或云端） | `http://127.0.0.1:8001/v1` |
| `api_key` | 服务鉴权（无则留空） | `sk-xxx` |
| `model` | 视觉模型名 | `qwen-vl-max` |

## console（控制台自身）

| 字段 | 说明 | 示例 |
|---|---|---|
| `port` | 控制台 Web 端口 | `8890` |
| `max_ref_images` | R2V 单段参考图上限（官方 Ref2VA ≤9） | `8` |

## models（R2V 出片模型，可选）

| 字段 | 说明 | 示例 |
|---|---|---|
| `r2v.unet` | Ref2VA 扩散模型（放远程 `models/diffusion_models/`） | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| `r2v.clip` | Ref2VA 文本编码器（放远程 `models/text_encoders/`） | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |

**说明**：
- `r2v.unet` 必须是官方 Ref2VA 权重（与 T2V/I2V 的 FL2VA 是两套模型）；提交时自动检测服务器
  是否已加载，缺失则回退 FL2VA 并提示。
- `r2v.clip` 开源默认用官方 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`；个人本地可换成
  未审查版（如 `qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors`），
  但**不要**把未审查模型名写进提交到开源仓库的 `config.example.json`。

---

## 环境变量覆盖（可选）

以下环境变量可覆盖 config.json（优先于配置文件）：

| 环境变量 | 覆盖项 |
|---|---|
| `BATCH_CONSOLE_CONFIG` | 指定 config.json 路径 |
| `COMFYUI_SERVER` | `comfyui.server` |
| `LLM_CLOUD_API_KEY` | `llm.cloud.api_key` |
| `IMAGE_CLOUD_API_KEY` | `image_gen.cloud.api_key` |
