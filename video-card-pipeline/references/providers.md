# 出片入口

模型别名 → 入口条目 → 适配器，三者解耦（与生图同构）：

```yaml
video:
  providers: {entries: {agnes: {kind: agnes, api_key_env: AGNES_API_KEYS}}}
  models:    {agnes-flash: {provider: agnes, model: agnes-video-2.5-flash, free: true}}
```

`free: true` 免 `--approved`；`min_gap` 两次提交的最小间隔秒数（免费档限速）；`poll_interval` 轮询间隔。
所有入口都是**异步契约**：`submit` 拿回 handle 存进 manifest，`poll` 查状态。进程崩了、超时了，`collect` 用同一个 handle 接着查，**不会重提**。

**关键帧几张都行，模型吃几张看它自己**：handoff、command 拿到全部关键帧；其余（mock、出片 API 示例、云 GPU）默认只吃首尾两帧，中间几张**不送出**（不冒充参考图），`validate` / `prompt` 如实提示；有首帧 + 中间帧、没尾帧时按首帧模式出。模型能吃更多，就在 `models.<别名>` 或供应商条目上写 `max_keyframes: <张数>`。


## mock
ffmpeg 把首帧做成缓慢推近的可播 mp4（没首帧就用测试图）。`duration`、`delay`（模拟排队）可配。全流程演练用。

## agnes（可选示例：一个真实的出片 API；契约是实跑确认的，别照官方 README 改）
- 建单 `POST {base}/v1/videos`；轮询 `GET {base}/agnesapi?video_id=<id>&model_name=<模型>`。
- flash：`mode` keyframe（`first_frame` / `last_frame`）→ 排不进时退到 reference（`images`≤5，提示词前加 `<Picture n>`，会重新构图）→ text；`size=720P`，`seconds` 4–12；传 `width`/`num_frames` 会 400。
- v2.0：`width/height/num_frames(8n+1)/frame_rate`；单图 `image`；首尾帧用 `mode=keyframes` + `extra_body.image=[首,尾]`。
- 图直接喂 JPEG data URL（1280 宽、q92），不经图床。
- 429/503 退避 `retry_wait`（默认 90s），多把 key 轮换；其他 5xx 记 `unknown`。
- key：`api_key_env`（逗号或换行分隔）→ `launchctl getenv` → `api_key_file` 的同名行 → `newapi_db`（可选：只读一个 new-api / one-api 网关的 sqlite 库取 key）。

## toonflow（画布内出片，可选插件）
不随本 Skill 发。插件放在和 Skill 并排的 `plugins/画布/video/`（或 `plugin_dir` 指定）；不配 `kind: toonflow` 就完全不加载。

## handoff（网页平台）
`generate` 生成 `video-output/handoff/<卡>_<模型>_<n>/`（全部关键帧，按时间排号 `关键帧1_0s_首帧.png` …；参考图、`提示词.txt`、`参数.txt`），任务状态记为 `waiting_import`。
人在网页上生成后，`import <卡> <mp4>` 收回（这张卡只有一个等待中的交接任务时不用写 `--model`，有几个就得写；默认复制、不动下载的文件，`--move` 才删；输出目录里的文件拒收），接着走同样的落盘 → 抽帧 → 送审。`open: true` 会打开交接包目录和平台网页。

## command（任意 CLI）
`command` 模板里可用的占位符：`{first_frame} {last_frame} {keyframes} {keyframes_json} {prompt} {prompt_file} {duration} {aspect} {model} {mode} {out}`。`{keyframes}` 是全部关键帧路径（按时间、逗号分隔），`{keyframes_json}` 是一个文件：`[{path, at, slot}]`。退出码 0 且写出了 `{out}` 才算成功。
`billed_on_failure: true` 时，失败记 `unknown`（不重提）。可灵等同步出片的 CLI 可以直接接；
即梦 `dreamina` 是异步的（提交拿 `submit_id`，再 `query_result` 查），要写一个"提交 + 轮询 + 下载到 {out}"的包装脚本，且每次扣积分：接的时候配 `billed_on_failure: true`。

## compshare（可选示例：自己租云 GPU 跑开源模型，以优云智算 CompShare + MiniMax H3 为例）
只经平台官方 CLI（`compshare-cli` ≥0.5.6，`uv tool install compshare-cli`；更老的版本 SSH 传参丢引号、没有 `instance monitor`）。凭据只走 `compshare config`，流水线不读。
换别家云平台：照 `scripts/cloud.py` 的几个调用（建机 / 开关机 / 传文件 / 远端任务）换成那家的 CLI 即可，出片和闸门逻辑不用动。

**GPU 按小时计费，开关机和出片分开：**
```bash
$V cloud up --create --approved "张三 10-23 批开机 2 小时"             # 第一次：只 dry-run 打印配置和报价，不建机（退出码 0）
$V cloud up --create --price-ok --approved "张三 10-23 批开机 2 小时"  # 用户看过报价、同意后才真建机
$V cloud up --approved "张三 10-24 批开机 2 小时"                      # 之后：已有实例就是开机，不再报价
$V generate <卡…> --model cloud-h3            # 每条 = 上传任务文件夹 → 远端可断线任务 → 取回 result.mp4
$V cloud status                               # 实例状态、模型服务健康
$V cloud down                                 # 批次跑完必须关；`cloud up` 还会设 safety_shutdown_hours 的兜底定时关机
```
- `--approved` 写**人的原话**（谁、何时、批了什么），按 card-core `jobs.approval_problem` 判：空、少于 4 个字、像占位符（`...`、`<…>`、TODO、待填）的一律拒。开机后 `cloud-h3` 单条不再问，所以开机是这条线唯一的闸门。
- `generate` 在盒子没开时直接拒（记 failed，可重提），**从不自己开机或建机**；单条免 `--approved`（钱在 `cloud up` 时批）。
- 每条：本地 `runs/<run>/cloud_job/`（job.json + prompt.txt，图片副本传完即删）→ `instance cp` 到 `<remote_root>/runs/<run>` →
  `instance job submit --job-id vcp-<run>`（同一个 id 重交是幂等的）跑 `h3_run.py` → `job show` 轮询 → 取回 `result.mp4`、`done.json`、`remote.log`。
- 轮询时连不上云端只算 running（`collect` 能接着查）；远端任务失败记 failed，日志在 `remote.log`。
- 远端脚本 `scripts/remote/h3_run.py`（纯标准库）：后端 `vllm` | `comfyui` | `mock`；卡的 mode → H3 任务：keyframes/first_frame → `fl2va`，text → `t2va`，reference → `ref2va`。
- 自检用假 CLI `scripts/remote/fake_compshare.py`，JSON 结构照 0.5.6 源码。

**vLLM-Omni 请求**（照官方 recipe `recipes/MiniMaxAI/MiniMax-H3.md`，用假服务核对过字段）：`POST /v1/videos/sync`，multipart：
`prompt / fps=24 / num_inference_steps=50 / flow_shift=12 / seed / extra_params={"task","duration","audio_flow_shift":3.0}`；
首尾帧 = 两个 `input_references` + `extra_params.frame_indices=[0,-1]`；只首帧 = `input_reference`；参考图 ≤9 张 + `aspect_ratio=adaptive`。不给宽高时按首帧比例、短边 768。出的 mp4 带 32kHz 立体声。

**选机器**：按模型官方写的显存 / 内存 / 磁盘要求挑；按小时计费，攒一批卡再开机。33B 的 H3 在单卡 48GB 上用 int8 全量权重（不是 pruned）能整个放进显存，bf16 放不下会卸载、慢好几倍。

**ComfyUI 后端**（单卡推荐）：在 `cloud.comfyui` 里填 `workflows.<任务>` 和 `nodes.<任务>`：
- `workflows.<任务>`：API 格式工作流 json。写本机路径（相对项目目录或绝对路径）就每条随任务文件夹一起传上去，盒子上不留状态；写盒子上的绝对路径也行。
- `nodes.<任务>`：`{prompt: "<节点>.<输入>", first_frame: …, last_frame: …, seed: …, frames: …, width: …, height: …, steps: …, output: "<出视频的节点 id>"}`。卡里没给的图（比如没有尾帧）会把对应的图片加载节点和它的连线一起删掉。不给 width/height 时尺寸由工作流自己按首帧算。
- 模型条目里也能写 `comfyui:`（整块覆盖 workflows / nodes），一个盒子挂几档：抽卡档、定稿档、官方原版。
- 自带三份首尾帧（fl2va）工作流，放在 `scripts/remote/workflows/`，只用 ComfyUI 核心的 MiniMax H3 节点和社区常见的加速节点：
  `h3_fl2va_official_api.json`（官方 20 步 res_multistep，约 0.75MP）、
  `h3_fl2va_final_api.json`（lightx2v SLA turbo LoRA + `H3SLAAttention` + `MiniMaxH3DualClockSamplerT8`，6 步，约 0.75MP）、
  `h3_fl2va_draft_api.json`（同上，4 步，约 0.4MP，也就是 480p，用来抽卡）。
  加速档要用到的 LoRA 和插件，镜像里得有；换镜像以后对照它的 `/object_info` 核对节点名和文件名。

## 新增一种入口
在 `providers.py` 写一个 `Provider` 子类（实现 `submit` / `poll`，可选 `credentials_ok`），并登记进 `KINDS`。上面的契约不变。
