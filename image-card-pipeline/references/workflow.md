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
$PL inspire add <找来的图…> --note "<来源/想法>"     # 灵感区，给 8 位 id
$PL sketch <名> --idea "<灵感一段，英文>" --ref <id> --promote character:<名> --budget 2 --approved "<谁/何时>"   # --promote：来定哪条真源（style 要带 @画风 / @氛围）
$PL sheet s-<名>                                    # 人挑
$PL promote <候选> --as character:<名> --approved "<用户原话+日期>"
```

- `--as` 四种落点：`character:` / `prop:` / `style:` 写进 `sources.yaml` 对应表；`template:` 写进 `prompt-zone/<路径>.md`（正文取草图卡的 `--idea`，或 `--text`）。
- 必须 `--approved`：定真源是人的决定，AI 不能替。
- 写 `sources.yaml` 前后自动存版本（不再留 `.bak`）；旧 `source` 进 `history`，回退就是把它抄回来，或用 `版本 取 / 回退`。装了 ruamel.yaml 时保留注释，没装会提示注释丢失。
- 卡片指纹包含参考图内容：真源一换，用到它的卡的旧提示词即判过期，`generate` 拒跑，要求重新 `compose`。
- 注意：`--budget` 仍是总数。换真源后旧候选还占着序号，要新图就抬 budget。
- 记录：每次沉淀追加一行到 `output/promotions.jsonl`；草图卡状态改为 `kept` 并记一条 accept 反馈。
