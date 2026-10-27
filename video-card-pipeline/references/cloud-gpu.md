# 云 GPU 出片方案（自己租 GPU 跑 MiniMax H3）

`compshare` 入口的整体用法：租什么机器、装什么、分几档、一批片子怎么跑、多少钱、哪些还没验证。
命令和接口字段见 `providers.md` 的 compshare 一节；这里只讲方案。示例平台是优云智算（CompShare），换别家照 `scripts/cloud.py` 改。

## 一句话

一台 48GB 显存的卡 + 现成的 H3 ComfyUI 镜像，攒一批卡开一次机。先用**草稿档**试动作，**定稿档**日常抽卡，审片通过的几条再**精修**，跑完马上关机。

## 什么时候值得用

- 要批量出片、反复抽卡：按小时计费，一小时能出几十条，比按条付费的 API 便宜。
- 要能控制的出片链路：seed、步数、分辨率、工作流都在自己手里，AI 全程走命令行，不用点网页。
- 只出一两条、或者不想管机器：用出片 API 或网页交接包更省事。

## 机器和权重

| 项 | 选什么 | 为什么 |
|---|---|---|
| 显卡 | **单卡 48GB**（例如 4090 48G） | 33B 的 H3 用 int8 全量权重能整个放进显存 |
| 权重 | **int8 全量**（主模型约 34GB）+ int8 文本编码器 | bf16 约 66GB 放不下，会卸载，慢好几倍 |
| 剪枝版 | 只给 24 / 32GB 的卡用 | 48GB 上实测只快约 10%，不值得牺牲画质 |
| 后端 | **ComfyUI** | vLLM-Omni 要 200GB 以上内存，单卡机器一般给不了 |
| 镜像 | 平台上现成的 H3 ComfyUI 社区镜像（开机自启 8188） | 省掉自己装模型、插件的时间；换镜像后对照 `/object_info` 核对节点名和文件名 |
| 磁盘 / 内存 | 按镜像和模型官方要求；模型在共享盘的镜像省系统盘 | — |

本地笔记本（16GB 内存级别）不跑 H3，也不做视频超分。

## 档位

一台机器挂几档，每档就是 `project.yaml` 里的一个模型条目，各带一份工作流（`scripts/remote/workflows/`）。

| 档 | 模型名（示例） | 工作流 | 分辨率 / 步数 | 用途 | 实测（5 秒片，48GB 卡） |
|---|---|---|---|---|---|
| 草稿 | `cloud-h3-fast` | `h3_fl2va_draft_api.json` | 约 0.4MP（480P）· SLA 加速 4 步 | 试提示词、试动作 | 端到端约 60 秒 |
| **定稿（默认）** | `cloud-h3` | `h3_fl2va_final_api.json` | 约 0.75MP（约 1184×672）· SLA 加速 6 步 | 日常抽卡，审片就看这一档 | 远端约 90 秒，端到端约 2 分钟 |
| 精修 | 走 `post` 钩子 | — | 选中的条超分到 1080P | 只给审片通过的几条 | 还没搭好 |
| 官方对照 | `cloud-h3-official` | `h3_fl2va_official_api.json` | 约 0.75MP · 官方 20 步 | 怀疑加速档掉画质时对照 | 约 330 秒（含首次加载模型） |

加速档 = lightx2v SLA turbo LoRA + `H3SLAAttention` + `MiniMaxH3DualClockSamplerT8`。同一张首帧、同一个 seed 对比，定稿档 6 步和官方 20 步肉眼几乎分不出，快 3 倍多。

另带三份试验工作流，没有定论，按需挂：`h3_fl2va_pruned_api.json`（定稿档换剪枝主模型）、`h3_fl2va_8step_api.json`（8 步 LoRA、不开 SLA）、`h3_fl2va_1080_api.json`（定稿配置直接出约 1.5MP，看能不能省掉超分）。

**换分辨率会换画面。** 草稿档抽中的那条，拿同一个 seed 换成定稿档重出，出来的不是同一条片子。所以草稿档只用来定提示词和动作方向，真正挑片在定稿档里挑；要更高清就对选中的那条做超分（精修），不要换档重出。

## 一批片子怎么跑

```bash
# 0. 攒卡：卡都 validate 过、关键帧定好，再开机（开着就在计费）
$V validate 01-01 01-02 01-03 …

# 1. 开机：人批准开机的原话。第一次建机先 --create 看报价，同意后再加 --price-ok
$V cloud up --approved "张三 10-23 批开机 2 小时"
$V cloud status                                    # 等模型服务健康

# 2. 草稿档试方向（可选）：动作、提示词没把握的卡先跑一条
$V generate 01-02 --model cloud-h3-fast --budget 1

# 3. 定稿档抽卡：每张卡出几条，进审片台
$V generate 01-01 01-02 01-03 --model cloud-h3 --budget 9

# 4. 人在审片台给结论 → 取回
$V sync

# 5. 跑完立刻关机（cloud up 还会设兜底定时关机，但别指望它）
$V cloud down

# 6. 精修：选中的条做超分（在 hooks.post 里挂好超分命令以后）
$V post <条> upscale
```

- 钱只在 `cloud up` 那一步批：开机后单条 `generate` 不再问，所以批开机时要说清时长。
- `generate` 发现盒子没开就直接拒，从不自己开机。
- 中途断网、本机崩了：`collect` 接着查，远端任务不会重交。

## 成本怎么估

按小时价 × 开机时长算，跟出了多少条无关。以定稿档远端约 90 秒一条算，一小时大约 35～40 条；草稿档更多。
省钱的办法就三个：攒够一批再开、草稿档少用（只用来定方向）、跑完马上关。价格以 `cloud up --create` 的报价为准。

## 模型许可（用之前自己核对）

H3 是开源权重，但用的是社区许可，不是 MIT / Apache。下面是要点，**以官方许可原文为准**：

- 年营收低于约定门槛（写本文时是 2000 万美元）可以免费商用；
- 有地域限制：部分国家和地区不在许可范围内，生成的视频也不能拿去那些地方用；
- 商用产品的界面上要标明用了 MiniMax H3；
- 不能拿生成的视频去训练别的模型。

## 已经验证 / 还没验证

已验证（48GB 卡 + 社区 ComfyUI 镜像）：首帧出片（fl2va）的草稿 / 定稿 / 官方三档；工作流随任务上传、盒子上不留状态；卡里没给的图会自动删掉对应节点。

还没验证：

- 首尾帧都给（keyframes）、纯文字（text）、参考图（reference）三种模式，目前只配了 fl2va 工作流；
- `cloud up / down` 从建机到兜底定时关机的完整一圈；
- 精修档的超分方案（选哪个超分模型、在云上跑多久）；
- 打斗类动作加 LoRA：打算做成定稿档上的可选项，不单独设档；能不能和 SLA 6 步一起用、要不要 8 步，都待测；
- 32GB 卡（例如 5090）跑剪枝版的速度和画质。

## project.yaml 示例

```yaml
video:
  providers:
    cloud-gpu: {kind: compshare}
  models:
    cloud-h3:                        # 定稿档（默认），用 cloud.comfyui 里的工作流
      {provider: cloud-gpu, model: MiniMax H3 定稿档, seed: 42, min_seconds: 3, max_seconds: 15}
    cloud-h3-fast:                   # 草稿档：自带一套工作流，整块覆盖 cloud.comfyui
      provider: cloud-gpu
      model: MiniMax H3 480P 草稿档
      seed: 42
      min_seconds: 3
      max_seconds: 15
      comfyui:
        workflows: {fl2va: ~/.claude/skills/video-card-pipeline/scripts/remote/workflows/h3_fl2va_draft_api.json}
        nodes: {fl2va: {prompt: '9.prompt', first_frame: '5.image', last_frame: '6.image', seed: '11.noise_seed',
                        frames: '9.length', width: '9.width', height: '9.height', steps: '22.steps', output: '18'}}
    cloud-h3-official:               # 官方 20 步对照：同上，换 h3_fl2va_official_api.json，steps 节点是 '13.steps'
      provider: cloud-gpu
      model: MiniMax H3 官方 20 步
      seed: 42
      comfyui:
        workflows: {fl2va: ~/.claude/skills/video-card-pipeline/scripts/remote/workflows/h3_fl2va_official_api.json}
        nodes: {fl2va: {prompt: '9.prompt', first_frame: '5.image', last_frame: '6.image', seed: '11.noise_seed',
                        frames: '9.length', width: '9.width', height: '9.height', steps: '13.steps', output: '18'}}
  cloud:
    cli: compshare
    instance: ''                     # 已有实例 id；留空则 `cloud up --create` 按 create 建
    backend: comfyui
    remote_root: /root/vcp
    safety_shutdown_hours: 3         # 兜底定时关机
    server: {command: '', health: 'http://127.0.0.1:8188/system_stats', probe_timeout: 300}
    comfyui:                         # 默认 = 定稿档
      url: http://127.0.0.1:8188
      fps: 24
      workflows: {fl2va: ~/.claude/skills/video-card-pipeline/scripts/remote/workflows/h3_fl2va_final_api.json}
      nodes:
        fl2va: {prompt: '9.prompt', first_frame: '5.image', last_frame: '6.image', seed: '11.noise_seed',
                frames: '9.length', width: '9.width', height: '9.height', steps: '22.steps', output: '18'}
```

工作流路径写成你放 Skill 的实际位置（相对项目目录或绝对路径都行，`~` 会展开），每条任务会把它一起传上去。
