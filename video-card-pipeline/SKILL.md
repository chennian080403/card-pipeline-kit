---
name: "video-card-pipeline"
description: "卡片驱动的通用生视频流水线：一张视频卡（关键帧：首帧/中间几张/尾帧，角色真源/参考素材/运镜动作）驱动 出片 → 抽帧自审 → 送审片台（人看片、给结论、写备注、打时间点标注）→ 同步结论择优 → 登记素材库 → 剪辑单拼成片 全流程。出片入口可插拔：mock、任意命令行、网页交接包，另带两个可选示例（一个出片 API、自租云 GPU）。单独能用；和 image-card-pipeline 共用 project.yaml / 真源 / 素材库时组成生态（关键帧写 image:<图卡>，关键帧问题自动回写生图卡；生图素材区标出哪些图正被当关键帧）。当用户要做'视频卡出片/图生视频流水线/关键帧 / 首尾帧批量出片/审片与放映厅/剪辑单拼片/把生视频接到新项目'时使用。"
---

# video-card-pipeline · 卡片驱动的通用生视频流水线

**一张卡对应一个镜头，也就是一条片子。** 卡里写清关键帧（首帧、中间几张、尾帧）、谁出场、怎么动；流水线据此解析参考 → 拼提示词 → 出片 → 抽帧 → 送审 → 择优 → 登记 → 上看板 → 拼成片。
人只在三处出现：**开头**确认卡、批付费；**审片台**给结论、写备注、打标注；**素材库**点定稿。

它不绑定任何项目。项目靠 `project.yaml` 的 `video:` 段接入，密钥只从环境变量读。从零接入照 `references/quickstart.md` 做。
`profiles/` 放本机专属的接入档案（自己的放映厅、素材库、超分钩子……），导出给别人时不带；有的话先读 `profiles/*/README.md`。

## 分工

和生图 SKILL.md「一、底座」同一套角色。下面只按角色写；每个角色具体是哪个工具、怎么调用，写在本 Skill 目录的 `roles.md`（照 `roles.example.md` 填）。**没有 `roles.md` 时，几个 AI 角色都由主模型自己做**，规矩不变。

| 角色 | 是谁 | 接什么活 |
|---|---|---|
| 人 | 用户 | 批预算、审片台给结论、否决原因、沉淀真源、素材库定稿 |
| 脚本 | 本 Skill 的命令 | 能用规则判的都归它：时长、分辨率、引用、关键帧是否定稿、提示词长度、任务状态、存版本 |
| 主模型 | 主会话（和你对话的那个 AI）；要判断的子任务可派**判断子代理** | 所有看片下判断的活，以及写卡、改 motion、归因、改关键代码 |
| 文字助手（可选） | 便宜快的模型 | 不看图的杂活：批量建卡、批量改字段、写剪辑单初稿、写测试、改文档、跑命令、查资料 |
| 看图助手（可选） | 能看图的便宜模型 | 要看图的杂活：抽帧粗筛、批量描述帧、给参考图打标签；结论不算数 |

四条原则：
1. **看片下判断归主模型**：`sheet` 后的 AI 自审 `review`、择优建议、归因。看图助手只做看图的杂活，结论不算数。
2. **文字助手和看图助手交回的东西由主模型核对后才算数**。
3. **批钱和定稿只归人**：审片结论只从审片台来；AI 只照抄人的原话填 `--approved`。
4. **能用脚本判的不让模型判**。

| 环节 | 谁做 | 怎么分 |
|---|---|---|
| 建卡、写 motion | 主模型；批量交文字助手 | 镜头表批量转卡、同一字段批量改交文字助手，主模型抽查 |
| `validate` / `prompt` / `doctor` | 脚本 | 主模型读拼好的提示词，看口径对不对 |
| 批预算 | 人 | 回一句"X 出 N 条"，原话写进 `--approved` |
| `generate` / `collect` | 脚本 | 平时主模型敲；人批了总数后大批量可交文字助手跑，`--approved` 照抄人的原话 |
| `sheet` 抽帧 | 脚本 | 给主模型和人看 |
| AI 自审 `review` | 主模型 | 条数多时可先让看图助手按清单粗筛，结论仍由主模型给 |
| 审片结论 | 人 | 只在审片台；`sync` 取回 |
| 归因 `feedback --root-cause` | 主模型 | 判断该改卡、改关键帧（回写生图卡）、换模型还是换参考 |
| 后期 `post`、`register`、`assemble`（`board` 只在装了看板插件时用） | 脚本 | 剪辑单初稿可交文字助手，主模型定 |
| 定稿 | 人 | 只在素材库点 |
| 改本 Skill 的代码 | 主模型定方案、改关键逻辑（大改可派判断子代理）；文字助手写测试、改文档、跑自检 | 文字助手交回的改动由主模型复核 |

## 架构

```
卡片层    video-cards/*.yaml       一张卡 = 一个镜头（字段见 references/card-schema.md）
口径层    video-blocks/            style.txt / motion_tail.txt（每条提示词的固定头尾）；卡里 styles: [名] 再拼上画风真源的描述
引用层    refs.py                  路径 / source:真源 / image:图卡 / library:条目 / take:视频条
执行层    providers.py             mock · handoff（网页）· command（任意 CLI）· 可选示例 agnes（出片 API）/ compshare（自租云 GPU）；toonflow 是外置的画布插件
          cloud.py + remote/       云 GPU 开关机（示例走 compshare CLI）· 盒子上跑的 h3_run.py（vllm / comfyui）
审片层    stations.py              builtin（自带审片页）· plugin（你的放映厅，样板 templates/station_plugin.example.py），协议见 core/station.py
存档层    core/library.py          素材库草稿登记（与生图同一契约）
看板层    video.board              可选看板插件（默认不装）：只放审过的成片
后期层    hooks.post + assemble    超分等任意命令 · 剪辑单 → 叠化/配乐/字幕成片
底座      scripts/core/            card-core 副本：版本、任务状态机、审片协议、素材库、真源种类（源头是并排的 card-core/）
```

命令行是 `vpipeline.py`（只有参数和入口），命令按职责分在 `admin.py`（建项目、卡、自检）、`produce.py`（出片）、
`reviewing.py`（审片、反馈、后期）、`delivery.py`（素材库、成片，画布看板走插件）；`cards.py` 管卡，`store.py` 管落盘。

## 底座（card-core）

两条流水线共用的"怎么做"，一处写、复制进各 Skill（`scripts/core/`，不要改副本）。改源头后跑
`python3 card-core/sync.py`；`sync.py --check` 发现副本走样就报错。生图也接入了，只用 `sources`。只装了视频时没有源头，副本照常用。

| 模块 | 管什么 |
|---|---|
| `versioning` | 项目文件夹是**自己的** git 仓库时，写命令前后自动存版本；只是躺在别的仓库里不存；`video.rules.autosave: false` 关掉 |
| `jobs` | 任务状态机：ok 跳过、submitted / waiting_import 只续不重提、unknown 停下等人 `forget --why`；付费闸门 |
| `station` | 审片协议（照放映厅）：整条 / 逐段、满意 / 不满意 / 放弃 + 满意和不满意的地方分开写；写入要 token、人的署名、当前 revision；追加留痕；汇总成流水线的 通过 / 不通过 / 待定 |
| `library` | 素材库（local / http）草稿登记，条目一律草稿 |
| `sources` | 真源种类（`sources.kinds`） |
| `jsonio` | 原子写 json、追加日志 |

## 自检

`python3 scripts/selfcheck.py`：临时项目 + mock，不联网不花钱，约 12 秒，覆盖铁律、审片协议、生图联动契约、底座一致、审片插件样板。
旁边没装生图或没有 card-core 源头时，相关几项记「跳过」不算失败。`--extra <档案自检.py>` 加跑接入档案自带的检查。改完代码先跑它。

**谁负责什么**：
- **审片台**：看片、给结论、写备注、打标注都在这里做，人只在这里点一次。
- **素材库**：存审过、以后要复用的东西，条目带上审片结论和来源。
- **看板**：不在主线里。可以挂一个看板插件（例如 Toonflow 画布，放在并排的 `plugins/画布/`），同时提供出片入口和看板；不配置就完全不加载。

## 单独用 / 一起用

| | 关键帧怎么写 | 真源 | 素材库 |
|---|---|---|---|
| 只用视频 | 文件路径、`source:<名>` | 自己手写 `sources.yaml` | 可选，`local` 或 `http` |
| 和生图一起 | `image:<图卡>`：自动取生图 pick 的那张；规则要求定稿时，自动换成定稿副本。生图素材区会标出哪些图正被当关键帧（生图那边写 `link.video_config`，或两边同一份 project.yaml） | 生图 `promote` 写进来，视频直接用 | 同一个库，图和视频按 `media` 区分，`derived_from` 记来源 |

接法：在生图的 `project.yaml` 上跑 `vpipeline.py init`，只追加 `video:` 段（会先备份）。契约见 `references/ecosystem-contract.md`。

## 快速开始

```bash
SKILL=<本 Skill 所在目录>          # 例：~/.claude/skills/video-card-pipeline
V="python3 $SKILL/scripts/vpipeline.py --config ./project.yaml"

$V init                                   # 新项目；或给生图项目追加 video: 段
$V new-card 01-01 城外初见 --first-frame image:01-01 --group 第一幕
#   编辑卡里的 motion（英文，只写运镜与表演；台词字幕走剪辑单）
$V validate 01-01                         # 字段、引用、关键帧时间线和是否定稿、时长范围、提示词长度
$V generate 01-01 --budget 2              # 默认 mock，不配密钥也能跑通全流程
python3 $SKILL/scripts/review_web.py --config ./project.yaml --port 8861   # 自带审片页（整条 / 逐段）
$V sync                                   # 取回人的结论：通过 → pick，不通过 → 取消 pick
$V register 01-01                         # pick 的条 → 素材库草稿
$V assemble 第一幕                        # cuts/第一幕.yaml → 成片
```

## 命令表

| 命令 | 作用 |
|---|---|
| `init` | 建项目；已是生图项目就只追加 `video:` 段 |
| `new-card <id> [标题] [--first-frame --group]` | 从模板建卡 |
| `cards` / `show` / `status` | 列卡 / 打印卡 / 卡·任务·结论·pick 总览 |
| `validate <卡…>` | 卡合格后状态记为 confirmed |
| `prompt <卡>` | 打印拼好的提示词 |
| `generate <卡…> [--model --budget N --approved … --no-wait --post 钩子]` | 提交并（默认）等出片。`--budget` 是总条数 |
| `collect [卡…]` | 继续轮询 submitted 的任务（崩了、超时了都能接上；**绝不重提**） |
| `import <卡> <mp4> [--model] [--move]` | 收回网页交接包的片子，或把任意外部片子收成一条；默认复制不删源，`--move` 才删 |
| `forget <key> --why` | 人看过 unknown / failed / 卡住的 submitting 任务后才能清掉 |
| `sheet <条或卡>` | 首/中/尾帧拼图，给 AI 自审、给人扫一眼 |
| `review <条> pass\|fail\|borderline --note` | **AI 自审**（人的结论只从审片台来） |
| `submit <条…>` / `sync [卡…]` | 送审片台 / 取回人的结论（结论、备注、标注） |
| `pick <卡> <条…> [--remove]` | 手动挑选 |
| `feedback <卡> accept\|reject\|revise --reason --root-cause card\|model\|frame\|ref` | 记反馈与归因；`frame` 时每张来自 `image:` 的关键帧都回写到它的生图卡 |
| `post <条> <钩子>` | 跑后期（超分等），出派生条 `<条>__<钩子>` |
| `register <卡>` | pick 的条 → 素材库草稿（带审片结论与 `derived_from`） |
| `board push <卡…>` / `board status` | 画布看板（可选插件；没装时 `status` 只报“没有看板”） |
| `assemble <剪辑单>` | 叠化、配乐、烧字幕成片，自动送审片台 |
| `providers [--check]` / `doctor` | 入口与凭据 / 项目体检（含版本状态） |
| `cloud status\|up\|down [--create [--price-ok]] [--approved "张三 10-23 批开机 2 小时"]` | 云 GPU 开关机：`up` 必须 `--approved` 人的原话（口径同规矩第 2 条）；`--create` 只打印配置和报价，用户同意后加 `--price-ok` 才建机；开机后设兜底定时关机、推送远端脚本、等模型服务健康 |

## 出片入口（可插拔，见 references/providers.md）

| kind | 用途 | 自动化 |
|---|---|---|
| `mock` | ffmpeg 合成可播的片子，不联网 | 全自动 |
| `agnes` | 可选示例：一个真实的出片 API（Agnes） | 全自动；429/503 退避并轮换 key |
| `handoff` | 网页平台：生成交接包（全部关键帧按时间排号 + 提示词 + 参数），人在网页上生成，`import` 收回 | 半自动 |
| `command` | 任意 CLI：命令模板写出 `{out}` | 全自动 |
| `toonflow` | 可选插件，不随本 Skill 发（放在并排的 `plugins/画布/`） | — |
| `compshare` | 可选示例：自己租云 GPU 跑开源模型（以优云智算 + MiniMax H3 为例，换别家照 `cloud.py` 改）；`cloud up/down` 管开关机 | 全自动；盒子没开就拒，从不自己开机 |

## 不可越过的规矩

1. **人不得被 AI 替代**：只有人能定稿和审片；`review` 只记 AI 自审。素材库条目一律是草稿。审片台拒收 Agent 署名的结论。
2. **付费生成前必须确认**：非 `free` 的模型要加 `--approved`，写人的原话（如 `"张三 10-23 批 01-01 出 2 条"`），并按总条数封顶。空、少于 4 个字、像占位符（`...`、`<…>`、TODO、待填）的一律被拒。
3. **不重复花钱**：`ok` 的任务跳过；`submitted` 的只续轮询；超时后任务保留，`collect` 接着查。
4. **`unknown` 不许重提**：可能已经出片或已扣费，停下来让人看 run 目录。提交前先记 `submitting`，提交中出任何异常（超时、断网、Ctrl-C）都记 `unknown`；两个会话同时跑也只提交一次（manifest 每次只在锁里改一条）。
5. **只有定稿能当关键帧**：开 `rules.keyframes: final`（旧名 `first_frame: final` 照认）时，首帧、中间几张、尾帧每一张都要是定稿（契约 §5）。
6. **提示词只写镜头与表演**：台词、字幕、歌词走剪辑单。
7. **不覆盖**：一条片子只存一份；后期另出派生条；成片按 `_vNN` 递增。不生成 `.bak`，历史靠 git。
8. **同一个事实源只有一份**：真源在 `sources.yaml`，只在生图审核页 / `promote` 定；看板（插件）只展示，不定任何东西，也从不改看板的数据。

## 目录

```
video-card-pipeline/
  SKILL.md · project.example.yaml · roles.example.md（分工填空；本机的 roles.md 导出不带）
  scripts/  vpipeline.py(CLI) admin.py produce.py reviewing.py delivery.py cards.py store.py common.py
            config.py refs.py providers.py stations.py media.py review_web.py selfcheck.py cloud.py
            remote/h3_run.py               盒子上跑的出片脚本 · remote/fake_compshare.py 自检用假 CLI
            core/                          card-core 副本（不要改，改源头再 sync）
  web/review.html                  自带审片页（整条 / 逐段）
  templates/                       视频卡 / 剪辑单 / motion_tail / sources 模板 · station_plugin.example.py（审片插件样板）
  profiles/                        本机专属接入档案（导出不带）
  references/
    quickstart.md                  从零接入你自己的项目（单用视频 / 和生图一起）
    ecosystem-contract.md          与生图的共同约定（先读这个）
    card-schema.md                 卡片字段
    providers.md                   入口契约与实测坑
    workflow.md                    一轮怎么跑 + 接入新项目
```
