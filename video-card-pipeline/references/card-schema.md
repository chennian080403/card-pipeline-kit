# 视频卡字段

一张卡 = 一个镜头 = 一条片子。模板 `templates/card.template.yaml`。存盘时空字段不写；人写的在上，流水线记录（`status / hold / created / feedback / sources`）在分隔线下。
旧卡的 `act` 当作 `group` 读，`takes / chosen` 不再写进卡（改记在 `video-output/`）。

| 字段 | 说明 |
|---|---|
| `id` / `title` | 卡 id（文件名以它开头）/ 一句话标题 |
| `group` | 同一幕/组；审片台按它分组。接的是自己的放映厅插件时，按插件要求的分组名写 |
| `model` | `video.models` 别名；空 = `models.default` |
| `keyframes` | 关键帧（契约 §5）：任意张，按时间排；每项写引用，或 `{ref: <引用>, at: <秒>}`，不写 `at` 就在前后两张之间均分。没写 `first_frame` 时第一张算 0 秒，两张以上且没写 `last_frame` 时最后一张算结尾 |
| `first_frame` / `last_frame` | 关键帧两端的简写：0 秒那张 / 结尾那张（资产引用，见契约 §3）。`rules.keyframes: final` 时所有关键帧都必须是定稿 |
| `characters` / `props` | 真源名：作参考图（reference 模式时随片送出） |
| 项目自加的种类（`sources.kinds`） | 例如 `scenes: [城门]`：不带描述的种类作参考图随片送出；`text: true` 的只把描述拼进提示词 |
| `styles` | 画风 / 氛围真源名（`sources.yaml` 的 `styles`）：把它的 `prompt` 描述拼进提示词，不送样张；样张要送就写进 `refs.images: [source:styles/<名>]` |
| `refs.images / videos / audios` | 额外参考素材，写法同上；`take:<卡>/<条>` 引用本项目的片子 |
| `mode` | `auto`（默认）按素材推断：有首帧且两张以上关键帧 → `keyframes`，只有首帧 → `first_frame`，只有参考 → `reference`，都没有 → `text`；也可写死（只认 `auto / keyframes / first_frame / reference / text`，别的 `validate` 报 PROBLEM）。不认识的顶层字段（多半是拼错）`validate` 给 note |
| `duration` / `aspect` / `resolution` | 秒 / `16:9`、`9:16` … / 留空用模型默认。模型可在 `models:` 里标 `min_seconds`、`max_seconds` |
| `takes` | 默认出几条（`--budget` 覆盖，总数不是增量） |
| `motion` | 英文：运镜、谁做什么、表情、环境里什么在动。参考图已定画面时只写动作和镜头，不重复描述画面 |
| `hard_rules` | 本镜额外硬规则（英文句） |
| `motion_tail` | 覆盖 `video-blocks/motion_tail.txt` 的固定尾巴 |
| `subtitle` | 台词/字幕，只给剪辑单用，不进提示词 |
| `audio` | true 时请求带声（画布模型支持切换时） |
| `hold` | 非空即锁 |

提示词 = `video-blocks/style.txt` + `styles` 等带描述种类的描述 + `motion` + `hard_rules` + 尾巴（`#` 开头的行不进）。

## take（一条片子）的元数据 `video-output/takes/<条>.json`

`name`（`<卡>_<模型>_<n>`，后期派生 `<条>__<钩子>`）、`file`、`card`、`group`、`model`、`provider`、`mode`、`prompt`、
`refs`（每个引用的 `origin` 与 `sha256`）、`card_fingerprint`、`probe`（时长/分辨率/fps/音轨）、`sheet`、`run_dir`、
`derived_from`、`station`（审片台回执）、`library`（登记回执）、`board`（画布节点）。

## 其余落盘

```
video-output/
  manifest.json        jobs[<卡>|<模型>|<n>] = submitting | submitted | waiting_import | ok | failed | unknown（+ handle 可续轮询）+ approvals；每次只在锁里改一条
  runs/<卡>-<模型>-<n>-<时间>/   job.json / 请求响应留档
  takes/  sheets/  review/<卡>.json（ai 自审 + human 审片结论 + pick）
  station/station.json  自带审片台的结论（builtin 时）
  handoff/<卡>_<模型>_<n>/   网页交接包
  final/<剪辑单>_vNN.mp4
```
