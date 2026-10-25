# 从零接入你自己的项目（视频）

照这一页做完，你就有一个自己的视频项目：写视频卡 → 出片 → 在审片页看片、给结论 → 进素材库 → 按剪辑单拼成片。
先用模拟出片（mock，不联网、不花钱）跑通一遍，再接你自己的出片服务。

两种用法：
- **只用视频**：关键帧（首帧、中间几张、尾帧）直接写图片路径。
- **和生图一起用**：关键帧写 `image:<图卡>`，自动用生图那边点了「要」的图；真源和素材库两边共用一份。

## 0. 准备

| 要什么 | 干什么用 | 怎么装 |
|---|---|---|
| Python 3.9 以上 | 跑脚本 | 系统自带或 python.org |
| PyYAML | 读配置和卡（必须） | `pip3 install pyyaml` |
| ffmpeg / ffprobe | 模拟出片、抽帧、拼成片（必须） | macOS `brew install ffmpeg`，其他系统见 ffmpeg.org |
| Pillow | 参考图压缩后直接随请求发（建议装） | `pip3 install pillow` |
| git | 每次改动自动存版本（可选） | 系统自带或 git-scm.com |

## 1. 放好 Skill

把 `video-card-pipeline/` 放进你 AI 工具的 Skill 目录，例如 Claude Code 是 `~/.claude/skills/`。
和生图一起用，就把 `image-card-pipeline/` 放在**同一个目录**里，两个文件夹并排放。视频会自动找到它。

跑自检（约 15 秒，不联网、不花钱）：

```bash
python3 ~/.claude/skills/video-card-pipeline/scripts/selfcheck.py
```

最后一行没有失败就行。只装了视频时，「生图联动」「card-core 源头」两项会显示「跳过」，这是正常的。

## 2. 建项目

```bash
SKILL=~/.claude/skills/video-card-pipeline
cd 你的项目文件夹
python3 $SKILL/scripts/vpipeline.py --config project.yaml init
```

- 空文件夹：建一个只有视频的项目。
- 已经是生图项目（有生图的 `project.yaml`）：只在它后面追加一段 `video:`，真源、素材库两边共用。

## 3. 填 project.yaml 的 video: 段

每一项在 `project.example.yaml` 里都有注释：

1. **出片入口**（`video.providers` + `video.models`）：
   - 先不动，`models.default: dry` 就是模拟出片。
   - 有命令行工具的平台：用 `kind: command`，把命令写成模板，用 `{first_frame}`、`{prompt_file}`、`{out}` 这些占位符。
   - 只能在网页上生成的平台：用 `kind: handoff`，流水线把首帧、提示词、参数打成一个交接包，你在网页上生成完，`import` 收回来。
   - 带的两个可选示例：`agnes`（一个出片 API）、`compshare`（自己租云 GPU 跑开源模型）。用不上就删掉。
   - **密钥只写环境变量的名字**，值放进环境变量。各家接口的坑见 `references/providers.md`。
2. **审片台**（`video.station`）：
   - 默认 `builtin`，就是自带的审片网页，什么都不用装。
   - 已经有自己的审片工具：照 `templates/station_plugin.example.py` 写一个插件。这个样板本身就能用：片子放进一个文件夹，人在旁边的 `review.json` 里写结论。
3. **口径块**（`video-blocks/`）：`style.txt` 写每条提示词的开头（画面口径），`motion_tail.txt` 写结尾（比如「保持人脸不变、不要字幕」）。都写英文。
4. **后期**（`video.hooks.post`，可选）：超分、降噪之类，挂任意命令，用 `{in} {out}` 占位符。
5. **规矩**（`video.rules`）：
   - `require_approval: true`：花钱出片必须带上你批准的原话。
   - `keyframes: final`：只允许素材库里定稿的图当关键帧（首帧、中间几张、尾帧都算）。
6. **素材库**（顶层 `library`）：和生图共用。单用视频时建议 `kind: local` + `root: 素材库`。

填完跑体检：

```bash
python3 $SKILL/scripts/vpipeline.py --config project.yaml doctor
```

## 4. 分工

和生图一样：把 `roles.example.md` 复制成 Skill 文件夹里的 `roles.md`，填每个角色用哪个工具。只用一个 AI 就不用填。

## 5. 跑通一遍（模拟出片）

```bash
V="python3 $SKILL/scripts/vpipeline.py --config project.yaml"
$V new-card 01-01 城门初见 --first-frame 首帧.png        # 和生图一起用时写 --first-frame image:01-01
#   打开 video-cards/ 里的新卡，改 motion（英文：镜头怎么动、人怎么演；台词字幕不写这里）
$V validate 01-01
$V generate 01-01 --budget 1                               # 模拟出片免批准；接真入口后要加 --approved "…"
python3 $SKILL/scripts/review_web.py --config project.yaml --port 8861
```

浏览器打开 `http://127.0.0.1:8861`（端口被占时脚本会报错退出，换一个 `--port`，别打开别人项目的审片页），看片，整条或分段给「满意 / 不满意 / 放弃」，写上满意和不满意的地方。然后：

```bash
$V sync                 # 取回你的结论：满意的自动挑出来
$V register 01-01       # 挑出的片子登记进素材库（草稿）
```

一幕的片子都挑好以后，照 `cuts/_template.yaml` 写剪辑单，`$V assemble <剪辑单名>` 拼成片。

## 6. 接真入口之后

- 每次出片前，AI 会先问你批多少条。你回一句话，它原样抄进 `--approved`。
- 片子出了问题，告诉 AI 原因。它会判断该改卡、换模型、换参考还是改关键帧；关键帧来自生图卡的，会自动把问题记回那张生图卡。
- 网络断了、程序崩了：`collect` 接着查，已经提交的任务不会重复提交，不会重复花钱。

更多：命令全表在 `SKILL.md`；视频卡字段在 `references/card-schema.md`；和生图的约定在 `references/ecosystem-contract.md`。
