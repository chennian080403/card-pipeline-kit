# 卡片生图 / 生视频 Skill 骨架 v1.0

两个给 AI 用的 Skill，把「出图、出片」做成一条有规矩的流水线：

- **生图**（`image-card-pipeline/`）：一张卡出一张图。写卡 → 拼提示词 → 出图 → AI 自审并提议 → 你在审核页点「要 / 不要」→ 进素材库。
- **视频**（`video-card-pipeline/`）：一张卡出一个镜头。写卡 → 出片 → 抽帧自审 → 你在审片页看片给结论 → 进素材库 → 按剪辑单拼成片。

这是一副**骨架**：出图出片的供应商、真源种类、分类、画面口径、AI 分工，都留成了空位让你按自己的项目填。
不绑定任何平台，密钥只走环境变量。花钱前必须你批准，定稿只能你点，这两条脚本卡死，AI 越不过去。

## 三种用法，自己选

| 你要 | 放哪些文件夹 | 看哪份说明 |
|---|---|---|
| 只出图 | `image-card-pipeline/` | `image-card-pipeline/references/quickstart.md` |
| 只出视频 | `video-card-pipeline/` | `video-card-pipeline/references/quickstart.md` |
| 图做成视频 | 两个都放，**并排放在同一个目录** | 先看生图那份，再看视频那份 |

放到你 AI 工具的 Skill 目录（例如 Claude Code 是 `~/.claude/skills/`）。每个 Skill 都自带要用的共用代码，单独放也能用。

## 五分钟看效果

不用配任何东西，不联网，不花钱：

```bash
pip3 install -r requirements.txt    # 视频还要 ffmpeg：macOS 用 brew install ffmpeg
bash 一键演示.sh                     # 在 ./演示项目 里从建卡跑到审核页，最后告诉你打开哪个网页、怎么出真图
```

## 包里还有

| 文件 | 干什么 |
|---|---|
| `card-core/` | 两个 Skill 共用代码的正本。只有你要改共用代码时才用得上，平时不用管（各 Skill 里的 `scripts/core/` 是它的副本） |
| `工具/版本` | 可选：git 的中文壳（macOS / Linux）。放进 PATH 后，在项目里用 `版本 历史`、`版本 取`、`版本 回退` 找回改坏的东西 |
| `一键演示.sh` | 上面那个演示：拿姜澜（华服）当人物真源，建一张「姜澜站立释放法术」的卡；不配密钥出模拟图，接上平台就出真图 |
| `演示素材/姜澜_华服.jpg` | 演示用的人物参考图 |

## 装好以后

1. 跑各 Skill 的自检：`python3 <Skill 文件夹>/scripts/selfcheck.py`，没有失败就是装好了。
2. 照 quickstart 建你自己的项目，先用模拟出图跑通，再接真供应商。
3. 把 `roles.example.md` 复制成 `roles.md`，填你用的 AI 工具。只用一个 AI 可以不填。

## 许可证

代码和文档：MIT，见 `LICENSE`。

`演示素材/` 里的姜澜形象不在 MIT 范围内，只供跑演示、学习这套流程使用，不要拿去商用或当作自己的角色发布。
