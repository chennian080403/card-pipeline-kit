# 供应商接入

三种适配器，全部只用标准库。凭据**只从环境变量读**（`api_key_env`），可再给一个 `api_key_file` 兜底（launchd 场景）。

## `openai_images`

走 OpenAI 图像协议，两种路径按有无参考图自动选：

- 无参考图 → `POST {base}/images/generations`，JSON。
- 有参考图 → `POST {base}/images/edits`：
  - 参考图是**本地文件/ data URL** → `multipart/form-data` 内联直发（`image` 单张、`image[]` 多张）。**素材不出本机**。
  - 参考图是**纯 URL 列表** → JSON，`image` 为 URL 数组。

回包取 `data[0].b64_json`，没有就取 `data[0].url` 再下载。

```yaml
kind: openai_images
base_url: https://api.example.com/v1
api_key_env: IMAGE_API_KEY
timeout: 300
max_inline_refs: 8          # ≤ 这么多张且总体积不过线 → 内联
max_inline_bytes: 12582912
accepts_size: true          # 该线路不吃 size 就设 false（照样发会 400）
```

## `chat_completions`

OpenAI 兼容的 chat 端点，图不在正文里，在 `choices[0].message.images[0].image_url.url`。参考图按 data URL 内联，一次最多 `max_refs` 张。

```yaml
kind: chat_completions
base_url: http://127.0.0.1:8000/v1
api_key_env: ALT_API_KEY
timeout: 240
attempts: 3                 # 上游会概率性回 200 空图/EOF 500，必须重试
max_refs: 4
```

**实测坑（来自既有落地，通用化后保留）**：

1. **图不在 `content` 里**。这类模型 `choices[0].message.content` 常为 `null`，判空要看 `images`。
2. **上游只用流式后端时概率性失败**。同一请求会出现 HTTP 500 `... EOF`，也会出现 HTTP 200 但无图、usage 全 0。**两种都算失败**，所以内置重试，别把一次 200 当成功。
3. **有的线路不吃 `size`**。出图尺寸固定，页面上的尺寸选择对它无效 → `accepts_size: false`。
4. 适配器也兼容"图以 markdown data URI 出现在 `content` 里"的返回形态。

## `mock`

不联网，回一张 1×1 PNG。用来在没有凭据时跑通全链路、验证卡片口径与审核页。

## 凭据解析顺序

1. `api_key_env` 指定的环境变量；
2. `api_key_file` 指定的文件。

`providers` 命令会逐个别名报 `ready / NOT READY`，缺什么说得清清楚楚。**永远不要**把密钥值写进 `project.yaml` 或任何产物。

## 图床（`hosts.py`）

只有"参考图必须以公网 URL 送出"时才需要（例如供应商只吃 URL，且张数/体积超过内联阈值）。

| kind | 行为 |
|---|---|
| `none`（默认） | 禁止发布；调用方要么内联要么明确失败 |
| `cli` | 每文件跑一条命令，**stdout 每行一个公网 URL**——任何图床脚本照这个约定写就能接（例如自己写一个传 Cloudflare R2 / S3 的小脚本） |
| `http` | `POST {"filename":…, "data_base64":…}`，从回包读 `url` / `public_url` |

多文件并发上传，顺序保持；有一张传失败，整批拒跑（不会少带一张图照样出）。

## 提示词长度与 504

实测上游对超长提示词会 **504**，且 504 不是固定超时上限（同一模型 61.5s 失败、105s / 113.8s 成功）。所以：

- `rules.max_prompt_chars.warn / reject` 按项目标定，超线拒提交；
- 5xx / 504 记成 `unknown`，**不是**"没有图"，也**不是**重生成的许可。
- 请求发出后的任何异常（读超时、断线、非 JSON、200 没图、下载结果失败）同样是 `unknown`；只有连接没建立、本地参数错、4xx 拒收才是 `failed`。`chat_completions` 的 `attempts` 只重试「连不上」，别的一律不重试。人核对后用 `forget` 清掉 `unknown`（见 workflow.md）。
