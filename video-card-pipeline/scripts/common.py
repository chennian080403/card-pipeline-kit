"""Small helpers shared by the vpipeline command modules."""

from __future__ import annotations

import sys
from pathlib import Path

from core.jsonio import now, read_json, write_json  # noqa: F401 -- re-exported for the command modules


def die(message):
    raise SystemExit(f"✗ {message}")


# The Toonflow canvas is an optional plugin kept outside the skill, next to it (plugins/画布/video).
# Nothing imports it unless a provider or video.board asks for kind: toonflow.
CANVAS_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "画布" / "video"


def canvas_plugin(*specs):
    """Put the canvas plugin on sys.path and return its `canvas` module. `plugin_dir` in any spec overrides."""
    folder = next((Path(s["plugin_dir"]).expanduser() for s in specs if s and s.get("plugin_dir")), CANVAS_PLUGIN)
    if not (folder / "canvas.py").is_file():
        raise RuntimeError(f"canvas plugin not installed: {folder}/canvas.py is missing (optional plugin, not shipped with the skill; set plugin_dir to where you put it)")
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import canvas
    return canvas
