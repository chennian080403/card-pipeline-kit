# 生态契约（image-card-pipeline × video-card-pipeline）

两个 skill **不互相 import 代码**，只按这份契约读写同几份文件。各自单独能用；放在一起时靠这里的约定打通。
改契约要升版本号，两边一起改。

## 1. 一份配置，各取所需

```
project.yaml
  project: {name, root}        共用
  paths.sources                共用：真源登记表
  library                      共用：素材库
  paths.cards / blocks / ...   生图专用（视频不读）
  models / providers / rules   生图专用（视频不读）
  video:                       视频专用（生图忽略这一整段）
```

- 只用生图：没有 `video:` 段。
- 只用视频：没有生图的 `paths.cards`，关键帧写文件路径或 `source:`。
- 一起用：在生图的 project.yaml 上跑 `vpipeline.py init`，只追加 `video:` 段（先备份）。
  也可以两份文件分开，用 `video.link.image_config` 指过去；想让生图那边看到哪些素材正被当关键帧，再在生图的 project.yaml 写 `link.video_config` 指回视频项目（§5.1）。

## 2. 真源登记表（sources.yaml）

```yaml
characters:
  <名>: {source: <路径|URL>, face: <路径>, role: <参考图说明>, pack: <资产包>, history: [...]}
props:
  <名>: {source: ..., role: ...}
styles:
  <名>: {source: ..., role: ..., prompt: <英文描述>, look: 画风|氛围}   # prompt / look 可选（v1.3）
```

### 真源种类（v1.4）

```yaml
# project.yaml 顶层（两边共用；视频的配置没写时读 video.link.image_config 指的那份）
sources:
  kinds:
    scenes: {label: 场景, as: scene}                   # 新种类：卡里写 scenes: [城门]
    lenses: {label: 镜头语言, as: lens, text: true}    # 带描述：条目的 prompt 拼进提示词
    props:  {label: 器物}                              # 也可以只给默认种类改个显示名
```

- 键名 = `sources.yaml` 的节名 = 卡里的字段名。默认三种 `characters`（人物）、`props`（道具）、`styles`（风格，`text: true`）总在，只能加、能改显示名，不能删。
- `label`：页面上的中文名，也是资产包的一级目录。`as`：`promote --as <as>:<名>` 用的单数（写节名也认）。`role`：参考图默认说明，`{name}` 换成条目名。
- `text: true`：条目带 `prompt` 描述，引用它的卡拼进提示词。生图同时把样张作参考图；视频只拼字。
- `text` 不写或 false：样张作参考图（生图出图时、视频 reference 模式随片送出）。
- `characters` 两边都有特殊处理（卡里是 `{name, lookup, position, expression}`、`face` 特写），其余种类卡里都是名字列表。
- 把某种类从配置里拿掉，`sources.yaml` 里已登记的条目不丢，审核页照常显示（标签用节名）。

- 路径相对 `project.root`。
- **写入方只有生图**（`promote`，旧值进 `history`），视频只读。
- 单独用视频时，可以手写这份文件。
- 视频卡的 `styles: [名]` 读 `styles.<名>.prompt` 拼进视频提示词（v1.3）；名字没登记 → `validate` 报错，登记了但没 `prompt` → 只提示。`look` 视频不用。

## 3. 资产引用写法

视频卡里所有「指向一张图 / 一段视频」的字段（`first_frame`、`keyframes[]`、`last_frame`、`refs.*`）都可以写下面几种：

| 写法 | 含义 |
|---|---|
| `path/x.png` 或绝对路径 | 文件 |
| `https://…` | URL，原样传 |
| `source:<节>/<名>`：节是 `sources.kinds` 里任一种（默认 `characters` / `props` / `styles`）；`source:<名>` 简写 `source:characters/<名>` | 真源登记表 |
| `image:<图卡>` | 生图那张卡 **pick 的第一张**（读生图的 `output/review/<卡>.json`） |
| `image:<图卡>/<候选名>` | 生图指定候选 |
| `library:<id>` | 本地素材库条目 |
| `take:<视频卡>/<条名>` | 本项目的一条视频（视频参考 / 续写） |

每条解析结果都记 `origin`（来自哪里）和 `sha256`，写进 take 的元数据，登记素材库时变成 `derived_from`。

## 4. 素材库条目

两边登记同一种条目，**永远是草稿**（`production_approved: false`），定稿只能由人来做。

- **本地素材库**（`library.kind: local`）：`<root>/library.json` 的格式是 `image-card-pipeline/local-library-v1`。为兼容生图早先写入的条目，名字保持不变。
  - 共有字段：`id`（sha256 前 8 位）、`title`、`batch`、`file`、`sha256`、`prompt`、`model`、`status`（`draft|final|rejected|pending`）、`production_approved`、`decisions`。
  - 视频追加：`media: video`、`derived_from: [{label, origin, sha256}]`、`review`（审片台结论）、`card`。
  - 生图条目没有 `media` 字段，按扩展名推断。
- **http 素材库**：`/api/catalog` 拿 token，`/api/agent/ingest` 登记。文件不在素材库目录下时，先 `/api/agent/drop` 投递字节。

## 5. 关键帧：「只有定稿能当关键帧」

一条片子由几张**关键帧**撑起来（关键的参考图：首帧、中间几张、尾帧，可能 2 张，也可能 4、5 张）。视频卡这样写：

```yaml
first_frame: image:01-01            # 简写：0 秒那张
keyframes:                          # 任意张，按时间顺序
  - image:01-02                     # 不写 at：在前后两张之间均分
  - {ref: library:3fa2c1d0, at: 3}  # at：第几秒
last_frame: image:01-03             # 简写：最后一秒那张（= duration）
```

- 没写 `first_frame` 时 `keyframes` 第一张算 0 秒；两张以上且没写 `last_frame` 时最后一张算结尾。时间必须递增、落在 0 到 duration 之间，否则 `validate` 报 PROBLEM。
- 关键帧必须从 0 秒开始：第一张写了 `at` 且不是 0（或只有尾帧）时 `validate` 报 PROBLEM（要么补首帧，要么挪进 `refs.images`），不会悄悄变成文生视频。
- 引用写法同 §3。解析在 card-core `core/keyframes.py`（`timeline(card)`），两边用同一份。
- 送给模型：大多数模型只吃首尾两帧，**中间几张不会送出**，也不冒充参考图（`validate` / `prompt` 如实提示「中间 N 张不送」）；有首帧 + 中间帧、没尾帧时这类模型按首帧模式出。模型能吃更多就在模型上写 `max_keyframes`。网页交接包按时间排号放全部关键帧（不再重复放进参考图）；命令行供应商另有 `{keyframes}`、`{keyframes_json}` 占位符。

**定稿就是定关键帧。** `video.rules.keyframes: final`（旧名 `first_frame: final` 照认）时，**每一张**关键帧都必须满足下面任意一条：

1. 文件在 `rules.final_dirs` 里；
2. 本地素材库里有同内容（sha256）、`status: final` 的条目；
3. 在 `final_dirs` 里找得到同内容的副本。生成时会改用这份定稿副本。

都不满足时，`validate` 报 PROBLEM（指出是哪一张），`generate` 拒跑。

### 5.1 生图这边看得到哪些素材正被当关键帧

生图**只读**视频卡（不写）。视频卡的位置：同一份 project.yaml 带 `video:` 段时读 `video.paths.cards`（默认 `video-cards`）；两份文件分开时在生图的 project.yaml 写 `link.video_config: <视频的 project.yaml>`。都没有就不标。

认法和视频解析引用一致：`library:<id>`；`image:<图卡>`（那张卡 pick 的第一张）/ `image:<图卡>/<候选>`（按前缀）；文件路径按内容（sha256）比，定稿副本也算。`source:` / `take:` / URL 不是素材库条目，不标。

生图的 `/api/state` 每条素材多一个 `keyframe_of: [{card, title, at, slot}]`（slot = first | middle | last）。审核页素材区标「关键帧 · N」，详情里列出是哪几张视频卡；移回原处、丢弃、撤回定稿前弹确认。

## 6. 反馈回流

视频判「关键帧有问题」时，执行 `feedback <卡> reject --root-cause frame`。卡里每张来自 `image:<图卡>` 的关键帧（同一张图卡只算一次），视频都会调用生图的**公开命令**：

```
pipeline.py --config <生图配置> feedback <图卡> revise --reason "[视频 <卡> <条>] <原因>" --root-cause frame --by video-card-pipeline
```

视频不直接改生图的卡文件。生图卡的格式由生图自己维护。

## 7. 共同规矩

| 规矩 | 生图 | 视频 |
|---|---|---|
| AI 不定稿 | `register` 只登记草稿 | 同左；审片结论也只是记录人的话 |
| 花钱前确认 | `--approved` 必须是人的原话：空、少于 4 个字、像占位符（`...`、`…`、`<…>`、`TODO`、待填、占位）一律拒绝 | 同左，口径一致（card-core `jobs.approval_problem`）；`free: true` 的模型免确认 |
| 看图结论只认主模型和人 | `review --by` 只收 `rules.reviewers`（默认 claude / human）；`output/review/<卡>.json` 每条多 `"by"` 字段，向后兼容 | 审片结论只收人署名（Agent 署名 403）；视频读生图 `pick` 不受 `by` 影响 |
| 不重复花钱 | `ok` 幂等跳过 | 同左；`submitted` 只续轮询，不重提 |
| 状态不明不重提 | `unknown` 停下：请求发出后出任何异常（超时、断线、5xx、回包坏、200 没图、结果下载失败）都记 `unknown`，进程死掉留下的 `running` 也记 `unknown`；只有确定没发出去才记 `failed`；人看过以后 `forget <卡> <序号> --why` | 同左；提交前先记 `submitting`，提交中出任何异常（超时、断网、Ctrl-C）记 `unknown`，两者都不重提；人看过以后 `forget <key> --why` |
| 不覆盖 | 候选只有一份 | take 只追加；后期出派生条 `<条>__<钩子>` |
| 密钥 | 只从环境变量来 | 同左（另可只读一个 new-api 网关的 sqlite 库） |

## 8. 底座（card-core）

共用代码的唯一源头在和 Skill 并排的 `card-core/core/`，复制进各 Skill 的 `scripts/core/`（`sync.py` 同步，`--check` 查走样）。
复制而不是 import，是为了每个 Skill 单独装也能用。当前接入：视频（全部模块）、生图（只用 `sources`、`keyframes`）。
生图的 `library.py` / `save_version` 与它同源，暂不替换。本契约的正本在视频 Skill，生图 Skill 带一份同样的副本（也由 `sync.py` 复制）。

## 9. 审片协议

- 结论：整条（段 0）或逐段，`good`（满意）/ `bad`（不满意）/ `discard`（放弃）/ 空（撤回）；满意的地方、不满意的地方分开写。
- 写入门禁：`X-Review-Token`（缺或错 403）、`X-Actor` 为 `user@browser` 或 `human:<名>`（缺 400，Agent 署名 403）、revision 过期 409。
- 留痕：每次写入追加一行 `activity-log.jsonl`，不改写。
- 汇总给流水线：整条结论优先；否则全满意 = 通过，有不满意没满意 = 不通过，混合 = 待定；放弃 = 不通过 + discard。
- 撤回后 `sync`：清掉那条的结论；由 `sync` 带进 pick 的那条移出 pick，人手动 `pick` 的保留。
- 自带审片页、放映厅插件都按这个协议；别人自带的审片台写插件时，`fetch` 直接返回 `core.station.aggregate(...)`。
