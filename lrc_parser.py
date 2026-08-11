"""LRC 歌词文件解析器 —— 纯逻辑模块，无外部依赖。"""

import re


class LrcParser:
    """解析 LRC 歌词文件，返回按时间排序的 (时间戳, 歌词文本) 列表。"""

    # 时间标签：[mm:ss.xx] 或 [mm:ss]
    TIME_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    # 全局偏移：[offset:+/-ms]
    OFFSET_RE = re.compile(r"\[offset:([-\d]+)\]", re.IGNORECASE)

    @classmethod
    def parse(cls, text: str) -> list[tuple[float, str]]:
        """解析 LRC 文本。

        Args:
            text: LRC 文件的完整文本内容。

        Returns:
            按时间升序排列的 (秒数, 歌词行) 列表。
        """
        offset_ms = 0
        lines: list[tuple[float, str]] = []

        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue

            # 解析全局偏移 [offset:xxx]
            offset_match = cls.OFFSET_RE.search(raw)
            if offset_match:
                try:
                    offset_ms = int(offset_match.group(1))
                except ValueError:
                    offset_ms = 0
                continue

            # 解析时间标签 [mm:ss.xx]
            times = cls.TIME_RE.findall(raw)
            if not times:
                continue

            # 提取歌词文本（去除时间标签后的剩余内容）
            lyric_text = cls.TIME_RE.sub("", raw).strip()
            if not lyric_text:
                lyric_text = " "

            for minutes, seconds in times:
                try:
                    timestamp = int(minutes) * 60 + float(seconds)
                except ValueError:
                    continue
                lines.append((timestamp, lyric_text))

        # 应用全局时间偏移
        if offset_ms:
            delta = offset_ms / 1000.0
            lines = [(max(0.0, ts + delta), text) for ts, text in lines]

        lines.sort(key=lambda item: item[0])
        return lines
