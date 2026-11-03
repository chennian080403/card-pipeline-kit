# 架构

## 四层

```
cards/*.yaml        卡片层    一张卡 = 一个出图单元；只写"这一张要什么"，不写"怎么实现"
blocks/ + prompt-zone/  口径层  可复用的画面口径与提示词模板（真源·字；英文正文段才进提示词）
sources.yaml        真源层    角色/道具参考图登记表：换真源只改一行，所有卡跟着走
scripts/            执行层    providers / hosts / library / pipeline / review_web
```

**为什么分层**：卡片极易过期。把"长相"从卡里挪进真源层（`sources.yaml`），把"口径"挪进口径层（`blocks/`、`prompt-zone/`），卡就只剩"这一镜独有的东西"（谁在哪、做什么、什么表情、额外禁令）。改一次真源，所有卡一起更新，不用逐卡改。

## 数据流

```
card ──► resolve_refs ──► 参考图列表（本地路径 / URL）
   │                          │
   │                          └─► 张数与体积在阈值内 → 内联（multipart / data URL）
   │                              否则 → hosts 上传拿公网 URL（只有此时参考图才离开本机）
   └──► compose_prompt ──► prompt 文本
                             │
                             ├─► 落盘 output/prompt/<卡>.txt
                             └─► 记指纹 output/prompt/<卡>.meta.json（卡 + 全部 blocks 的 hash）
                                    │
generate ──► 指纹一致才用落盘文本，否则要求重新 compose
         └─► providers.generate ──► 原始字节
                                    │
                                    ├─► output/runs/<卡>-<模型>-<n>/  request.json + result.* + run.json
                                    ├─► output/candidates/<卡>_<模型>_<n>.<ext>   ← 候选只有一份
                                    └─► output/manifest.json  jobs[<卡>|<模型>|<n>] = ok|unknown|failed|running
```

## 目录布局（运行后）

```
output/
  prompt/<卡>.txt              拼好的提示词
  prompt/<卡>.meta.json        指纹 + 变量 + 字数
  candidates/<卡>_<模型>_<n>.* 候选（唯一一份；不另存副本）
  runs/<卡>-<模型>-<n>/        request.json / result.* / run.json / error.txt
  sheets/<卡>_preview.jpg      候选 + 参考图条拼图
  review/<卡>.json             每候选结论 + pick（「要」了的图；视频 image:<卡> 读它）
  manifest.json                四态任务记录 + 批准记录
```

## 供应商路由

模型别名 → 供应商条目 → 适配器。别名在 `models:`，条目在 `providers.entries:`，两者解耦：

```
models:
  main-model: {provider: main, model: some-image-model}
  alt-model:  {provider: alt,  model: vendor-image-model}
```

同一别名可加 `size/quality/timeout/max_inline_refs` 等覆盖项，覆盖项优先于条目级默认值。三种适配器见 `providers.md`。

## 素材库（可选）

`library.kind: none` 时整条登记链路关闭，「要」只记结论和 pick。接上后「要」（`keep`，或分步的 `register`）把图作为**草稿**送过去，`production_approved` 恒 `false`——定稿永远由人在库里点。契约见 `library.py` 顶部注释。

## 审核页（review_web）

人出现的地方。本机 HTTP，只连 127.0.0.1，不生图、不花钱。每个写操作都走和命令行同一套 `cmd_*`，所以规矩一样生效；操作前后各自动存一次版本。