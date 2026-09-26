"""配置持久化 —— 读写 `_internal/config/data.json`。

结构：
{
  "folders": [path, ...],      # 已加载的文件夹（下次启动自动恢复，不存在则跳过）
  "active_folder": path|null,  # 上次选中的文件夹
  "config": {                  # 右侧操作栏 / 播放模式 / 置顶 / 桌面歌词条 配置
    "speed", "pitch_fix", "pitch_shift", "volume",
    "lrc_offset", "balance", "gain", "play_mode", "always_on_top",
    "lyric_bar", "lyric_alpha", "lyric_width", "lyric_font"
  }
}

读写均容错：损坏/缺失时返回默认结构；写前自动创建目录。
"""

import json
import os
import sys

DEFAULTS: dict = {
    "folders": [],
    "active_folder": None,
    "config": {
        "speed": 1.0,
        "pitch_fix": False,
        "pitch_shift": 0.0,
        "volume": 100.0,
        "lrc_offset": 0.0,
        "balance": 0.0,
        "gain": 1.0,
        "play_mode": "loop_all",
        "always_on_top": False,
        "lyric_bar": "off",       # 桌面歌词条：off / top / bottom
        "lyric_alpha": 85,       # 歌词条透明度（30~100 %）
        "lyric_width": 25,       # 歌词条初始宽度（占屏幕 %）
        "lyric_font": 18,        # 歌词条字体大小（px）
    },
}


def _base_dir() -> str:
    """配置根目录：打包为 _MEIPASS（exe 旁 _internal），开发为脚本目录/_internal。"""
    try:
        return sys._MEIPASS  # type: ignore[attr-defined]
    except AttributeError:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_internal")


def data_path() -> str:
    """data.json 绝对路径。"""
    return os.path.join(_base_dir(), "config", "data.json")


def _deep_copy_default() -> dict:
    """返回默认结构的深拷贝。"""
    return json.loads(json.dumps(DEFAULTS))


def load() -> dict:
    """读取配置；文件缺失/损坏/缺字段时返回（补全的）默认结构。"""
    merged = _deep_copy_default()
    try:
        with open(data_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return merged
    if not isinstance(raw, dict):
        return merged
    for key in merged:
        if key in raw:
            merged[key] = raw[key]
    cfg = merged["config"]
    raw_cfg = raw.get("config")
    if isinstance(raw_cfg, dict):
        for key in cfg:
            if key in raw_cfg:
                cfg[key] = raw_cfg[key]
    return merged


def save(data: dict) -> None:
    """写入配置（自动建目录；失败静默，不阻塞主流程）。"""
    try:
        path = data_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
