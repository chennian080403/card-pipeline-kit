---
name: "image-card-pipeline"
description: "卡片驱动的通用生图流水线：一张卡（人物/道具/构图/动作/硬规则）驱动 找参考 → 拼提示词 → 生图 → 自审并提议哪几张该要 → 人在审核页点「要」登记素材库 → 反馈归因 全流程，外加一个本机审核与素材库网页。任何项目都靠一份 project.yaml 接入（供应商、模型、提示词模板、真源登记表、图床、素材库全可配），密钥只走环境变量。当用户要做'卡片生图/镜头卡出图/批量生图登记审核/把生图流程接到新项目'时使用。"
---

# image-card-pipeline · 卡片驱动的通用生图流水线

**一张卡 = 一张图**。卡里写清人物、道具、构图、环境、动作、硬规则；流水线据此找参考、拼提示词、生成、自审并提议哪几张该要。人在开头批预算，在审核页点「要 / 不要 / 定为真源」，最后在素材库定稿。

不绑定任何项目：项目靠一份 `project.yaml` 接入（卡片、提示词模板目录 `paths.prompt_zone`（真源·字）、真源表、供应商、图床、素材库全可配），密钥只从环境变量读。从零接入照 `references/quickstart.md` 做。改动记录在 `references/changelog.md`，本文只写现行规矩。
`profiles/` 放本机专属的接入档案，导出给别人时不带；有的话先读里面的说明。

```bash
SKILL=<本 Skill 所在目录>          # 例：~/.claude/skills/image-card-pipeline
PL="python3 $SKILL/scripts/pipeline.py --config ./project.yaml"
```

---

# 一、底座：谁来做、守什么

## 分工

生图的活**主要交给主模型**（和你对话的那个 AI）。下面只按角色写；每个角色具体是哪个工具、怎么调用，写在本 Skill 目录的 `roles.md`（照 `roles.example.md` 填）。**没有 `roles.md` 时，几个 AI 角色都由主模型自己做**，规矩不变。

| 角色 | 是谁 | 接什么活 |
|---|---|---|
| 人 | 用户 | 批预算、否决原因、沉淀真源、素材库定稿 |
| 脚本 | 本 Skill 的命令 | 能用规则判的都归它：尺寸、透明、参考图、字数、过期、存版本 |
| 主模型 | 主会话；要判断的子任务可派**判断子代理** | 所有要看图下判断的活，以及写卡、改卡、归因、改关键代码 |
| 文字助手（可选） | 便宜快的模型 | 不看图的杂活：批量建卡、批量改字段、写测试、改文档、跑命令、查资料 |
| 看图助手（可选） | 能看图的便宜模型 | 要看图的杂活：批量描述图、按清单粗筛、给参考图打标签 |

四条原则：
1. **看图下判断归主模型**：挑参考、看样、自审、提议哪几张该要、归因。看图助手只做看图的杂活，结论不算数。
2. **文字助手和看图助手交回的东西由主模型核对后才算数**。
3. **花钱和定稿只归人**：AI 只照抄人的原话去执行。
4. **能用脚本判的不让模型判**。

| 环节 | 谁做 | 怎么分 |
|---|---|---|
| 找素材 `inspire add` | 人 / 文字助手 | 人给图或说方向；按方向批量搜集、入库交文字助手，去重由脚本做 |
| 给素材打标签、写描述 | 看图助手 | 量大时交看图助手，主模型抽查 |
| 挑参考 | 主模型 → 人 | 主模型看图挑出几张并说理由，人点头 |
| 写灵感段 `--idea` | 主模型 | 要看着参考写英文画面描述 |
| 建卡、填卡 | 主模型；批量交文字助手 | 单张卡、要对照真源的卡由主模型写；镜头表批量转卡、同一字段批量改交文字助手，主模型抽查 |
| `validate` / `compose` / `doctor` | 脚本 | 谁都能敲；主模型读拼好的提示词，看口径对不对 |
| 批预算 | 人 | 回一句"X 出 N 张"，原话写进 `--approved` |
| `generate` / `sketch` 出图 | 脚本 | 平时主模型敲；人批了一批总数后，大批量循环可交文字助手跑，`--approved` 照抄人的原话 |
| 尺寸、透明、参考图、字数 | 脚本 | 自动记进 manifest，不靠模型看 |
| `sheet` 拼图 | 脚本 | 给主模型和人一起看 |
| 看图自审 `review` | 主模型 | 身份、道具、构图、多余物、表情；记 `--by claude`（`claude` 即主模型）。候选很多时可先让看图助手按清单粗筛，结论仍由主模型给。别的角色给结论会被脚本拒绝（`rules.reviewers`） |
| 要 / 不要 | 人在审核页点；主模型可以提议 | 人用的就三个按钮。主模型自审后只提议「建议要 X、Y」，不替人点；人口头说「要 X」时，主模型照办敲 `keep … --by human` |
| 否决原因 | 人 | 一句话就行 |
| 归因 `feedback --root-cause` | 主模型 | 判断该改卡、改真源还是改口径块 |
| 改卡重生 | 主模型；同一改动铺到多张卡交文字助手 | 改法由主模型定 |
| 沉淀 `promote` | 人定，脚本执行 | 定真源是人的决定 |
| `register` 登记草稿 | 脚本 | 恒为草稿 |
| 定稿 | 人 | 只在素材库点。定稿就是定关键帧（关键的参考图）：视频开了只认定稿（`keyframes: final`）时，视频卡的关键帧（首帧、中间几张、尾帧）只能用定稿图，见生态契约 §5 |
| 存版本 | 脚本 | 写命令前后自动存 |
| 改本 Skill 的代码 | 主模型定方案、改关键逻辑（大改可派判断子代理）；文字助手写测试、改文档、批量替换、跑自检 | 文字助手交回的改动由主模型复核 |
| 查供应商文档、模型实测 | 文字助手查，主模型下结论 | |

## 不可越过的规矩（都由脚本卡住）

1. **人不得被 AI 替代**：流水线只到"草稿登记"，`production_approved` 恒为 `false`；定稿只能在素材库由人点。
2. **付费前必须批准**：`generate` / `sketch` 要 `--approved "<谁/何时/原话>"`，按**总张数**封顶（`--budget` 是总数不是增量）。`rules.require_approval: false` 可关。
3. **`ok` 幂等跳过**：同一个 `卡|模型|序号` 已 `ok` 就不再重发。改了卡后旧提示词出的图仍占张数，`generate` 会提示 "made from an OLDER prompt"，要新版就抬 `--budget`。
4. **`unknown` 不许重生成**：请求发出后出的任何岔子（5xx/504、超时、断线、非 JSON、没图、下载失败、进程中途没了）都记 `unknown`（可能已出图并已计费），回包原文在 run 目录 `response.json`。人核对后 `forget <卡> <序号> --why "…"` 才会重发。
5. **提示词不能过期**：`compose` 记卡 + 口径块 + 参考图的指纹；任一改动后 `generate` 拒跑，要求重新 `compose`（或 `--refresh`）。`validate` 有 PROBLEM 时 `compose` 拒绝，`--force` 才拼。
6. **提示词过长会 504**：`rules.max_prompt_chars` 定 warn/reject，超线拒发。
7. **参考图优先不外发，也不悄悄丢**：张数与体积在阈值内就内联（超体积先自动缩小），非必要不上传图床；发不全（超 `max_refs`、没图床、图床传失败）就整批拒跑。`validate` 有 PROBLEM（含引用没登记的真源）时 `generate` 拒跑。
8. **一个事实只有一份**：长相在 `sources.yaml`，口径在 `blocks/` + `prompt-zone/`，卡里不抄长相。
9. **看图结论只认主模型和人**：`review --by` 只收 `rules.reviewers`（默认 `[claude, human]`；`claude` 指主模型，判断子代理也记 `claude`），审核页记为 `human`。
10. **版本交给 git，不留 .bak**：项目文件夹是 git 仓库（`init` 自动建），每个写命令（命令行和审核页）动手前存一次、做完存一次；`doctor` 报 `version :` 一行。找回用 `版本 历史 / 差异 / 取 / 回退`。`rules.autosave: false` 可关。

## 自检

改了本 Skill 的代码，必须先跑通再交：

```bash
python3 $SKILL/scripts/selfcheck.py        # 临时目录 + mock，从 init 跑到审核页，不花钱
```

项目自己的健康用 `$PL doctor`：路径、模板数、逐卡可用数（`usable`）、版本、凭据。

---

# 二、工程化：流程、结构、分类

## 流程：短效定调 → 长效复用

| | 短效（一次性） | 长效（长周期） |
|---|---|---|
| 干什么 | 找素材 → 人挑 → 说灵感 → 试几张 → **定下**真源/风格/说法 | 拿定下的东西反复出图，始终一致 |
| 命令 | `inspire` → `sketch` → `promote` | `new-card` → `validate` → `compose` → `generate` → `sheet` → `review`（主模型自审）→ 人点「要」（= `keep`） |
| 落点 | `inspiration/`、`s-*` 草图卡 | `sources.yaml`、`prompt-zone/`、素材库 |

`promote` 是两段之间的桥：人认可的候选写进真源表（旧值进 `history`）或沉淀成提示词模板；引用它的长效卡因指纹变化被判过期，重新 `compose` 后跟着走。

```bash
# 短效
$PL inspire add ~/Downloads/a.png --note "老板发的参考" --tag 人物
$PL sketch 女主 --idea "A young swordswoman in red, calm gaze" --ref <灵感id> --budget 2 --approved "用户 …"
$PL sheet s-女主
$PL promote s-女主_main_1 --as character:女主 --approved "用户：就这张" [--face face.png]
#   --as style:水墨红 / prop:剑 / template:沉淀/女主气质

# 长效
$PL new-card 01-01 "城外初见"            # --kind rerender | portrait
$PL validate 01-01
$PL compose  01-01 --var 介质=Illustration
$PL generate 01-01 --budget 2 --approved "用户 10-23：01-01 出 2 张"
$PL sheet    01-01
$PL review   01-01 01-01_main_1 pass --note "体态对"      # 主模型自审，默认 --by claude
#   人在审核页点「要」= review pass + pick + register 三步合一；命令行等价于：
$PL keep     01-01 01-01_main_1 --by human
#   逐步敲也行：review … pass → pick 01-01 01-01_main_1 → register 01-01
```

卡片状态由流水线推进：`draft → confirmed → composed → generated → reviewed → registered → kept | rejected`。人在页面上只给两种结论：「要」（pass，进素材区）和「不要」（fail，进废稿区，原因可不写；写了就进 `feedback` 作改卡依据）。命令行的 `review` 还收 `borderline`，只给主模型自审时标「拿不准、请人看」，页面上它仍留在出图区（标「旧·待定」）。口径过期就在卡里写 `hold: "<原因>"`，删掉才能继续。

## 结构：四层

```
卡片层    cards/*.yaml            一张卡 = 一个出图单元（字段见 references/card-schema.md）
口径层    blocks/ + prompt-zone/  style.txt / tail.txt / 构图块 / 环境块 + 可勾选的提示词模板
真源层    sources.yaml            角色/道具/风格（种类可在 project.yaml 的 sources.kinds 加）的参考图登记表：换真源只改一行，所有卡跟着走
执行层    scripts/                pipeline(CLI) · review_web(审核页) · providers(供应商) · hosts(图床) · library(素材库)
```

数据流与 `output/` 布局见 `references/architecture.md`。

## 分类：卡型 + 资产包

**卡型**（`kind`）：默认场景卡；`rerender` 重绘卡（拿一张图只改一处：`base` + `keep` + `change`）；`portrait` 人物设定卡（单人全身、默认透明底，常用来出真源）；`sketch` 草图卡（短效，`sketch` 命令生成）。见 card-schema.md。

**资产包**：所有区共用一棵树 `大包/小包/子包`，图只落在叶子包。只归类、不搬文件。

- 大包默认 `人物 / 道具 / 场景 / 风格 / 测试`，项目可在 `packs.categories` 换。
- 卡写 `pack: 人物/主角/氛围测试`；不写则推断：有角色 → `人物/<角色>/<group>`，有道具 → `道具/<道具>/<group>`，否则 `测试/<group>`。`pack` 不进指纹。
- **真源一个名字只认一张**。同一人物的不同造型各算一个真源：`主角/婚服`、`主角/便装`，卡里写 `characters: [{name: 主角, lookup: 主角/婚服}]`；道具同理 `主角/剑`。真源落在 `人物/<名>/长相`、`道具/<名>/长相`、`风格/<名字前缀>`，条目写 `pack:` 可覆盖。
- **真源种类在配置里加**：`project.yaml` 写 `sources: {kinds: {scenes: {label: 场景, as: scene}}}`，就多一节「场景」真源：卡里 `scenes: [城门]`，`promote --as scene:城门`，审核页「长相」下拉多出「场景」；加 `text: true` 的种类带描述（像画风），页面多一个按钮。默认三种不能删；从配置里拿掉的种类，已登记的条目照样显示。
- 灵感落 `风格/<第一个标签>`，`--pack` 可指定。不通过的候选显示在废稿区的同一路径。

## 审核页（人出现的地方）

```bash
python3 $SKILL/scripts/review_web.py --config ./project.yaml --port 8860
```

只连 127.0.0.1，不生图不花钱。每个写操作都调一个命令行子命令（`keep` / `review` / `pack` / `promote` / `unregister` …），页面能做的命令都能做，只有素材库「定稿」故意只给人；自检第 23 项卡住这一条。**人用的就页面上这几个按钮；`review` / `pick` / `register` 这些分步命令是给主模型和脚本用的。**两个入口、同一个库，切换时停在同一路径：

- **短效 · 定调**：参考区、试样区（定真源）。
- **长效 · 复用**：出图区（审成品）→ 素材区 → 真源区 → 废稿区。

每张图只有三个按钮（要简练、谁都看得懂）：

| 按钮 | 意思 | 结果 |
|---|---|---|
| **要** | 能直接用进片子 | 通过 + 进素材区（草稿，人点「定稿」才算定稿） |
| **不要** | 不行 | 进废稿区，原因可不写；点开再点「要」能捞回 |
| **定为真源 ▸** | 有值得以后反复用的东西，再选喜欢它的什么 | 长相 → 存图（人物 / 道具，及 `sources.kinds` 加的存图种类）；画风、氛围（及加的带描述种类）→ 存图 + 描述；构图 → 存字（模板） |

图进了素材区以后，详情里换成这几个按钮：

| 区 | 按钮 | 结果 |
|---|---|---|
| 素材区 | **定稿**（键 1） | 草稿 → 定稿，这张算正式能用；只有人能点（规矩 1），AI 和命令行都没有这一步。视频卡的关键帧（`first_frame` / `keyframes` / `last_frame`，写 `image:<卡>`、`library:<id>` 或直接给路径）在视频开了只认定稿时，必须是这里定稿的图。正被视频卡当关键帧的素材标「关键帧 · N」，详情里列出是哪几张视频卡；移回、丢弃、撤回定稿前会提醒（生图只读视频卡：同一份 project.yaml 带 `video:` 段，或写 `link.video_config`） |
| 素材区 | **待定** | 定稿撤回成待定，图还在素材区 |
| 素材区 | **移回原处**（键 2）/ **丢弃**（键 3） | 回出图区 / 试样区（未审）/ 进废稿区；已定稿的先确认 |
| 真源区 | **移回素材区**（键 2）/ **丢进废稿区**（键 3） | 撤掉这次定真源（有旧版换回旧版，没有就删条目），原图回素材区 / 进废稿区 |

拿不准就不点，图留在出图区。真源区分三个页签：**图**（长相、画风、氛围的样张，画风 / 氛围带描述，没写的标「待补描述」，点开详情能补写 / 改写）、**字**（构图等提示词模板）、**记录**（每次定真源的账）。每张图实际发出的提示词跟着图永久保存（`output/runs/…/request.json`），不另设提示词区。以前点过的通过 / 待定显示为「旧·…」，不自动搬。

键盘：W 切短效 / 长效，E 在这条线里切下一个区（长效 出图 → 素材 → 真源 → 废稿，⇧E 倒着）；详情和大图里不切。每个区各记停留位置，切回来回到原处，刷新清空。页面分左栏、右边两块，亮框在哪块方向键就归哪块（鼠标移过去、Tab、到头再按一下都能换块，按住不放不跨块）。右边页签 / 筛选小框（全部 / 未审 / 旧结论，真源区 图 / 字 / 记录）用 S 挨个切（⇧S 倒着），不进方向键；文件夹、图连成一串，↑↓←→ 选，回车 = 进文件夹 / 开详情（真源区「字」「记录」一条条竖排，↑↓ 走，回车 = 展开全文 / 看这条的图）；详情里回车看大图，←→ 切图、1 / 2 / 3 在各区意思一致——1 往前一步、2 退回上一步、3 丢进废稿区（出图 / 试样：1 要 · 3 不要；素材：1 定稿 · 2 回出图区 / 试样区 · 3 丢弃；真源：2 回素材区 · 3 撤掉真源、原图进废稿区；废稿：1 要 · 2 回出图区 / 试样区，都变回未审），记完跳下一张、空格 跳过、A 写备注（一张图只有一条备注：出图区 / 试样区 / 废稿区叫备注，点「不要」时就是原因；素材区叫「备注 / 丢弃原因」，按 3 丢弃时记进卡里；都是同一条，记在候选图上。A 只管进框，框里 Esc / 回车 收起并保存；停手 1.5 秒自动存只落文件，收起、切图、关详情、切走标签页时才存一版进版本历史（写一次备注一条提交，翻版本途中也不提交），不弹浏览器输入记录。防手滑不拦人：一次编辑（进框到收起）留一版旧的进 `note_history`（最近 10 版），不在输入框里时 ⌘Z 在现在这版和上一版之间来回换（「↶ 上一版」按钮同 ⌘Z）；兜底是后台每张图留最近 10 版 `note_history`（同一段字只留一份，空备注不留），再早的用 `版本` 找。删掉一大半时旁边亮提醒；结论按钮不会冲掉备注。真源描述删掉一大半也要确认，「↶ 填回改之前的」从记录里取）。出图区 / 试样区 / 废稿区的详情里按 D 进定真源模式（那块亮框）：← → 或 S 切喜欢什么（长相 / 画风 / 氛围 / 构图；模式里 ← → 不换图），A 进描述框（长相没有描述，进名字框，Tab 去别的框），回车弹确认框、再回车定下；模式里 1 / 2 / 3 暂停，D / Esc / ⌫ 退出，换图自动退出。定真源那块的框里 Esc / 回车 只收起，不关详情。Esc 和 ⌫ 都是退一步（关大图 → 退出定真源模式 → 关详情 → 回上一层）。Q 平铺 / 收起。进了空包，框自动交给左栏。名字撞上已有真源会先显示要被换掉的那张，确认后才替换；素材区已定稿的图「移回原处」要再确认。废稿只有一处，图上标「草图 / 成品」。素材库可用 `library.kind: local`（文件夹 + `library.json`）。

---

## 命令表

| 命令 | 作用 |
|---|---|
| `init` | 脚手架出新项目（含 `.gitignore` 和 git） |
| `new-card <id> [标题] [--kind rerender\|portrait]` | 从模板建卡 |
| `cards` / `show <卡>` | 列卡 / 打印一张卡 |
| `validate <卡>` | 字段、真源、hold、未填变量；缺字段、引用没登记的真源、找不到参考图都报 PROBLEM（`generate` 前会自动再查一遍，有 PROBLEM 拒跑） |
| `compose <卡> [--var N=V]` | 拼提示词落盘 + 记指纹 + 报字数 |
| `generate <卡> --approved … [--budget N] [--model 别名]` | 调供应商、写四态 manifest、落候选 |
| `forget <卡> <序号\|key> [--model 别名] --why …` | 人核对过（run 目录、供应商后台）确认 `unknown` 那张真没出图，清掉它好让下次重发；记录留在 manifest `forgotten` |
| `sheet <卡>` | 候选 + 参考图条拼成预览 |
| `keep <卡> <候选> [--note] [--by claude\|human]` | 「要」：通过 + 记进 pick + 登记素材库**草稿**，一步到位（审核页的「要」就是它） |
| `review <卡> <候选> pass\|fail\|borderline --note … [--by claude\|human]` | 记单张结论（页面「不要」= `fail`；`borderline` 只给主模型自审用） |
| `pick <卡> <候选…> [--remove]` | 记进 / 移出 pick（视频卡 `image:<卡>` 读它）；`keep` 已包含 |
| `register <卡> [候选…]` | pick 里的图（或指定的图）登记为素材库**草稿**；`keep` 已包含 |
| `pack <卡> [大包/小包/子包]` | 改卡的资产包路径，给空回到默认规则；不搬文件（详情页改「资产包」保存就是它） |
| `state [--json]` | 一次看全局：每张卡的候选和结论、真源、模板、素材库、灵感；`--json` 和审核页 `/api/state` 同一份 |
| `unregister <卡> <候选> [--discard --note …]` | 移出素材区：回原区（变回未审），或进废稿区（原因记进卡）；已定稿的拒绝，人先在审核页点「待定」撤回定稿 |
| `note <卡> <候选> --note …` | 只写 / 清一张图的备注，结论不动（审核页备注自动保存就是它） |
| `unreview <卡> <候选>` | 撤回结论：废稿区的图回试样区 / 出图区，变回未审（审核页废稿区按 2） |
| `demote --as <种类>:<名> [--force] [--discard]` | 撤掉一次定真源：有历史换回上一版，没有就删掉这条（有卡在用要 `--force`）；原图回素材区，草图回试样区，`--discard` 则进废稿区；记进 `promotions.jsonl`（`action: demote`） |
| `feedback <卡> accept\|reject\|revise --reason … [--root-cause …]` | 记反馈与归因（同一条连记两次只留一条） |
| `inspire add <文件…> [--note --tag --pack]` / `inspire list` | 灵感区（按内容哈希去重，给 8 位 id） |
| `sketch <名> --idea … [--ref id] [--promote 种类:名] --approved …` | 一句灵感 + 参考直接出候选（生成 `s-<名>` 草图卡）；`--promote` 写明这张草图是来定哪条真源的（`character:女主` / `"style:黄昏 @氛围"` …），审核页定真源直接停在这一项 |
| `promote <候选> --as character:\|prop:\|style:\|<自加种类>:\|template:<名> --approved …` | 沉淀成真源 / 风格 / 提示词模板；种类见 `sources.kinds` |
| `describe --as style:<名> --text "<英文描述>" --approved …` | 只改已登记画风 / 氛围（带描述种类）的描述，样张不动；`--text ""` 清空。每次改都记进 `promotions.jsonl`（谁批、改前、改后，`action: describe`）；审核页真源详情里的「保存描述」就是它 |
| `providers` / `doctor` | 模型与凭据 / 全量自检 |

## 目录

```
image-card-pipeline/
  SKILL.md                       本文件（现行规矩）
  project.example.yaml           项目配置模板（全部可配项与注释）
  roles.example.md               分工填空（本机的 roles.md 导出不带）
  profiles/                      本机专属接入档案（导出不带）
  scripts/
    pipeline.py                  CLI（全部命令 + 版本自动存）
    review_web.py                审核与素材库网页（web/review.html）
    providers.py                 供应商适配：openai_images / chat_completions / mock
    hosts.py                     图床：none / cli / http
    library.py                   素材库登记契约（恒为草稿）
    selfcheck.py                 本 Skill 的自检
  templates/                     卡片模板、blocks 示例、prompt-zone 示例、sources 示例
  references/
    quickstart.md                从零接入你自己的项目（单用生图 / 和视频一起）
    ecosystem-contract.md        和视频 Skill 的共同约定（与视频那份同一份）
    architecture.md              四层结构、数据流、output 布局
    reference-reverse.md         参考图逆向：反推七字段、局部参考、边界与自检
    card-schema.md               卡片字段与卡型
    providers.md                 供应商接入契约与实测坑
    workflow.md                  一轮怎么跑、幂等与重跑、沉淀细节
    acceptance.md                自审与验收口径
    changelog.md                 改动记录（带日期的决定都在这）
```

单独用就够了；也能和视频 Skill（video-card-pipeline）一起用，两边只按契约互通，契约在 `references/ecosystem-contract.md`（两个 Skill 各带一份，内容相同）；改真源、素材库或引用格式前先改契约并升版本。深入细节前先读对应 reference，别一次全读。
