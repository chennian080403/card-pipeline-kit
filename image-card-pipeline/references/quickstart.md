# 从零接入你自己的项目（生图）

照这一页做完，你就有一个自己的生图项目：写卡 → 出图 → 在审核页点「要 / 不要」→ 进素材库。
先用模拟出图（mock，不联网、不花钱）跑通一遍，再接你自己的出图服务。

只用生图，看这一页就够了。还想把图做成视频，装好视频 Skill 后看它的 `references/quickstart.md`。

## 0. 准备

| 要什么 | 干什么用 | 怎么装 |
|---|---|---|
| Python 3.9 以上 | 跑脚本 | 系统自带或 python.org |
| PyYAML | 读配置和卡（必须） | `pip3 install pyyaml` |
| Pillow | 拼预览图 `sheet`（建议装） | `pip3 install pillow` |
| ruamel.yaml | 改真源表时保留你写的注释（可选） | `pip3 install ruamel.yaml` |
| git | 每次改动自动存版本，改坏了能找回（可选） | 系统自带或 git-scm.com |

## 1. 放好 Skill

把整个 `image-card-pipeline/` 文件夹放进你 AI 工具的 Skill 目录，例如 Claude Code 是 `~/.claude/skills/`。
要和视频一起用，就把 `video-card-pipeline/` 放在**同一个目录**里，两个文件夹并排放。

装好先跑自检，确认环境没问题（约半分钟，不联网、不花钱）：

```bash
python3 ~/.claude/skills/image-card-pipeline/scripts/selfcheck.py
```

最后一行是「N 项通过，0 项失败」就行，项数随版本变，只看失败是不是 0（没把 `card-core/` 一起放进来时，「card-core 副本与源头一致」那一项显示「跳过」，也算通过）。

## 2. 建项目

找一个空文件夹当项目，在里面跑：

```bash
SKILL=~/.claude/skills/image-card-pipeline
cd 你的项目文件夹
python3 $SKILL/scripts/pipeline.py --config project.yaml init
```

它会建好这些：

```
project.yaml      项目配置（下一步要填的就是它）
cards/            卡片，一张卡出一张图
blocks/           口径块：style.txt（每条提示词的开头）、tail.txt（结尾）、构图块、环境块
prompt-zone/      可勾选的提示词模板
sources.yaml      真源登记表：人物、道具、画风的参考图
output/           出的图、记录，都在这下面
```

## 3. 填 project.yaml

按顺序填，每一项在 `project.example.yaml` 里都有注释：

1. **供应商**（`providers` + `models`）：你用哪家出图。
   - 先不动，`models.default: dry` 就是模拟出图，后面几步照样能跑。
   - 要接真服务：在 `providers.entries` 里加一条，`kind` 选 `openai_images`（OpenAI 图片接口那一类）或 `chat_completions`（图片放在聊天回复里的那一类）。
   - 在 `models` 里起个别名指向它，再把 `models.default` 改成这个别名。
   - **密钥只写环境变量的名字**（`api_key_env: MY_IMAGE_KEY`），值放进环境变量，绝不写进文件。
   - 各家接口的坑见 `references/providers.md`。
2. **真源种类**（`sources.kinds`，可不填）：默认有 人物 / 道具 / 风格 三种。你的项目还要别的，比如「场景」，就加一行：
   ```yaml
   sources:
     kinds:
       scenes: {label: 场景, as: scene}
   ```
3. **分类**（`packs.categories`，可不填）：审核页左边的大包，默认 人物 / 道具 / 场景 / 风格 / 测试，改成你自己的。
4. **口径块**（`blocks/`）：`style.txt` 写你项目统一的画面口径（介质、光线、渲染方式），`tail.txt` 写每张图都要守的话（比如「不要文字水印」）。都写英文。
5. **素材库**（`library`）：建议填 `kind: local` + `root: 素材库`（`init` 生成的是 `kind: none`，直接手改这两行；别用会整份重写 yaml 的工具改，会把注释丢掉），就是项目里的一个文件夹，不用起服务。审核页点「要」的图会登记进去（草稿），你在素材库点「定稿」才算定稿。
6. **规矩**（`rules`）：`require_approval: true` 时，花钱出图必须带上你批准的原话。模拟出图不花钱，但也要带，好让你提前习惯。

填完跑体检，没有报错就行（还没接真供应商时，`NOT READY (missing env …)` 那几行是正常的，接好、设了环境变量就会消失）：

```bash
python3 $SKILL/scripts/pipeline.py --config project.yaml doctor
```

## 4. 分工（按你自己的 AI 工具填）

`SKILL.md` 里的分工只写角色：主模型、判断子代理、文字助手、看图助手。
把 Skill 里的 `roles.example.md` 复制成 `roles.md`（放在 Skill 文件夹里），填上每个角色是哪个工具、怎么调用。
**只用一个 AI 就不用填**：没有 `roles.md` 时，全部由主模型自己做。

## 5. 跑通一遍（模拟出图）

可以直接让你的 AI 照着做，也可以自己敲：

```bash
PL="python3 $SKILL/scripts/pipeline.py --config project.yaml"
$PL new-card 01-01 城门初见            # 建卡，打开 cards/ 里的新文件，填 action（英文一段：这张图发生什么）
$PL validate 01-01                     # 查卡
$PL compose 01-01                      # 拼提示词，结果在 output/prompt/01-01.txt
$PL generate 01-01 --budget 2 --approved "张三 10-24：01-01 出 2 张"
python3 $SKILL/scripts/review_web.py --config project.yaml --port 8860
```

浏览器打开 `http://127.0.0.1:8860`（端口被占时脚本会报错退出，换一个 `--port`，别打开别人项目的审核页），在「出图区」点一张图的「要」，它就进了素材区和素材库（草稿）。
模拟出图出的是纯色方块，这就对了。接上真供应商以后，同样的命令出的就是真图。

## 6. 接真供应商之后

- 每次出图前，AI 会先问你批多少张。你回一句话，它原样抄进 `--approved`。脚本只拦得住空的、不到 4 个字的、明显占位的（`…`、`<…>`、TODO）；像 `test` 这种能过，所以「照抄原话」最终靠 AI 守规矩、你抽查记录。
- 换真源（比如主角换了造型）只改 `sources.yaml` 一行，引用它的卡会提示「提示词过期」，重新 `compose` 就跟上了。
- 改坏了：项目是 git 仓库时，每次改动都自动存了版本。找回用包里 `工具/版本`（git 的中文壳，macOS / Linux，放进 PATH 后用 `版本 历史`、`版本 取`、`版本 回退`），或者直接用 git。

更多：命令全表在 `SKILL.md`；卡片字段在 `references/card-schema.md`；一轮怎么跑在 `references/workflow.md`。
