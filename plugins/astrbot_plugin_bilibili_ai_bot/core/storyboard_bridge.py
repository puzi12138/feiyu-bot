"""Expose the shared local-only storyboard adapter without plugin imports."""

import importlib.util
from pathlib import Path


_path = Path("D:/QQBot/scripts/BiliStoryboard.py")
_spec = importlib.util.spec_from_file_location("feiyu_bili_storyboard", _path)
_shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared)

ROOT = _shared.ROOT
NODE = _shared.NODE
CLI = _shared.CLI
FFMPEG = _shared.FFMPEG
FFPROBE = _shared.FFPROBE
storyboard_segment = _shared.storyboard_segment
storyboard_sheets = _shared.storyboard_sheets
