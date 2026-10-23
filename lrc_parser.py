"""LRC 歌词文件解析器 —— 纯逻辑模块，无外部依赖。"""

import re


class LrcParser:
    """解析 LRC 歌词文件，返回按时间排序的 (时间戳, 歌词文本) 列表。"""

    # 时间标签：[mm:ss.xx] 或 [mm:ss]
    TIME_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    # 翻译行：[tr mm:ss.xxx] / [tr mm:ss]（tr 后可多个空格；V1.11.0）
    TRANSLATION_RE = re.compile(r"\[\s*tr\s+(\d+):(\d+(?:\.\d+)?)\s*\]",
                                re.IGNORECASE)
    # 全局偏移：[offset:+/-ms]
    OFFSET_RE = re.compile(r"\[offset:([-\d]+)\]", re.IGNORECASE)

    @staticmethod
    def ms_key(seconds: float) -> int:
        """秒 → 毫秒整数键（翻译行按时间戳毫秒配对，避免浮点误差；V1.11.0）。"""
        return int(round(max(0.0, float(seconds)) * 1000))

    @classmethod
    def parse(cls, text: str) -> list[tuple[float, str]]:
        """解析 LRC 文本（兼容旧接口：仅返回歌词行，不含翻译行）。

        Args:
            text: LRC 文件的完整文本内容。

        Returns:
            按时间升序排列的 (秒数, 歌词行) 列表。
        """
        return cls.parse_with_translations(text)[0]

    @classmethod
    def parse_full(
            cls, text: str
    ) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
        """解析 LRC 文本 → (主歌词行, 翻译时间轴)，两者完全独立（V1.12.0）。

        - 主歌词行形如 `[mm:ss.xx]文本`，按时间升序；
        - 翻译行形如 `[tr mm:ss.xxx]翻译文本`，单独成为一条**独立时间轴**，
          不与主歌词行配对（时间戳不必对应任何歌词行），按时间升序；
        - 同时间戳的翻译行后者覆盖前者；
        - `[tr mm:ss.xxx]` 后方无内容的空行同样保留，表示「自此刻起无翻译」
          的时间轴标记（V1.11.1，播放端据此显示「下一句歌词」占位）；
        - `[offset:...]` 同时作用于两条时间轴。

        Returns:
            (lines, trans_lines)：均为按时间升序的 (秒数, 文本) 列表。
        """
        offset_ms = 0
        lines: list[tuple[float, str]] = []
        # {毫秒键: (秒数, 文本)} —— 同时间戳后者覆盖，最后按时间升序输出
        trans_entries: dict[int, tuple[float, str]] = {}

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

            # 翻译行：[tr mm:ss.xxx]文本 —— 进入独立翻译时间轴
            tr_match = cls.TRANSLATION_RE.search(raw)
            if tr_match:
                try:
                    ts = int(tr_match.group(1)) * 60 + float(tr_match.group(2))
                except ValueError:
                    continue
                tr_text = cls.TRANSLATION_RE.sub("", raw).strip()
                # 空文本行也保留为时间轴条目（V1.11.1）：
                # 表示「自此刻起无翻译」，播放端据此显示「下一句歌词」占位；
                # 同时间戳后者覆盖前者（显式清空同样生效）。
                trans_entries[cls.ms_key(ts)] = (ts, tr_text)
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

        # 应用全局时间偏移（两条时间轴同步位移）
        trans_lines = sorted(trans_entries.values(), key=lambda item: item[0])
        if offset_ms:
            delta = offset_ms / 1000.0
            lines = [(max(0.0, ts + delta), text) for ts, text in lines]
            trans_lines = [(max(0.0, ts + delta), text)
                           for ts, text in trans_lines]

        lines.sort(key=lambda item: item[0])
        return lines, trans_lines

    @classmethod
    def parse_with_translations(
            cls, text: str) -> tuple[list[tuple[float, str]], dict[int, str]]:
        """兼容接口：→ (歌词行列表, {毫秒键: 翻译文本})（V1.11.0 起）。

        V1.12.0 起主歌词与翻译完全分离，播放端应使用 `parse_full` 的
        翻译时间轴；本接口仅保留给按时间戳查表的旧调用方。

        Returns:
            (lines, translations)：lines 为按时间升序的 (秒数, 文本)；
            translations 为 {毫秒整数: 翻译文本}。
        """
        lines, trans_lines = cls.parse_full(text)
        return lines, {cls.ms_key(ts): t for ts, t in trans_lines}
