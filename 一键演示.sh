#!/usr/bin/env bash
# 一键演示：在一个新文件夹里，用模拟出图 / 模拟出片（不联网、不花钱）从建卡跑到审核页。
#   bash 一键演示.sh [项目文件夹，默认 ./演示项目]
# 包里有哪个 Skill 就演示哪个：只有生图、只有视频、两个都有，都行。
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
IMG="$KIT/image-card-pipeline"; VID="$KIT/video-card-pipeline"
DEMO="${1:-./演示项目}"
[ -e "$DEMO" ] && [ -n "$(ls -A "$DEMO" 2>/dev/null)" ] && { echo "✗ $DEMO 已存在且不是空的，换一个文件夹名"; exit 1; }
mkdir -p "$DEMO"; cd "$DEMO"; DEMO="$(pwd)"
python3 -c "import yaml" 2>/dev/null || { echo "✗ 缺 PyYAML：pip3 install pyyaml"; exit 1; }
have_img=0; have_vid=0
[ -f "$IMG/scripts/pipeline.py" ] && have_img=1
[ -f "$VID/scripts/vpipeline.py" ] && have_vid=1
[ $have_img$have_vid = 00 ] && { echo "✗ 旁边没找到 image-card-pipeline 或 video-card-pipeline"; exit 1; }
pl() { python3 "$IMG/scripts/pipeline.py" --config project.yaml "$@"; }
v()  { python3 "$VID/scripts/vpipeline.py" --config project.yaml "$@"; }
# 改 yaml 里的一个字段：edit <文件> <python 表达式，d 是内容>
edit() { python3 - "$1" "$2" <<'PY'
import sys, yaml
path, code = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(path, encoding="utf-8")) or {}
exec(code)
yaml.safe_dump(d, open(path, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
PY
}

if [ $have_img = 1 ]; then
  echo "▶ 生图：建项目、建卡、模拟出 2 张"
  pl init >/dev/null
  edit project.yaml 'd["library"] = {"kind": "local", "root": "素材库"}'
  pl new-card 01-01 城门初见 >/dev/null
  card="$(ls cards/01-01_*.yaml)"
  edit "$card" 'd.pop("characters", None); d["action"] = "A traveller in a grey cloak stops at an old city gate at dawn, looking up at the banners."'
  pl validate 01-01 | tail -1
  pl compose 01-01 | tail -1
  pl generate 01-01 --budget 2 --approved "演示：01-01 出 2 张" | tail -1
fi

if [ $have_vid = 1 ]; then
  echo "▶ 视频：建项目、建卡、模拟出 1 条"
  v init >/dev/null
  if [ $have_img = 1 ]; then
    # 演示为了一路跑通，替你点一次「要」，视频首帧才有图可用。你自己的项目里「要」只能你在审核页点，AI 只提议
    pl keep 01-01 01-01_dry_1 --by human >/dev/null
    echo "  （演示替你点了一次「要」；你自己的项目里只能你点）"
    first="image:01-01"
  else
    command -v ffmpeg >/dev/null || { echo "✗ 缺 ffmpeg"; exit 1; }
    edit project.yaml 'd["library"] = {"kind": "local", "root": "素材库"}'
    ffmpeg -loglevel error -f lavfi -i "color=c=0x8a9bb0:s=1280x720" -frames:v 1 首帧.png
    first="首帧.png"
  fi
  v new-card 01-01 城门初见 --first-frame "$first" >/dev/null
  edit "$(ls video-cards/01-01_*.yaml)" 'd["motion"] = "Slow push-in toward the gate; the traveller lifts his gaze, cloak moving in the wind."'
  v validate 01-01 | head -1
  v generate 01-01 --budget 1 --approved "演示：01-01 出 1 条" | tail -2
fi

echo
echo "✓ 演示项目在 $DEMO"
[ $have_img = 1 ] && echo "  生图审核页：python3 \"$IMG/scripts/review_web.py\" --config \"$DEMO/project.yaml\" --port 8860   → 打开 http://127.0.0.1:8860 ，在出图区点「要 / 不要」"
[ $have_vid = 1 ] && echo "  视频审片页：python3 \"$VID/scripts/review_web.py\" --config \"$DEMO/project.yaml\" --port 8861   → 打开 http://127.0.0.1:8861 ，看片给结论；然后在项目里跑 sync 取回"
echo "  模拟出的图是纯色方块、片子是纯色推近，这就对了。接真供应商见各 Skill 的 references/quickstart.md。"
