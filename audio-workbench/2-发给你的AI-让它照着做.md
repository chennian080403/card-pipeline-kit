# AI 音频工作台：复刻指南（给 AI 看）

你要在用户电脑上写一个音频命令行（下面叫 `音频`，名字随用户定）：一条子命令干一件音频活。本地处理调 ffmpeg；配乐调 Google Lyria；结果登记进素材库，审核和定稿只归人。写成一个 Python 脚本，只用标准库加 ffmpeg。配置文件有 PyYAML 就用 YAML，没有就用 JSON（字段一样），不要自己手写 YAML 解析器。如果用户的 AI 工具支持 Skill，就再写一份 SKILL.md，把命令表和规矩放进去。

## 先查清用户电脑（动手前逐条确认，查不清就问人）

| 项 | 为什么要查 | 怎么查 |
|---|---|---|
| 系统，有没有 ffmpeg、ffprobe | 本地处理全靠它们 | `ffmpeg -version`、`ffprobe -version`；没有就装（Mac 用 `brew install ffmpeg`，Windows 用 `winget install ffmpeg`） |
| Python 3.9+，有没有 PyYAML | 脚本本身用 | `python3 -c "import yaml"` |
| 配乐用哪个平台、哪个模型 | Lyria 可以走 Google 官方，也可以走转售平台；接口和价格不一样 | 问人。走转售平台的，要人给接口文档地址 |
| 密钥放在哪 | 脚本只从环境变量读，不写进任何文件 | 问人用哪个环境变量名，让人自己设好；你只检查「有没有设」，不打印值 |
| 有没有现成的素材库 | 用户如果已有生图、视频的素材库（比如 `library.json`），音频登记进同一个；没有就新建一个 | 问人，或在项目里找 `library.json`、`project.yaml` |
| 要不要配音（TTS）、音效 | 本指南只实测过配乐；配音、音效要另接平台 | 问人。要的话先留接口，见「实现细节」最后一条 |
| 用户在审核和定稿时用什么名字署名 | 结论和定稿只收人的署名 | 问人，比如 `human:小王` |

## 核心原理（不能丢）

1. **一条命令只干一件事，不覆盖原文件**。输出一律写到 `-o` 指定的新路径；要替换原文件，等人听过确认后再挪。
2. **能用脚本判的不让模型判**。每条命令跑完，都用 ffprobe 打出时长、编码、采样率、声道。听感好不好由人判。
3. **花钱前要人批**。真付费的生成必须带 `--approved "人的原话"`，并记进 `approvals.jsonl`。空的、占位符（`...`、`<…>`、「待填」）、少于 4 个字的，脚本一律拒。试链路用 mock 或 `--dry-run`。
4. **结论和定稿只归人**。`lib review`、`lib decide` 的 `--by` 只收 `human:<名>` 这类人的署名；AI 的署名（`claude`、`agent`、`ai` 开头等）被拒。AI 只照抄人说的原话。
5. **登记永远是草稿**。素材进库是 `draft`，只有人 `decide final` 之后，`production_approved` 才变成 true。
6. **密钥只在环境变量里**。脚本不读密钥文件、不打印密钥。缺密钥就退出并说清缺哪个变量，不去别处找。
7. **付费请求失败不自动重发**。超时或 5xx 时，上游可能已经扣费出了曲，把报错原样交给人，让人先查平台账单。

## 流程

```
本地处理：  extract → denoise → trim-silence → normalize → convert   （常用顺序，每步都打时长和格式）
配乐：      写描述 → --dry-run 看请求 → 人批（原话）→ 生成 → 下载 → 存到 -o
入库：      lib register（草稿）→ 人听 → lib review good|bad|discard → lib decide final|rejected
            → 不行的 lib feedback 写原因和归因 → lib list 查
```

## 关键格式

### 本地命令和对应的 ffmpeg 写法

| 命令 | ffmpeg 要点 |
|---|---|
| `info <文件…>` | `ffprobe -v error -show_format -show_streams -of json`，一行输出一个 JSON：时长、编码、采样率、声道、体积 |
| `cut <in> -o … --start --end\|--duration` | 重新编码，不用 `-c copy`（copy 只能切在关键帧上，切点不准）。时间同时认秒数和 `00:01:30` |
| `concat <in…> -o …` | 用滤镜 `[0:a][1:a]…concat=n=N:v=0:a=1[a]`，可以混格式；不用 concat 列表文件（要求格式一致） |
| `convert <in> -o … [--rate --channels --bitrate]` | 按 `-o` 的后缀定格式 |
| `normalize <in> -o … --lufs --tp --lra` | `loudnorm=I=-16:TP=-1.5:LRA=11`。配音、播客用 -16，短视频平台用 -14 |
| `trim-silence <in> -o … [--threshold --min --all]` | 滤镜串：`E,areverse,E,areverse`，其中 `E = silenceremove=start_periods=1:start_threshold=<阈值>:start_silence=0.05`（去头，反转后再去一次就是去尾）。`--all` 在最后再接一个 `silenceremove=stop_periods=-1:stop_duration=<--min>:stop_threshold=<阈值>`，压中间的长停顿。`--threshold` 默认 -45dB，`--min` 默认 0.3 秒，只在 `--all` 时用 |
| `denoise <in> -o … [--method fft\|nlm --strength --floor --highpass]` | 默认 `fft`：`afftdn=nr=<--strength，默认 12>:nf=<--floor，默认 -50>`。`nlm` 用不带参数的 `anlmdn`，`--strength`、`--floor` 只对 fft 生效。`--highpass 80` 在前面加 `highpass=f=80` 切掉低频嗡声 |
| `extract <视频> -o …` | `-vn -map 0:a:0` 取第一条音轨 |

### 配乐请求（以 OpenAI 兼容的转售接口为例；走 Google 官方的，按官方文档改）

```
POST <平台地址>/v1/chat/completions
Authorization: Bearer <从环境变量读的密钥>
{"model": "<平台上的 Lyria 模型名>", "stream": false,
 "messages": [{"role": "user", "content": "<音乐描述>"}]}
```

- 作者用的平台：没有顶层 `prompt`，也没有时长参数，描述全写进 `content`。回包是同步的，连接要挂 40 秒到 3 分钟才返回。音频在哪两种都要认：旧格式在 `data[]`，每项一首，下载地址在 `url` 或 `download_url`；2026-10 起改成写在 `choices[0].message.content` 里，是一段 Markdown 链接（`[下载音频](https://….mp3)`）加一份同一首的 base64（`data:audio/mpeg;base64,…`），链接下载失败就解 base64。两处都没有才算失败，并把完整回包存下来给人看（这时多半已经扣费）。
- 模型名以平台 `GET /v1/models` 列出的为准，不要照文档或别人的配置抄（作者遇到过 Pro 版实际叫 `lyria-3-pro-preview`）。
- 429 是平台限流：原样报给人，不自动重发，隔一会儿再手动提交。
- 描述按这个顺序写：类型（纯音乐要写明「无人声」，歌曲写语言）→ 用途（片头、背景乐、片尾曲）→ 风格或流派 → 乐器 → 情绪和起伏（比如前段留白、后段渐强）→ 歌曲再写人声和歌词。
- 例子：`纯音乐，无人声。用途：古风短片片头。风格：中国风管弦。乐器：古筝、笛子、大鼓。情绪：沉稳大气，前段留白，后段鼓点渐强推到高潮。时长：约 30 秒。`

### 配置（`project.yaml`，不放密钥）

```yaml
library: {kind: local, root: 素材库}      # 没有素材库时用 none
audio:
  paths: {output: audio-output}           # 审核记录、反馈、批准记录放这里
  rules: {require_approval: true}
  providers:
    mock:  {kind: mock}                   # ffmpeg 合成正弦音，不联网，测链路用
    lyria: {kind: openai_chat, base_url: "<平台地址>", key_env: "<环境变量名>"}
  models:
    dry:   {provider: mock,  tasks: [music, tts, sfx]}
    music: {provider: lyria, model: "<模型名>", tasks: [music]}
  defaults: {music: dry}                  # 默认用 mock，真跑要点名 --model music
```

### 素材库和审核记录

| 文件 | 格式 |
|---|---|
| `<素材库>/library.json` | 条目列表。每条：`id`（文件 sha256 前 8 位，同内容再登记返回 already）、`title`、`prompt`、`model`、`media: audio`、`audio: {duration, codec, sample_rate, channels}`、`status: draft\|final\|rejected\|pending`、`production_approved`、`path`。文件拷进 `<素材库>/草稿区/<批次>/` |
| `<output>/station/station.json` | 每个条目最新的人工结论：`good\|bad\|discard`、`by`、`liked`、`disliked`、时间 |
| `<output>/station/activity-log.jsonl` | 每次给结论追加一行，只加不改 |
| `<output>/feedback.jsonl` | `id`、`accept\|reject\|revise`、`note`、`root_cause: text\|voice\|model\|ref\|edit`（稿子、声音、模型、参考、后期） |
| `<output>/approvals.jsonl` | 每次付费生成追加一行：谁批的原话、模型、描述、时间 |

用户已有生图或视频的素材库时，照它现成的 `library.json` 格式登记，只多加 `media: audio` 和 `audio:` 两个字段，不另起一套。没有的话，用这个最小格式：

```json
{"items": [{"id": "165cc950", "title": "片头", "prompt": "纯音乐，无人声……", "model": "music",
  "media": "audio", "audio": {"duration": 57.3, "codec": "mp3", "sample_rate": 44100, "channels": 2},
  "status": "draft", "production_approved": false,
  "path": "草稿区/20261024/165cc950_片头.mp3", "created": "2026-10-24T02:40:00"}]}
```

`path` 写相对素材库根目录的路径，批次名默认用日期。

## 复刻时要定的约定（照这个定，不要各自发挥）

| 事 | 约定 |
|---|---|
| 生成命令 | `music --prompt "<描述>" -o <文件> [--model <别名>] [--approved "<原话>"] [--dry-run]`；配音、音效同样写成 `tts --text`、`sfx --prompt`。`--model` 填配置里 `models` 的键（别名），请求体里的 `model` 用这个别名下的 `model` 字段（平台上的模型名），两者别混 |
| 选模型的顺序 | `--model` → `defaults.<任务>` → 报错。别名的 `tasks` 里没有这个任务的，直接拒 |
| 配置里的路径 | `library.root`、`paths.output` 都相对配置文件所在的目录，不是相对当前目录 |
| `base_url` | 写到 `/v1` 之前为止（例如 `https://平台域名`），脚本自己拼 `/v1/chat/completions` |
| 价格 | 每个付费别名在配置里写 `price`（每次多少钱），批量生成时用它算总价报给人 |
| 输出文件已存在 | 拒绝，要加 `--overwrite` 才覆盖；输出路径等于输入路径的，加了也拒 |
| 批准原话怎么算短 | 去掉空白和标点后少于 4 个字就拒。「批准」「OK」拒，「同意出一首」过 |
| 批准什么时候记 | 先查密钥、查平台配置，都没问题、马上要发请求之前，才写 `approvals.jsonl`。没发出去的不记 |
| 人的署名 | 配置里列白名单 `audio.reviewers: ["human:小王"]`，`--by` 必须在名单里，不在就拒。不用黑名单猜 AI 的名字 |
| 状态怎么变 | 登记 → `draft`；`review` 只记结论，不改状态；`decide` 改成 `final`、`rejected` 或 `pending`，只有 `final` 时 `production_approved` 是 true。`feedback` 要求必须有 `--root-cause` |
| mock | 不花钱，不用批准；时长取 `--seconds`，没给就 5 秒 |

## 实现细节（实测会踩的坑）

| 坑 | 怎么处理 |
|---|---|
| 时长写进描述也不准 | 写「约 30 秒」，实测出过 57 秒和 66 秒。要卡点就出完再 `cut` |
| 下载地址带签名、会过期 | 拿到回包立刻下载；回包里 `duration` 常常是空的，用 ffprobe 自己补 |
| 同步接口很慢 | 实测有到 2 分 50 秒的，HTTP 超时至少设 300 秒；超时不要重发（见核心原理第 7 条） |
| `loudnorm` 会把采样率升到 192k | 命令里加 `-ar <原采样率>`，把采样率固定回原文件的 |
| 底噪大的录音去不掉静音 | 阈值从默认 -45dB 调到 -35dB 再试 |
| 本机请求 `127.0.0.1` 被系统代理截走 | 如果你把生成做成本地服务再转发，Python 访问本机地址时用 `urllib.request.ProxyHandler({})` 绕开系统代理 |
| 素材库还不会放音频 | 生图审核页如果只会用 `<img>` 显示条目，音频会是一张坏图。先给音频单独一个素材库目录，或者让页面按 `media` 用 `<audio>` 播放 |
| 批量生成 | 先报总数和总价（按配置里的 `price` 算），人批了总数再跑 |
| `normalize` 只跑一遍时有偏差 | 单遍 `loudnorm` 实测会差 1–2 LU（纯正弦测试音差得更多）。跑完用 `loudnorm=print_format=json` 测一次实际响度，打出来给人看；要求更准就做两遍（第一遍测量，把 `measured_*` 填进第二遍） |
| `cut` 的切点 | `-ss` 放在 `-i` 后面、用 `-t` 写时长，切点才准；mp3 有帧对齐误差，核对时长允许差 0.1 秒 |
| 没人可问时 | 「先查清」里查不清的项，按默认值继续（mock、示例署名），把每个假设写进一份清单交给人核对，不要卡住不动 |
| 配音、音效 | 作者这边还没接真平台。先做一个 `command` 适配器：配置里写一条命令模板，占位符 `{text_file} {out} {voice} {seconds} {model}`，值都要做 shell 转义；以后有 HTTP 平台了，照配乐的写法加一个适配器 |

## 落地顺序

1. 装好 ffmpeg，先写 `info`、`cut`、`convert` 三条，用 `ffmpeg -f lavfi -i "sine=frequency=440:duration=5" test.wav` 生成测试音，跑通。
2. 补齐其余本地命令。每条都用测试音跑一遍，核对输出时长（比如 cut 1.5–8 秒，输出应该是 6.5 秒）。
3. 写 mock 生成和 `lib` 五条命令，跑一遍：登记 → 审核 → 定稿 → 反馈 → 查询。再故意用 AI 的署名去 review、用「…」去 approved，确认都被拒。
4. 接真平台：先 `--dry-run` 把拼出的请求给人看，人用原话批准后真跑一首，听过再定稿。
5. 把上面几步写成一个自检脚本（临时目录、测试音、mock、临时素材库，不联网不花钱），以后改了代码先跑它。
