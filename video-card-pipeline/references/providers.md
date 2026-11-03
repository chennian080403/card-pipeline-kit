# 出片入口

模型别名 → 入口条目 → 适配器，三者解耦（与生图同构）：

```yaml
video:
  providers: {entries: {agnes: {kind: agnes, api_key_env: AGNES_API_KEYS}}}
  models:    {agnes-flash: {provider: agnes, model: agnes-video-2.5-flash, free: true}}
```

`free: true` 免 `--approved`；`min_gap` 两次提交的最小间隔秒数（免费档限速）；`poll_interval` 轮询间隔。
所有入口都是**异步契约**：`submit` 拿回 handle 存进 manifest，`poll` 查状态。进程崩了、超时了，`collect` 用同一个 handle 接着查，**不会重提**。

**关键帧几张都行，模型吃几张看它自己**：handoff、command 拿到全部关键帧；其余（mock、出片 API 示例）默认只吃首尾两帧，中间几张**不送出**（不冒充参考图），`validate` / `prompt` 如实提示；有首帧 + 中间帧、没尾帧时按首帧模式出。模型能吃更多，就在 `models.<别名>` 或供应商条目上写 `max_keyframes: <张数>`。


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

## 新增一种入口
在 `providers.py` 写一个 `Provider` 子类（实现 `submit` / `poll`，可选 `credentials_ok`），并登记进 `KINDS`。上面的契约不变。
