# 卡片字段

一张卡 = 一个出图单元。`templates/card.template.yaml` 只放常用字段，其余字段看本文件。

存盘规则（`save_card`）：空字段和等于默认值的字段（`prompt_mode: lean`、`candidates: {default: 1}`）不写；
人写的内容在上，流水线记录（`status / hold / created / slug / feedback / sources`）放在分隔线下面。
指纹同样忽略这些记录字段、`pack`、空字段和默认值。

## 身份与状态

| 字段 | 说明 |
|---|---|
| `id` | 卡 id。文件名以 `id` 开头最省事（`load_card` 也接受文件名 `_` 前的那段） |
| `title` | 一句话标题 |
| `slug` | 文件/批次用的英文 slug |
| `group` | 同组卡共用批次；空则各自一组 |
| `pack` | 资产包路径 `大包/小包/子包`（如 `人物/主角/氛围测试`）；空则按角色/道具/group 推断。只管归类，不进指纹 |
| `promote` | 这张卡出的图要定成哪条真源：`character:<名>` / `prop:<名>` / `style:<名> @画风` 或 `@氛围` / `template:<名>`（项目自加的种类同理）。审核页按 D 进定真源模式直接停在这一项、名字填好。草图卡建时就写（`sketch --promote`）；不写由脚本推：长相按包 / 角色 / 造型，画风取 `styles` 里登记成画风的那条、氛围取其它 style（没标 look 的算氛围），都没有取标题里不是角色名的那段，构图取标题最后一段。写错 `validate` 报错。不进指纹 |
| `status` | `draft → confirmed → composed → generated → reviewed → registered → kept\|rejected`，由流水线推进 |
| `hold` | **非空即锁**：`validate` 报错，`compose`/`generate` 拒跑。口径过期时写原因（如"旧口径，第四幕已另路定稿"） |
| `created` | 建卡日期 |

## 出图参数

| 字段 | 说明 |
|---|---|
| `model` | 模型别名（config 的 `models:` 键）；空则用 `models.default` |
| `aspect` | `WxH`，如 `1536x1024`。供应商可能只支持固定档，见 `providers.md` |
| `background` | `true` 带背景场景图；`false` 素材/立绘类，背景口径交给 `blocks` |
| `transparent` | `true` 才拼"全透明底"并要求供应商出 alpha（仅部分供应商支持真透明） |
| `prompt_mode` | `lean`（默认）长相交给参考图；`long` 额外拼 `characters[].identity_note` |
| `candidates.default` | 本卡默认张数（`generate --budget` 覆盖） |
| `prompt_templates` | 从 `prompt-zone` 勾的模板 id，如 `["example/01_style#短句"]` |

## 人物与道具

```yaml
characters:
  - name: hero            # 显示名
    lookup: hero          # 到 sources.yaml 的 characters 里找哪条；空则用 name
    source: ''            # 或直接给路径/URL，覆盖登记表
    position: left        # left / right / center …
    expression: calm
    identity_note: ''     # 仅 prompt_mode: long 时拼进提示词
props: [hero/sword]       # 对应 sources.yaml 的 props 键
styles: [水墨红]           # 对应 sources.yaml 的 styles 键（画风 / 氛围真源）：样张作参考图送出；条目里有 prompt（描述）就一并拼进提示词
scenes: [城门]             # 项目在 sources.kinds 里自加的种类，写法同 props；text: true 的种类同 styles
```

`lean` 模式下，`sources.yaml` 条目若带 `face`（脸部放大图），会与主图一起送去当参考——身份靠图锁定，提示词不写长相。

## 画面内容

| 字段 | 说明 |
|---|---|
| `composition` | `blocks/compositions.yaml` 的 key，或直接写英文原句 |
| `environment` | `blocks/environments.yaml` 的 key，或英文原句 |
| `action` | 本镜发生什么：谁在哪、做什么、视线、情绪（英文一段） |
| `hard_rules` | 本卡额外硬规则（英文句），如 `"This frame is SILENT - no text."` |
| `extra_prompt` | 追加在提示词最后的自由文本；`compose` 判空时它也顶用 |

## 卡型（`kind`）

| `kind` | 用途 | 必填 | 建卡 |
|---|---|---|---|
| 空 / `card` | 场景卡：按构图、环境、动作出新图 | `action` 或 `extra_prompt` | `new-card <id>` |
| `sketch` | 短效草图卡，由 `sketch` 命令生成 | —— | `sketch <名>` |
| `rerender` | **重绘卡**：拿一张已有的图只改一处 | `keep`、`change`、一张要改的图 | `new-card <id> --kind rerender` |
| `portrait` | **人物设定卡**：单人全身设定图，常用来出真源 | 恰好一个 `characters` | `new-card <id> --kind portrait` |

### 重绘卡

```yaml
kind: rerender
base: 01-01_main_1        # 要改的图：路径，或候选名；固定当 Image 1。空则用第一张参考图（如角色真源）
keep: her face, costume, the camera and framing, the moonlit background and the painting style
change: the facial expression only -- from a wide smile to a calm, gentle look
characters: [{name: 主角, expression: calm and gentle}]   # 可选；写了 expression 就只加一句"只改表情"
```

- 不拼 `blocks/style.txt`（画风以底图为准），不写 `Subject:` 行（身份就是 Image 1）。`tail`、`hard_rules`、`extra_prompt` 照常。
- 措辞默认见 `pipeline.py` 的 `DEFAULT_RERENDER`；项目要换说法，在 `blocks/rerender.txt` 写正文，用 `{{keep}}`、`{{change}}` 占位。
- 找不到要改的图，`validate` / `compose` 直接报 PROBLEM。
- 原则：**改卡重生，不在旧图上反复修**仍成立；重绘卡是"同一张图只动一处"的场景（换表情、换配角、换颜色），不是修图工具。

### 人物设定卡

```yaml
kind: portrait
characters: [{name: 主角, expression: calm}]
view: front three-quarter view, face fully visible
pose: standing, one hand resting on the sword hilt
costume: a red layered swordswoman robe with a black leather belt
transparent: true       # 默认就是 true；要带背景写 false
```

- 拼一段全身设定图构图（`{{view}}`、`{{pose}}`），可用 `blocks/portrait.txt` 覆盖；`costume` 拼成 `Costume:` 行。
- 满意后 `promote <候选> --as character:主角/红衣` 沉淀成真源。

## 参考图

```yaml
refs:
  roles: []       # 每张参考图的角色说明，按顺序，如 ["identity of A", "the sword"]
  files: []       # 直接给的路径（相对项目根或绝对）
  confirmed: []   # 人工勾定的构图/环境参考
  urls: []        # 外部 URL
```

`refs.roles` 按最终顺序写；缺省时一律写 `reference`。参考图顺序 = 重绘卡的 `base` 最前，然后真源（角色主图、face、道具），`files`/`confirmed`/`urls` 在后，重复内容按 sha256 去重。

## 反馈与来源

```yaml
checklist: []   # 自审清单追加项；通用项自动派生（见 acceptance.md）
feedback: []    # feedback 子命令追加：{at, by, verdict, reason, root_cause}
sources: []     # 口径来源：文档章节 / 用户原话日期
```

## 提示词是怎么拼出来的（顺序）

（重绘卡、人物设定卡的差异见上面「卡型」）

1. `blocks/style.txt`
2. 卡里 `prompt_templates` 指定的模板正文（按 `顺序` 排序）
3. `Aspect ratio: <aspect>`
4. `Input images:` 每张参考图的角色
5. `Environment:` → `Composition:`
6. 每个角色一行 `Subject:`
7. `Action:`
8. `transparent: true` 时的透明底句
9. `hard_rules` 逐条
10. `blocks/tail.txt`
11. `extra_prompt`

模板正文与 blocks 的 `.txt` 都只保留**英文正文段**（以字母/引号/括号/花括号开头的段落），中文说明与表格不进提示词；`.txt` 里以 `#` 开头的整行当注释丢掉。
