# card-core · 卡片流水线的底座

生图（image-card-pipeline）和视频（video-card-pipeline）共用的"怎么做"：读写 json、存版本、任务状态机、
审片协议、素材库登记。这里是**唯一源头**；每个接入的 Skill 在 `scripts/core/` 放一份副本，
所以 Skill 拷给别人也能单独跑。两个 Skill 之间仍然互不 import，打通只靠生态契约
（`video-card-pipeline/references/ecosystem-contract.md`）。

## 怎么改

下面的命令在放 card-core 和各 Skill 的那一层目录里跑。

```bash
# 1. 只改这里的 core/*.py（副本头上写着"不要改"）
# 2. 同步到所有接入的 Skill
python3 card-core/sync.py
# 3. 跑各 Skill 的自检
python3 video-card-pipeline/scripts/selfcheck.py
# 随时查副本有没有走样（视频自检第 15 项也会查）
python3 card-core/sync.py --check
```

- 1.0.0：首版。

改了对外行为（审片协议、任务状态、素材库条目字段）要同时改 `VERSION`（`core/__init__.py`）和生态契约。


## 导出给别人用的包

```bash
python3 card-core/export.py <输出目录> [--zip]      # 出 card-pipeline-kit-v1.0/（两个 Skill + card-core + kit/ 里的说明、LICENSE、requirements.txt、.gitignore、演示、版本工具）
```

和这里用的是同一份代码，不分叉。导出时去掉：`profiles/`（本机接入档案）、`roles.md`（本机 AI 分工）、
`changelog.md`（个人决定记录，换成一份 v1.0 的）、`.git`、`kit/`、导出和发布脚本本身；内部版本号（契约 v1.x、card-core 1.x）统一改成 v1.0。
最后扫一遍有没有本机专属的名字（路径、人名、端口、AI 工具名），有就报错不出包。
**本机专属的东西只往 `profiles/` 和 `roles.md` 里放**，导出就自动干净。

## 模块

| 模块 | 内容 |
|---|---|
| `jsonio.py` | `read_json` / 原子 `write_json` / 追加 `append_jsonl` / `now` |
| `versioning.py` | 项目文件夹是自己的 git 仓库时存版本（写命令前后各一次）；躺在别的仓库里不存；`init` 建仓库和 .gitignore |
| `jobs.py` | 任务状态机：`next_step`（skip / wait / stop / submit）、付费闸门 `approve`、`forget`（必须写原因） |
| `station.py` | 审片协议：good / bad / discard + liked / disliked，整条或逐段；写入门禁 token + 人的署名 + revision；`aggregate` 汇总成 pass / fail / borderline |
| `sources.py` | 真源种类：默认 人物/道具/风格，`sources.kinds` 加新种类或改名；`promote --as` 的取值、卡里字段名、带不带描述（text）都从这里来 |
| `library.py` | 素材库 local（文件夹 + library.json，格式名沿用 `image-card-pipeline/local-library-v1`）/ http（素材库服务）；条目一律草稿 |

## 接入情况（consumers.json）

- video-card-pipeline：全部模块。
- image-card-pipeline：只用 `sources.py`；另带一份生态契约副本。
