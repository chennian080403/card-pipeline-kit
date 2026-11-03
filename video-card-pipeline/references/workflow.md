# 一轮怎么跑

```
开头（人）   写/确认视频卡 → validate → 付费入口给 --approved
AI 自主      generate → 抽帧 sheet → AI 自审 review → 自动送审片台
审片（人）   审片台里看片：整条或逐段 满意 / 不满意 / 放弃，满意和不满意的地方分开写 + 备注 + 时间点标注
AI 自主      sync 取回结论 → 通过的自动 pick → register 进素材库 → assemble 拼成片
定稿（人）   素材库里点定稿
```

否决时一句原因就够：`feedback <卡> reject --reason … --root-cause …`

| root-cause | 含义 | 下一步 |
|---|---|---|
| `card` | 动作、镜头、时长写得不对 | 改卡，再 generate 出新条 |
| `model` | 模型能力问题（人脸漂、自己走位） | 换模型，或抬高 `--budget` 再出 |
| `frame` | 关键帧本身有问题 | 来自 `image:` 的关键帧自动回写到各自的生图卡；去生图那边改 |
| `ref` | 真源或参考图不对 | 改 `sources.yaml` 或卡的 refs |

## 接入一个新项目

1. 只用视频：新目录里跑 `vpipeline.py --config project.yaml init`。和生图一起用：在生图项目的 `project.yaml` 上跑 `init`。
2. 改 `video.providers / models`：接你的入口。密钥放进环境变量。
3. 审片台：先用 `builtin`（`review_web.py`）。已有自己的放映厅的，写一个插件，提供 `make_station(cfg, spec)` → `submit / fetch / describe`，`fetch` 返回 `core.station.aggregate(...)`，照 `templates/station_plugin.example.py` 改（样板本身就能用：片子进一个文件夹，人填旁边的 `review.json`）。
4. 看板（可选，默认不用）：本 Skill 留了看板插件的口子（`video.board`），不配就完全不加载。
5. 后期（可选）：在 `hooks.post` 里挂命令（超分、补帧、降噪……），用 `{in} {out}` 占位符。
6. 想要版本记录：项目文件夹 `git init`（新建项目时 `init` 会自动做）。
7. `doctor` 全绿后，先用 `dry`（mock）跑一遍，再换真入口。改了 Skill 代码先跑 `scripts/selfcheck.py`。
