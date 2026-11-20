# 工作流

一轮怎么跑。人只在三处出现：**开头**（确认卡、勾参考、批预算）、**补充**（否决时一句原因）、**验收**（素材库点定稿）。其余自动。

## 一轮

```bash
PL="python3 $SKILL/scripts/pipeline.py --config ./project.yaml"

# ── 开头（人）───────────────────────────────
$PL new-card 05-01 "婚席血战_远景"      # 或手写 yaml
$PL validate 05-01                     # 真源在不在、字段齐不齐、有没有 hold
$PL compose 05-01 --var 介质=Illustration   # 拼词 + 报字数
#   → 人看提示词与参考图，确认这张卡要什么

# ── AI 自主 ─────────────────────────────────
$PL generate 05-01 --budget 2 --approved "用户 10-11：05-01 这批跑 2 张"
$PL sheet 05-01                        # 候选 + 参考图条拼图
$PL review 05-01 05-01_main_1 pass --note "体态对，眼神对"
$PL review 05-01 05-01_main_2 fail --note "多了一只耳坠"
#   → 主模型提议「建议要 05-01_main_1」，人去审核页看

# ── 验收（人）───────────────────────────────
#   审核页点「要」= review pass + pick + register 三步合一（命令行：keep 05-01 05-01_main_1 --by human）
#   登记的是草稿；人在素材库点定稿
```

## 什么时候人必须出现

| 时点 | 人做什么 | 不做的后果 |
|---|---|---|
| 生成前 | 确认卡口径、勾定参考、批预算 | AI 会按过期口径烧钱 |
| 否决时 | 给一句原因 | 归因断链，同样的错会重犯 |
| 验收 | 在素材库点定稿 | 草稿永远进不了素材库 |

`feedback` 把"为什么否"记回卡里，是下一轮改卡/改真源的依据。

## 幂等与重跑（重要）

- `generate --budget N` 里 **N 是这张卡+这个模型的总张数**，不是增量。已经 `ok` 的序号直接跳过；要更多图就抬 N。
- `failed` 只记「确定没跑」的：本地就错了（缺密钥、读不到参考图）、连接没建立起来、上游明确 4xx 拒收。重跑会重发那个序号。
- `unknown` = 请求已经发出去，之后出了任何岔子：超时、连接中断、5xx/504、回包不是 JSON、200 但没图、结果下载失败；还有记成 `running` 但进程已经没了的。可能已出图并已计费，**不自动重发**。
  - 回包一到就先原样存进 `output/runs/<卡>-<模型>-<n>/response.json`（再解析 / 下载），错误在同目录 `error.txt`。
  - 人核对过（run 目录 + 供应商后台）确认真没出图，才 `forget <卡> <序号> [--model <别名>] --why "看了什么"`：记录挪进 manifest 的 `forgotten`（带原因），run 目录挪到 `output/runs/_forgotten/`，下次 `generate` 才会重发这个序号。
- 发不出去的不发：`validate` 有 PROBLEM（含引用了没登记的真源、找不到的参考图）时 `generate` 拒跑，`--force` 也不放行；参考图超过 `max_refs` / 内联上限（先自动缩小、转 JPEG 再判断）又没有图床、或图床有一张传失败，整批拒跑并说清是哪张。
- 卡或 `blocks/` 一改，落盘提示词的指纹就对不上，`generate` 直接拒跑，要求重新 `compose`（或显式 `--refresh`）。

## 挂起与作废

口径过期就写 `hold: "<原因>"`。`validate` 报错，`compose`/`generate` 拒跑。改完卡删掉 `hold` 才能继续。宁可锁住，也不要按旧口径出一批废图。

## 多项目并存

一份 `project.yaml` 一个项目，`--config` 指哪份就用哪份（也可用 `IMAGE_CARD_CONFIG`）。同一个 skill 目录服务多个项目，互不干扰；本机专属的档案放在 `profiles/` 下（导出时不带）。

## 与既有系统并存

本 skill 不覆盖任何既有文件：它只**读**卡的目录、真源表、提示词模板目录，**写**也只写自己 `output/` 下的东西。项目原有的出图脚本可以照旧跑，两边共用同一批卡与真源。

## 短效 → 长效：沉淀（promote）

短效系统负责「定下来」，长效系统负责「一直用下去」。

```bash
$PL inspire add <人点名收录的图…> --pack 风格/雪 --approved "用户：这张收进参考区" --note "<来源/想法>"   # 参考区，给 8 位 id
$PL sketch <名> --idea "<灵感一段，英文>" --ref <id> --promote character:<名> --budget 2 --approved "<谁/何时>"   # --promote：来定哪条真源（style 要带 @画风 / @氛围）
$PL sheet s-<名>                                    # 人挑
$PL promote <候选> --as character:<名> --approved "<用户原话+日期>"
```

- 参考区只收**人明确点名收录的具体图**：`--pack` 必须显式写 `<大类>/<子组>`（至少两级），顶层大类在参考区白名单里（默认 `人物 / 道具 / 场景 / 风格`，项目可用 `inspiration.categories` 改，不含 `测试`）；`--approved` 写用户明确收录的原话，存进索引。缺一项整批拒绝，索引和文件都不动。
- 拿本地图当本次参考输入（`sketch --ref` / 卡里的 `refs.files`）**不等于**收进参考区。
- 外部平台生成的试样 / 候选图：先建卡（草图用 `sketch <名> --no-generate --pack …`，镜头用 `new-card`），再跑 `import <卡> <本地图…> --provider <来源> [--model <模型>]` 正式进入试样区 / 出图区（默认未审，同卡按内容哈希去重，不借参考区暂存）。
- 同一候选的 2K / 4K 超分版：跑 `variant add <卡> <候选> <本地图> --tier 2k|4k` 挂在原候选下面（存 `output/variants/`，共用原候选的审核、pick 顺序与视频关键帧引用）；审核页详情里可直接切换「原图 / 2K / 4K」并支持按 `V` 循环对比；如果精修改了脸或构图则算新候选，走 `import` 重新审。
- 移出参考区（可恢复下架）：跑 `inspire remove <完整8位ID>` 或在审核页参考区详情点「移出参考区」（键盘 `3`）。只做软下架——参考区隐藏、`sketch --ref <id>` 拒收，但磁盘原文件保留不断旧卡路径；可用 `inspire list --removed` 查已下架项、`inspire restore <完整8位ID>` 找回。
- 纯技术评测（原图 / 2K / 4K 横评、模型评测、模板试跑）放 `output/` 下的独立评测目录配对比页，**不进参考索引、候选清单或素材库**。源文件在受管候选 / 评测目录（或相对路径段含 `评测` / `upscale` / `超分` / `verify`）时，`inspire add` 直接拒绝，带 `--approved` 也不放行。
- 历史条目没写 `pack` 的照旧按 `风格/<第一个标签>` 读取和分组，不会被新规矩挡下。

- `--as` 四种落点：`character:` / `prop:` / `style:` 写进 `sources.yaml` 对应表；`template:` 写进 `prompt-zone/<路径>.md`（正文取草图卡的 `--idea`，或 `--text`）。
- 必须 `--approved`：定真源是人的决定，AI 不能替。
- 写 `sources.yaml` 前后自动存版本（不再留 `.bak`）；旧 `source` 进 `history`，回退就是把它抄回来，或用 `版本 取 / 回退`。装了 ruamel.yaml 时保留注释，没装会提示注释丢失。
- 卡片指纹包含参考图内容：真源一换，用到它的卡的旧提示词即判过期，`generate` 拒跑，要求重新 `compose`。
- 注意：`--budget` 仍是总数。换真源后旧候选还占着序号，要新图就抬 budget。
- 记录：每次沉淀追加一行到 `output/promotions.jsonl`；草图卡状态改为 `kept` 并记一条 accept 反馈。
