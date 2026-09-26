"""音乐播放器主应用 —— UI 布局、交互逻辑、歌词同步。"""

import os
import random
import threading
import time
from bisect import bisect_right
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

import config_store
from operation_panel import OperationPanel
from lrc_parser import LrcParser
from audio_engine import AudioEngine
from cover_utils import extract_cover_art, cover_to_tk_image
from utils import (
    format_time,
    format_hms,
    read_text_file,
    get_duration_mutagen,
    make_progress_bar,
    status_sep,
    bind_tooltip,
    resource_path,
    APP_NAME,
    APP_VERSION,
)
from console import PlayerConsole


# ===========================================================================
# 全局常量
# ===========================================================================

BG_COLOR = "#23272E"
FG_COLOR = "#C8C8C8"
ACCENT_COLOR = "#6FA3FF"
SUBTLE_COLOR = "#3A3F46"
BUTTON_WIDTH = 10
TICK_INTERVAL_MS = 10

PLAY_MODES = [
    ("列表循环", "loop_all"),
    ("单曲循环", "loop_one"),
    ("仅一首", "single"),
    ("随机播放", "shuffle"),
]

# 桌面歌词条「自定义」锚点（九宫格）→ 条左上角占 (屏宽-条宽)/(屏高-条高) 的比例
LYRIC_ANCHOR_FRAC = {
    "tl": (0.0, 0.0), "t": (0.5, 0.0), "tr": (1.0, 0.0),
    "l": (0.0, 0.5), "c": (0.5, 0.5), "r": (1.0, 0.5),
    "bl": (0.0, 1.0), "b": (0.5, 1.0), "br": (1.0, 1.0),
}

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac"}


# ===========================================================================
# LrcPlayerApp
# ===========================================================================

class LrcPlayerApp:
    """歌词播放器主应用。"""

    def __init__(self, root: tk.Tk, initial_file: str | None = None) -> None:
        self.root = root
        try:
            self.root.iconbitmap(resource_path("MP.ico"))
        except Exception:
            pass  # 图标文件缺失时不阻塞启动
        # ---- 屏幕适配：屏幕 <1366x768 时整体缩小，避免溢出 ----
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.small_screen = self.screen_w < 1366 or self.screen_h < 768
        base_scale = min(1.0, self.screen_w / 1366.0, self.screen_h / 768.0)
        if self.small_screen:
            # 小屏：基础比例上再缩小 15%（×0.85），下限 0.65
            self.ui_scale = max(0.65, base_scale * 0.85)
        else:
            self.ui_scale = 1.0

        title = f"{APP_NAME} V{APP_VERSION}"
        if self.small_screen:
            title += " 缩小兼容模式"
        self.root.title(title)
        self.root.configure(bg=BG_COLOR)
        self.root.minsize(self._px(1280), self._px(660))
        self.root.resizable(True, True)

        # ---- 音频引擎 ----
        self.engine = AudioEngine()

        # ---- 音频状态 ----
        self.audio_path: str | None = None
        self.lrc_path: str | None = None
        self.duration: float | None = None

        # ---- 歌曲列表 ----
        # 多文件夹模型：folder_tabs 保存各已加载文件夹的扫描结果（缓存）；
        # audio_items / audio_root 恒指向"当前选中文件夹"，其余代码无需感知多文件夹。
        self.folder_tabs: list[dict] = []          # 每项 {path,title,items,status}
        self.active_folder_index: int | None = None
        self._folder_scan_gen: dict[str, int] = {}  # path -> 该文件夹扫描世代
        self._scanning_paths: set[str] = set()     # 正在后台扫描的文件夹(normcase)
        self._folder_hover_index: int | None = None
        self._folder_del_idx: int | None = None     # × 按钮当前所在行索引
        self._folder_title: str | None = None        # 当前选中文件夹显示标题
        self.audio_items: list[dict[str, str | int | None]] = []
        self.audio_root: str | None = None
        self.current_song_index: int | None = None
        self.viewed_song_index: int | None = None
        self._bg_cache_index = 0
        self._bg_cache_gen = 0
        self._first_scan_done = False
        self._info_req_id = 0
        self.interlude_items: list[dict] = []
        self._cover_photo = None
        self._tooltip = None  # 悬停提示窗口
        self._scan_total = 0
        self._scan_done = 0
        self._il_move_mode = False
        self._move_locked_index: int | None = None
        self._clear_confirm_active = False
        self._clear_confirm_timer: str | None = None

        # ---- 歌词浮层 ----
        self._lyric_overlay = None  # 全窗口歌词浮层 Frame
        self._overlay_cover_photo = None
        self._overlay_lyric_vars: list[tk.StringVar] = []

        # ---- 桌面歌词条（独立置顶小窗，模式 off / top / bottom / custom）----
        self._lyric_bar: tk.Toplevel | None = None
        self._lyric_bar_label: tk.Label | None = None
        self._lyric_bar_var: tk.StringVar | None = None
        self._lyric_bar_mode = "off"
        self._lyric_bar_alpha = 85        # 歌词条透明度（30~100 %）
        self._lyric_bar_width_pct = 25    # 歌词条初始宽度（占屏幕 %）
        self._lyric_bar_font_size = 18    # 歌词条字体大小（px）
        self._lyric_bar_anchor = "c"      # 自定义锚点（九宫格：tl/t/tr/l/c/r/bl/b/br）
        self._lyric_bar_x = 0             # 自定义 X 偏移（px，相对锚点）
        self._lyric_bar_y = 0             # 自定义 Y 偏移（px，相对锚点）
        self._lyric_bar_size = (0, 0)     # 最近一次刷新后的条尺寸 (width, height)
        self._lyric_bar_text: str | None = None   # 条文本缓存（拖动中免重建主干行）
        self._lyric_xy_range: tuple[int, int, int, int] | None = None  # X/Y 范围缓存

        # ---- 播放状态 ----
        self.is_playing = False
        self.is_paused = False
        self.play_started_at: float | None = None
        self.base_time = 0.0
        self.lrc_offset = 0.0  # 歌词偏移（秒），正值=歌词延后，负值=歌词提前

        # ---- 歌词 ----
        self.lrc_lines: list[tuple[float, str]] = []
        self.lrc_times: list[float] = []
        self.current_lrc_index = -1

        # ---- UI 状态 ----
        self.user_seeking = False
        self.always_on_top = False
        self.play_mode_index = 0
        self.play_mode = PLAY_MODES[self.play_mode_index][1]

        # ---- 字体（小屏按 ui_scale 缩放）----
        self.title_font = self._pick_font("汉仪文黑-85W", self._font_size(18))
        self.lyric_font = self._pick_font("汉仪文黑-85W", self._font_size(13))
        self.info_font = self._pick_font("Jetbrains Mono", self._font_size(11))
        self.list_font = self._pick_font("汉仪文黑-85W", self._font_size(11))
        self.button_font = self._pick_font("汉仪文黑-85W", self._font_size(12))
        self.button_font_sm = self._pick_font("汉仪文黑-85W", self._font_size(10))
        self.overlay_name_font = self._pick_font("汉仪文黑-85W", self._font_size(20))
        self.overlay_artist_font = self._pick_font("汉仪文黑-85W", self._font_size(14))
        self.overlay_lyric_big_font = self._pick_font("汉仪文黑-85W", self._font_size(24))
        self.overlay_lyric_small_font = self._pick_font("汉仪文黑-85W", self._font_size(13))
        self.lyric_bar_font = self._pick_font(
            "汉仪文黑-85W", self._font_size(self._lyric_bar_font_size))

        # ---- 构建 ----
        self._configure_style()
        self._build_ui()

        if not self.engine.ready:
            self._disable_audio_controls()
            messagebox.showwarning("音频后端", "音频后端不可用（需 sounddevice 或 pygame）。")

        # 非 sounddevice 后端不支持倍速/保音高/平衡/增益，禁用对应控件
        if self.engine.backend != "sounddevice":
            self._disable_op_controls()

        # 未加载歌曲前统一禁用播放相关控件
        self._update_controls_state()

        # 启动恢复：读取 data.json 应用操作栏/模式/置顶，并计划后台恢复文件夹
        self._load_persisted_state()

        self.root.after(TICK_INTERVAL_MS, self._tick)
        self._setup_keyboard_shortcuts()

        # 控制台（创建后隐藏，点击「控制台」按钮显示）
        self.console = PlayerConsole(self)
        self.console.win.withdraw()

        if initial_file and os.path.isfile(initial_file):
            if self._load_audio_file(initial_file):
                self._auto_load_lrc(initial_file)
                self._play()   # 自动开始播放

    def handle_external_file(self, path: str) -> None:
        """外部进程打开文件时调用的入口（主线程执行）。"""
        if not os.path.exists(path):
            return
        if self._load_audio_file(path):
            self._auto_load_lrc(path)
            self._play()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ==================================================================
    # 字体 & 样式
    # ==================================================================

    def _px(self, v: int, min_px: int = 0) -> int:
        """按 ui_scale 缩放像素尺寸（可设下限，如操作栏最小 260px）。"""
        return max(min_px, int(round(v * self.ui_scale)))

    def _font_size(self, n: int) -> int:
        """按 ui_scale 缩放字号（下限 8，避免过小）。"""
        return max(8, int(round(n * self.ui_scale)))

    def _pick_font(self, family: str, size: int, weight: str = "normal") -> tkfont.Font:
        """查找可用字体，降级到系统默认。"""
        available = list(tkfont.families(self.root))
        lookup = {name.lower(): name for name in available}
        family = lookup.get(family.lower(), family)
        if family not in available:
            family = "Segoe UI" if "Segoe UI" in lookup.values() else "TkDefaultFont"
        return tkfont.Font(family=family, size=size, weight=weight)

    def _configure_style(self) -> None:
        """配置 ttk 控件主题与样式。"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "LRC.Horizontal.TScale",
            troughcolor=SUBTLE_COLOR,
            background=BG_COLOR,
        )
        style.configure(
            "LRC.Vertical.TScrollbar",
            troughcolor=BG_COLOR,
            background=SUBTLE_COLOR,
            bordercolor=BG_COLOR,
            arrowcolor=FG_COLOR,
            relief="flat",
        )
        style.map(
            "LRC.Vertical.TScrollbar",
            background=[("active", ACCENT_COLOR), ("!active", SUBTLE_COLOR)],
            arrowcolor=[("active", BG_COLOR), ("!active", FG_COLOR)],
        )

    # ==================================================================
    # UI 构建
    # ==================================================================

    def _build_ui(self) -> None:
        """左右双栏布局：左侧主控区，右侧封面+插播列表+按钮区。"""
        outer = tk.Frame(self.root, bg=BG_COLOR)
        outer.pack(fill="both", expand=True,
                   padx=self._px(18), pady=self._px(8))
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=0)
        outer.rowconfigure(0, weight=1)

        # ---- 左栏 ----
        left_col = tk.Frame(outer, bg=BG_COLOR)
        left_col.grid(row=0, column=0, sticky="nsew")

        self._build_header(left_col)
        self._build_button_rows(left_col)

        # 拖拽取消条（初始隐藏，拖动进度条时显示）
        self._cancel_frame = tk.Frame(left_col, bg="#6B1010",
                                      height=self._px(40))
        self._cancel_label = tk.Label(
            self._cancel_frame, text="拖动到此处以取消",
            bg="#6B1010", fg="#FF5555", font=self.title_font)
        self._cancel_label.pack(expand=True)

        self._build_progress_bar(left_col)

        self.bottom_frame = tk.Frame(left_col, bg=BG_COLOR)
        self._build_bottom_panels(self.bottom_frame)

        # ---- 右栏（固定宽度；子控件为 pack，须用 pack_propagate 保持定宽）----
        right_col = tk.Frame(outer, bg=BG_COLOR, width=self._px(260))
        right_col.grid(row=0, column=1, sticky="nsew", padx=(self._px(12), 0))
        right_col.pack_propagate(False)
        self._build_right_column(right_col)

        # ---- 操作区（固定宽度：倍速 / 音量 / 高级功能；小屏优先缩短，最小 260px）----
        op_col = tk.Frame(outer, bg=BG_COLOR, width=self._px(340, 260))
        op_col.grid(row=0, column=2, sticky="nsew", padx=(self._px(12), 0))
        op_col.pack_propagate(False)  # 子控件为 pack，须用 pack_propagate 保持定宽
        # 操作区面板（自包含滚动容器 + 倍速/音高/音量/高级/选项，见 operation_panel.py）
        self.op = OperationPanel(self, op_col)

        # ---- 状态栏 ----
        self._build_status_bar()

    def _build_status_bar(self) -> None:
        """窗口底部状态栏：程序状态、音频信息、音量、列表统计。"""
        bar = tk.Frame(self.root, bg=SUBTLE_COLOR, height=self._px(24))
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        status_font = tkfont.Font(family="Segoe UI", size=self._font_size(10))
        lbl_cfg = dict(bg=SUBTLE_COLOR, fg=FG_COLOR, font=status_font,
                       anchor="w", padx=6)

        # 程序状态 + 文字进度条（w=210）
        prog_frame = tk.Frame(bar, bg=SUBTLE_COLOR,
                              width=self._px(210), height=self._px(24))
        prog_frame.pack(side="left")
        prog_frame.pack_propagate(False)
        self._prog_var = tk.StringVar(value="就绪")
        prog_lbl = tk.Label(prog_frame, textvariable=self._prog_var, **lbl_cfg)
        prog_lbl.pack(fill="both", expand=True)
        bind_tooltip(prog_lbl, self._prog_var, self, status_font)

        status_sep(bar)

        # 音频设备（w=260，显示输出设备名）
        audio_frame = tk.Frame(bar, bg=SUBTLE_COLOR,
                               width=self._px(260), height=self._px(24))
        audio_frame.pack(side="left")
        audio_frame.pack_propagate(False)
        self._audio_var = tk.StringVar(value=self._read_audio_info())
        audio_lbl = tk.Label(audio_frame, textvariable=self._audio_var, **lbl_cfg)
        audio_lbl.pack(fill="both", expand=True)
        bind_tooltip(audio_lbl, self._audio_var, self, status_font)

        status_sep(bar)

        # 歌曲列表统计（w=130）
        list_frame = tk.Frame(bar, bg=SUBTLE_COLOR,
                              width=self._px(130), height=self._px(24))
        list_frame.pack(side="left")
        list_frame.pack_propagate(False)
        self._liststat_var = tk.StringVar(value="共 0 首")
        list_lbl = tk.Label(list_frame, textvariable=self._liststat_var, **lbl_cfg)
        list_lbl.pack(fill="both", expand=True)
        bind_tooltip(list_lbl, self._liststat_var, self, status_font)

        status_sep(bar)

        # 插播统计（w=85）
        il_frame = tk.Frame(bar, bg=SUBTLE_COLOR,
                            width=self._px(85), height=self._px(24))
        il_frame.pack(side="left")
        il_frame.pack_propagate(False)
        self._ilst_var = tk.StringVar(value="插播: 0")
        il_lbl = tk.Label(il_frame, textvariable=self._ilst_var, **lbl_cfg)
        il_lbl.pack(fill="both", expand=True)
        bind_tooltip(il_lbl, self._ilst_var, self, status_font)

        # 小屏：状态栏右侧黄色提示当前分辨率
        if self.small_screen:
            small_frame = tk.Frame(bar, bg=SUBTLE_COLOR,
                                   width=self._px(150), height=self._px(24))
            small_frame.pack(side="right")
            small_frame.pack_propagate(False)
            self._smallscr_var = tk.StringVar(
                value=f"小屏 {self.screen_w}x{self.screen_h}")
            small_lbl = tk.Label(small_frame, textvariable=self._smallscr_var,
                                 bg=SUBTLE_COLOR, fg="#FFD54F",
                                 font=status_font, anchor="e", padx=6)
            small_lbl.pack(fill="both", expand=True)

        # 初始刷新
        self._refresh_status_bar()

    # ==================================================================
    # 键盘快捷键（Space / F11）
    # ==================================================================

    def _setup_keyboard_shortcuts(self) -> None:
        """全局键盘快捷键（已移除 Alt 快捷键）：Space 播放/暂停、F11 全屏。"""
        # Space → 播放/暂停
        self.root.bind("<space>",
                       lambda e: (self._toggle_play_pause(), "break")[1])
        # F11 → 全屏
        self.root.bind("<F11>",
                       lambda e: (self._toggle_fullscreen(), "break")[1])

    def _toggle_fullscreen(self) -> None:
        """切换全屏状态。

        Windows 上 Tk 进入全屏会自动置顶（-topmost），但退出全屏不会自动去掉。
        修复要点（覆盖"未开置顶也莫名置顶"等场景）：
        - 以 always_on_top（用户意图）为准记录/恢复，而非读取实际 -topmost，
          避免把 Windows 残留的置顶误记为用户意图；
        - 用户在全屏期间通过按钮/控制台改动置顶时（_topmost_edited_in_fullscreen），
          退出后尊重其最新意图，而不是被还原成进入前的旧状态；
        - 退出全屏时把实际 -topmost 强制同步回 always_on_top，杜绝残留置顶。
        """
        state = not bool(self.root.attributes("-fullscreen"))
        if state:
            # 进入全屏：记录用户当前的置顶意图，并复位"全屏期间改过置顶"标记
            self._saved_fullscreen_topmost = self.always_on_top
            self._topmost_edited_in_fullscreen = False
        self.root.attributes("-fullscreen", state)
        if not state:
            # 退出全屏：全屏期间改过则保留最新选择，否则恢复进入前意图
            if not getattr(self, "_topmost_edited_in_fullscreen", False):
                self.always_on_top = bool(
                    getattr(self, "_saved_fullscreen_topmost",
                            self.always_on_top))
            # 始终把实际置顶同步为用户意图，杜绝残留
            self.root.attributes("-topmost", self.always_on_top)
            self.op.refresh_topmost_buttons()

    def _build_header(self, parent: tk.Frame) -> None:
        """顶部：当前歌词行 + 音视频信息行。"""
        header = tk.Frame(parent, bg=BG_COLOR)
        header.pack(fill="x")

        self.now_line_var = tk.StringVar(value="未加载歌词")
        now_label = tk.Label(
            header,
            textvariable=self.now_line_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.title_font,
            anchor="w",
        )
        now_label.pack(fill="x")

        self.info_var = tk.StringVar(value="音频: --- | 歌词: ---/---")
        info_label = tk.Label(
            header,
            textvariable=self.info_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.info_font,
            anchor="w",
        )
        info_label.pack(fill="x", pady=(4, 6))

    def _build_button_rows(self, parent: tk.Frame) -> None:
        """两行按钮，每行 4 个。包裹在容器中供取消条覆盖。"""
        self._btn_container = tk.Frame(parent, bg=BG_COLOR)
        self._btn_container.pack(fill="x")
        btn_cfg = dict(
            bg=SUBTLE_COLOR,
            fg=FG_COLOR,
            font=self.button_font,
            width=BUTTON_WIDTH,
            activebackground=ACCENT_COLOR,
            activeforeground=BG_COLOR,
            relief="flat",
            padx=self._px(8),
            pady=self._px(5),
        )

        # ---- 第一行 ----
        row1 = tk.Frame(self._btn_container, bg=BG_COLOR)
        row1.pack(fill="x")

        self.open_file_btn = tk.Button(
            row1, text="打开文件", command=self._open_file, **btn_cfg)
        self.open_file_btn.pack(side="left")

        self.prev_btn = tk.Button(
            row1, text="上一曲", command=self._prev_track, **btn_cfg)
        self.prev_btn.pack(side="left", padx=(self._px(6), 0))

        self.play_pause_btn = tk.Button(
            row1, text="播放", command=self._toggle_play_pause, **btn_cfg)
        self.play_pause_btn.pack(side="left", padx=(self._px(6), 0))

        self.next_btn = tk.Button(
            row1, text="下一曲", command=self._next_track, **btn_cfg)
        self.next_btn.pack(side="left", padx=(self._px(6), 0))

        # ---- 第二行 ----
        row2 = tk.Frame(self._btn_container, bg=BG_COLOR)
        row2.pack(fill="x", pady=(6, 0))

        self.open_folder_btn = tk.Button(
            row2, text="打开文件夹", command=self._scan_folder, **btn_cfg)
        self.open_folder_btn.pack(side="left")

        self.back_10s_btn = tk.Button(
            row2, text="后退10s", command=self._seek_back_10s, **btn_cfg)
        self.back_10s_btn.pack(side="left", padx=(self._px(6), 0))

        self.stop_btn = tk.Button(
            row2, text="停止", command=self._stop, **btn_cfg)
        self.stop_btn.pack(side="left", padx=(self._px(6), 0))

        self.forward_10s_btn = tk.Button(
            row2, text="前进10s", command=self._seek_forward_10s, **btn_cfg)
        self.forward_10s_btn.pack(side="left", padx=(self._px(6), 0))

    def _build_progress_bar(self, parent: tk.Frame) -> None:
        """进度条 + 右侧时间标签。"""
        progress_row = tk.Frame(parent, bg=BG_COLOR)
        progress_row.pack(fill="x", pady=(8, 4))

        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(
            progress_row,
            style="LRC.Horizontal.TScale",
            orient="horizontal",
            from_=0.0,
            to=100.0,
            variable=self.seek_var,
            command=self._on_seek_changed,
        )
        self.seek_scale.pack(side="left", fill="x", expand=True)
        self.seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        self.time_var = tk.StringVar(value="00:00 / --:--")
        time_label = tk.Label(
            progress_row,
            textvariable=self.time_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.info_font,
            padx=10,
        )
        time_label.pack(side="right")

    def _build_bottom_panels(self, parent: tk.Frame) -> None:
        """底部纵向布局：中部（左=文件夹列表 + 右=歌曲列表）+ 歌曲信息条。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)
        parent.rowconfigure(2, weight=0)

        # ---- 中部：左=文件夹面板（新增，定宽），右=歌曲面板（可伸缩）----
        middle = tk.Frame(parent, bg=BG_COLOR)
        middle.grid(row=0, column=0, sticky="nsew")
        middle.columnconfigure(1, weight=1)
        middle.rowconfigure(0, weight=1)

        # 左侧文件夹面板
        folder_panel = tk.Frame(middle, bg=BG_COLOR, width=self._px(170, 120))
        folder_panel.grid(row=0, column=0, sticky="nsew", padx=(0, self._px(8)))
        folder_panel.pack_propagate(False)
        self._build_folder_panel(folder_panel)

        # 右侧歌曲面板
        song_panel = tk.Frame(middle, bg=BG_COLOR)
        song_panel.grid(row=0, column=1, sticky="nsew")

        self._songlist_label = tk.Label(
            song_panel, text="歌曲列表", bg=BG_COLOR, fg=FG_COLOR,
            font=self.list_font, anchor="w")
        self._songlist_label.pack(anchor="w")

        list_inner = tk.Frame(song_panel, bg=BG_COLOR)
        list_inner.pack(fill="both", expand=True)

        self.song_list = tk.Listbox(
            list_inner,
            bg=BG_COLOR, fg=FG_COLOR, font=self.list_font,
            selectbackground=ACCENT_COLOR, selectforeground=BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False,
        )
        self.song_list.pack(side="left", fill="both", expand=True)
        self.song_list.bind("<<ListboxSelect>>", self._on_song_select)

        song_scroll = ttk.Scrollbar(
            list_inner, command=self.song_list.yview, style="LRC.Vertical.TScrollbar")
        song_scroll.pack(side="right", fill="y")
        self.song_list.config(yscrollcommand=song_scroll.set)

        # ---- 分隔线 ----
        sep = tk.Frame(parent, bg=SUBTLE_COLOR, height=1)
        sep.grid(row=1, column=0, sticky="ew", pady=(4, 2))

        # ---- 歌曲信息条（全宽，grid 流式布局）----
        info_outer = tk.Frame(parent, bg=BG_COLOR)
        info_outer.grid(row=2, column=0, sticky="ew")

        # 列宽策略：第 0 列（文件名/专辑）弹性伸缩，右边三栏定宽
        info_outer.columnconfigure(0, weight=1)
        # 右边三栏的固定列宽（像素，按 ui_scale 缩放）
        _FIXED_COL_WIDTHS = {1: self._px(120), 2: self._px(100), 3: self._px(160)}

        self.song_info_labels: dict[str, tk.StringVar] = {}
        self._song_info_val_widgets: dict[str, tk.Label] = {}
        info_fields = [
            ("文件名", 0, 0), ("时长", 0, 1), ("格式", 0, 2), ("文件大小", 0, 3),
            ("专辑", 1, 0), ("音轨号", 1, 1), ("歌词", 1, 2),
        ]
        for field, row, col in info_fields:
            seg = tk.Frame(info_outer, bg=BG_COLOR)
            seg.grid(row=row, column=col, sticky="w",
                     padx=(0 if col == 0 else 12, 0), pady=1)

            key_lbl = tk.Label(
                seg, text=f"{field}:", bg=BG_COLOR, fg=ACCENT_COLOR,
                font=self.info_font, anchor="w")
            key_lbl.pack(side="left")

            var = tk.StringVar(value="-")
            val_lbl = tk.Label(
                seg, textvariable=var, bg=BG_COLOR, fg=FG_COLOR,
                font=self.info_font, anchor="w")
            val_lbl.pack(side="left", padx=(2, 0))
            self.song_info_labels[field] = var
            self._song_info_val_widgets[field] = val_lbl

            # 右边三栏固定宽度，超出裁剪
            if col > 0:
                w = _FIXED_COL_WIDTHS.get(col, self._px(75))
                seg.config(width=w, height=self._px(22))
                seg.pack_propagate(False)

            # 悬停显示完整文本
            bind_tooltip(val_lbl, var, self, self.info_font)

        # 初始化空列表
        self._refresh_song_list()

    def _build_folder_panel(self, parent: tk.Frame) -> None:
        """左侧文件夹面板：标题 + 文件夹 Listbox + 悬浮删除(×)按钮。

        每个文件夹一行；鼠标悬浮到某行时该行右端显示 ×，点击删除该文件夹
        （正在播放其中的歌曲时保留播放，仅移除列表条目）。
        """
        tk.Label(parent, text="文件夹", bg=BG_COLOR, fg=FG_COLOR,
                 font=self.list_font, anchor="w").pack(anchor="w")

        inner = tk.Frame(parent, bg=BG_COLOR)
        inner.pack(fill="both", expand=True, pady=(2, 0))

        self.folder_list = tk.Listbox(
            inner,
            bg=BG_COLOR, fg=FG_COLOR, font=self.list_font,
            selectbackground=ACCENT_COLOR, selectforeground=BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False,
        )
        self.folder_list.pack(side="left", fill="both", expand=True)
        fsb = ttk.Scrollbar(inner, command=self.folder_list.yview,
                            style="LRC.Vertical.TScrollbar")
        fsb.pack(side="right", fill="y")
        self.folder_list.config(yscrollcommand=fsb.set)

        self.folder_list.bind("<<ListboxSelect>>", self._on_folder_select)
        self.folder_list.bind("<Motion>", self._on_folder_motion)
        self.folder_list.bind("<Leave>", self._on_folder_leave)

        # 悬浮「×」删除按钮：作为 parent 子控件，按 Listbox 行坐标 place
        self._folder_del_btn = tk.Button(
            parent, text="×", command=self._remove_hover_folder,
            bg=SUBTLE_COLOR, fg="#FF7777",
            activebackground="#6B1010", activeforeground="#FF5555",
            relief="flat", bd=0, padx=0, pady=0, font=self.list_font)
        self._folder_del_btn.place_forget()
        self._folder_del_btn.bind("<Enter>", self._on_folder_del_enter)
        self._folder_del_btn.bind("<Leave>", self._on_folder_del_leave)
        self._folder_hover_index = None
        self._folder_del_idx = None
        self._refresh_folder_list()

    # ==================================================================
    # 多文件夹：刷新 / 添加 / 选中 / 删除
    # ==================================================================

    def _refresh_folder_list(self) -> None:
        """重建文件夹列表显示，并同步高亮当前选中项。"""
        self.folder_list.delete(0, "end")
        for tab in self.folder_tabs:
            title = tab.get("title") or os.path.basename(tab.get("path") or "")
            self.folder_list.insert("end", title)
        if self.active_folder_index is not None:
            self.folder_list.selection_clear(0, "end")
            self.folder_list.selection_set(self.active_folder_index)
            try:
                self.folder_list.see(self.active_folder_index)
            except tk.TclError:
                pass
        self._hide_folder_del()

    def _on_folder_select(self, _event: tk.Event | None = None) -> None:
        """文件夹列表选中变化 → 切换右侧显示的歌曲列表。"""
        sel = self.folder_list.curselection()
        if not sel:
            return
        self._select_folder_by_index(sel[0])

    def _on_folder_motion(self, event: tk.Event) -> None:
        """鼠标在文件夹列表移动：定位所在行，在行右端显示 ×（幂等防闪烁）。"""
        if not self.folder_tabs:
            return
        try:
            idx = self.folder_list.nearest(event.y)
        except tk.TclError:
            return
        if idx < 0 or idx >= len(self.folder_tabs):
            self._hide_folder_del()
            return
        try:
            bbox = self.folder_list.bbox(idx)
        except tk.TclError:
            bbox = None
        if not bbox:
            self._hide_folder_del()
            return
        # 同一行且按钮已显示 → 不再重复 place（避免鼠标微动时高频重绘闪烁）
        if (self._folder_hover_index == idx and self._folder_del_idx == idx
                and self._folder_del_btn.winfo_ismapped()):
            return
        self._folder_hover_index = idx
        self._folder_del_idx = idx
        _x, y, _w, h = bbox
        btn = self._folder_del_btn
        # y-1 / 高 h+1：Listbox 文本区相对 place 坐标有约 1px 偏移，修正“偏下”
        btn.place(in_=self.folder_list, relx=1.0, x=-self._px(22),
                  y=max(0, y - 1), width=self._px(18), height=h + 1)
        btn.lift()

    def _on_folder_del_enter(self, _event: tk.Event | None = None) -> None:
        """鼠标进入 × 按钮：置顶保持可见（避免触发列表 Leave 而闪烁消失）。"""
        if self._folder_del_btn.winfo_ismapped():
            self._folder_del_btn.lift()

    def _on_folder_del_leave(self, _event: tk.Event | None = None) -> None:
        """鼠标离开 × 按钮：指针已不在其内则隐藏。"""
        if self._pointer_in_widget(self._folder_del_btn):
            return
        self._hide_folder_del()

    def _pointer_in_widget(self, widget) -> bool:
        """当前指针是否在某控件区域内。"""
        try:
            x = self.root.winfo_pointerx() - widget.winfo_rootx()
            y = self.root.winfo_pointery() - widget.winfo_rooty()
            return (0 <= x <= widget.winfo_width()
                    and 0 <= y <= widget.winfo_height())
        except Exception:
            return False

    def _hide_folder_del(self) -> None:
        """隐藏 × 并清空悬浮行记录（幂等）。"""
        try:
            self._folder_del_btn.place_forget()
        except tk.TclError:
            pass
        self._folder_hover_index = None
        self._folder_del_idx = None

    def _on_folder_leave(self, _event: tk.Event | None = None) -> None:
        """鼠标离开文件夹列表：若指针仍悬停在 × 上则保留，否则隐藏。"""
        if self._pointer_in_widget(self._folder_del_btn):
            return
        self._hide_folder_del()

    def _remove_hover_folder(self, _event: tk.Event | None = None) -> None:
        """删除当前悬浮行对应的文件夹（正在播放其中的歌曲时保留播放）。"""
        idx = self._folder_hover_index
        self._hide_folder_del()
        if idx is None:
            return
        if 0 <= idx < len(self.folder_tabs):
            self._remove_folder_by_index(idx)

    def _update_songlist_title(self) -> None:
        """更新右侧歌曲列表标题：显示当前选中文件夹名。"""
        lbl = getattr(self, "_songlist_label", None)
        if lbl is None:
            return
        if (self.active_folder_index is not None
                and 0 <= self.active_folder_index < len(self.folder_tabs)):
            name = self.folder_tabs[self.active_folder_index].get("title") \
                or os.path.basename(
                    self.folder_tabs[self.active_folder_index].get("path")
                    or "")
            lbl.config(text=f"歌曲列表 - {name}")
        else:
            lbl.config(text="歌曲列表")

    def _add_folder(self, folder: str) -> str:
        """把文件夹加入多文件夹列表并选中（若已存在仅选中缓存，不重扫）。

        返回提示文本。GUI「打开文件夹」与控制台 open <文件夹> 共用。
        """
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            return "路径不存在或不是文件夹"
        key = os.path.normcase(os.path.abspath(folder))
        for i, tab in enumerate(self.folder_tabs):
            if os.path.normcase(os.path.abspath(
                    tab.get("path") or "")) == key:
                self._select_folder_by_index(i)
                self._schedule_config_save()
                return f"文件夹已在列表中，已切到: {os.path.basename(folder)}"
        tab = {
            "path": folder,
            "title": os.path.basename(folder) or folder,
            "items": [],
            "status": "loading",
        }
        self.folder_tabs.append(tab)
        self._refresh_folder_list()
        self._select_folder_by_index(len(self.folder_tabs) - 1)
        self._ensure_bottom_shown()
        self._start_folder_scan(len(self.folder_tabs) - 1)
        self._schedule_config_save()
        return f"已添加并开始扫描文件夹: {folder}"

    def _ensure_bottom_shown(self) -> None:
        """显示底部面板；首次加载时展开窗口并锁定最小宽度（小屏按比例缩放）。"""
        self.bottom_frame.pack(fill="both", expand=True, pady=(6, 0))
        if not self._first_scan_done:
            self._first_scan_done = True
            self.root.geometry(f"{self._px(1280)}x{self._px(660)}")
            self.root.minsize(self._px(1280), self._px(660))

    def _select_folder_by_index(self, index: int) -> None:
        """切换当前选中文件夹：把其缓存结果挂到 audio_items 并刷新右侧列表。"""
        if index < 0 or index >= len(self.folder_tabs):
            return
        self.active_folder_index = index
        tab = self.folder_tabs[index]
        self.audio_root = tab.get("path")
        self.audio_items = tab.get("items") or []
        self._folder_title = tab.get("title")
        self._refresh_folder_list()
        self._update_songlist_title()

        # 若正在播放的歌在当前文件夹里：定位高亮；否则清空列表选中态
        playing_path = self.audio_path
        found = None
        if playing_path:
            for i, it in enumerate(self.audio_items):
                if it.get("path") == playing_path:
                    found = i
                    break
        if found is not None:
            self.current_song_index = found
            self.viewed_song_index = found
            self._refresh_song_list()
            self.song_list.selection_set(found)
            self.song_list.see(found)
            self._show_song_info_for_index(found)
        else:
            self.current_song_index = None
            self.viewed_song_index = None
            self.song_list.selection_clear(0, "end")
            self._clear_song_info()
            self._refresh_song_list()
        # 缓存结果中若有未读取时长的项，补起后台时长缓存
        if self.audio_items and any(
                it.get("duration") is None for it in self.audio_items):
            self._start_bg_cache()
        self._update_controls_state()
        self._refresh_status_bar()

    def _remove_folder_by_index(self, index: int) -> None:
        """移除文件夹（仅移除列表条目；若正在播放其中的歌曲则保留播放）。"""
        if index < 0 or index >= len(self.folder_tabs):
            return
        was_active = (self.active_folder_index == index)
        removed = self.folder_tabs.pop(index)
        path = removed.get("path")
        if path:  # 中止该文件夹进行中的扫描
            self._folder_scan_gen.pop(os.path.normcase(path), None)
            self._folder_scan_gen.pop(path, None)
            self._scanning_paths.discard(
                os.path.normcase(os.path.abspath(path)))

        if self.active_folder_index is not None:
            if self.active_folder_index == index:
                self.active_folder_index = None
            elif self.active_folder_index > index:
                self.active_folder_index -= 1

        self._refresh_folder_list()
        self._schedule_config_save()

        if not was_active:
            return
        # 删除的是当前选中文件夹：切到相邻文件夹，或全部清空（播放仍保留）
        if self.folder_tabs:
            self._select_folder_by_index(min(index, len(self.folder_tabs) - 1))
        else:
            self.active_folder_index = None
            self.audio_root = None
            self.audio_items = []
            self.current_song_index = None
            self.viewed_song_index = None
            self.song_list.selection_clear(0, "end")
            self._clear_song_info()
            self._refresh_song_list()
            self._update_songlist_title()
            self._update_controls_state()
            self._refresh_status_bar()

    # ==================================================================
    # 右侧栏：封面 + 插播列表 + 按钮区
    # ==================================================================

    def _build_right_column(self, parent: tk.Frame) -> None:
        """构建右侧栏：专辑封面、插播列表（可伸展）、按钮区（底部固定）。"""
        btn_cfg = dict(
            bg=SUBTLE_COLOR, fg=FG_COLOR, font=self.button_font,
            activebackground=ACCENT_COLOR, activeforeground=BG_COLOR,
            relief="flat", padx=6, pady=4,
        )

        # ---- 按钮区 A（状态感知：1行全宽 + 2列等宽固定）----
        btn_frame = tk.Frame(parent, bg=BG_COLOR)
        btn_frame.pack(side="bottom", fill="x")

        self._btn_a = tk.Button(
            btn_frame, text="播放此首", command=self._play_viewed_song, **btn_cfg)
        self._btn_a.pack(fill="x", pady=1)

        row = tk.Frame(btn_frame, bg=BG_COLOR)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        self._btn_b = tk.Button(
            row, text="添加到插播", command=self._interlude_add,
            width=7, **btn_cfg)
        self._btn_b.grid(row=0, column=0, sticky="ew", padx=(0, 2), pady=1)

        self._btn_c = tk.Button(
            row, text="清除插播", command=self._il_clear_confirm,
            width=7, **btn_cfg)
        self._btn_c.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=1)

        # ---- 插播列表（可伸展，填充剩余空间）----
        il_frame = tk.Frame(parent, bg=BG_COLOR)
        il_frame.pack(side="bottom", fill="both", expand=True, pady=(0, 6))

        self._il_label = tk.Label(
            il_frame, text="插播列表", bg=BG_COLOR, fg=FG_COLOR,
            font=self.list_font, anchor="w")
        self._il_label.pack(anchor="w")

        self.interlude_list = tk.Listbox(
            il_frame,
            bg=BG_COLOR, fg=FG_COLOR, font=self.list_font,
            selectbackground=ACCENT_COLOR, selectforeground=BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False,
        )
        self.interlude_list.pack(side="left", fill="both", expand=True)
        self.interlude_list.bind("<FocusOut>", self._on_interlude_focus_out)

        # 插播列表选择变化时刷新按钮，移动模式下锁定选中项
        self.interlude_list.bind("<<ListboxSelect>>",
                                lambda e: self._on_interlude_select())

        # ---- 专辑封面（固定 220×220，与图片尺寸一致，避免灰边；小屏缩放）----
        cover_frame = tk.Frame(parent, bg=BG_COLOR,
                               height=self._px(220), width=self._px(220))
        cover_frame.pack(side="bottom", fill="x", pady=(0, 8))
        cover_frame.pack_propagate(False)
        self.cover_label = tk.Label(
            cover_frame, bg=BG_COLOR, fg=FG_COLOR,
            text="专辑封面", font=self.list_font,
        )
        self.cover_label.pack(fill="both", expand=True)
        # 点击封面打开全窗口歌词浮层
        self.cover_label.bind("<Button-1>", self._open_lyric_overlay)

    # ==================================================================
    # 操作区（倍速 / 音高 / 音量 / 高级功能 / 选项区）
    #   V1.7.7 提取至 operation_panel.py（OperationPanel，自包含滚动容器），
    #   实例见 _build_ui 中的 self.op
    # ==================================================================

    def _toggle_console(self) -> None:
        """打开/关闭控制台窗口。"""
        if getattr(self, "console", None) is not None:
            self.console.toggle()

    # ==================================================================
    # 专辑封面
    # ==================================================================

    def _update_cover(self, path: str | None) -> None:
        """根据当前播放的音频文件更新专辑封面。"""
        if not path:
            self._show_default_cover()
            self._refresh_overlay_header()
            return
        data = extract_cover_art(path)
        photo = cover_to_tk_image(data, self._px(220)) if data else None
        if photo:
            self._cover_photo = photo
            self.cover_label.config(image=photo, text="")
        else:
            self._show_default_cover()
        self._refresh_overlay_header()

    def _show_default_cover(self) -> None:
        """显示默认封面占位。"""
        self._cover_photo = None
        self.cover_label.config(image="", text="无封面")

    # ==================================================================
    # 插播列表操作
    # ==================================================================

    def _refresh_il_buttons(self) -> None:
        """根据插播列表选中状态和移动模式刷新按钮文字与命令。"""
        # 先重置清除按钮状态（模式切换时恢复）
        self._reset_clear_button()

        if self._il_move_mode:
            self._btn_a.config(text="确认", command=self._il_move_confirm)
            self._btn_b.config(text="上移", command=self._il_move_up)
            self._btn_c.config(text="下移", command=self._il_move_down)
            for b in (self._btn_a, self._btn_b, self._btn_c):
                b.config(state="normal")
            return

        sel = self.interlude_list.curselection()
        if sel:
            self._btn_a.config(text="插入到插播", command=self._interlude_insert)
            self._btn_b.config(text="从插播移除", command=self._interlude_remove)
            self._btn_c.config(text="移动位置", command=self._il_enter_move_mode)
        else:
            self._btn_a.config(text="播放此首", command=self._play_viewed_song)
            self._btn_b.config(text="添加到插播", command=self._interlude_add)
            self._btn_c.config(text="清除插播", command=self._il_clear_confirm)
        # 未加载任何文件/文件夹时禁用歌曲相关按钮，否则全部启用
        loaded = self.audio_path is not None or bool(self.audio_items)
        for b in (self._btn_a, self._btn_b, self._btn_c):
            b.config(state="normal" if loaded else "disabled")

    def _reset_clear_button(self) -> None:
        """将清除按钮恢复为默认状态。"""
        if self._clear_confirm_timer:
            self.root.after_cancel(self._clear_confirm_timer)
            self._clear_confirm_timer = None
        self._clear_confirm_active = False
        self._btn_c.config(text="清除插播", fg=FG_COLOR,
                          font=self.button_font, width=7,
                          command=self._il_clear_confirm)

    # ---- 插播基本操作 ----

    def _refresh_interlude_list(self) -> None:
        """刷新插播列表显示，并保持原滚动位置不变。"""
        try:
            yv_top, yv_bottom = self.interlude_list.yview()
        except tk.TclError:
            yv_top, yv_bottom = None, None

        self.interlude_list.delete(0, "end")
        for item in self.interlude_items:
            name = os.path.basename(item.get("path") or "")
            self.interlude_list.insert("end", name)

        # 恢复滚动位置（仅当之前有内容且当前列表非空）
        if yv_top is not None and self.interlude_items:
            self.interlude_list.yview_moveto(yv_top)

        self._update_interlude_label()
        self._refresh_status_bar()

    def _update_interlude_label(self) -> None:
        """更新插播列表标题：有歌曲时追加播完所需总时长。"""
        label = getattr(self, "_il_label", None)
        if label is None:
            return
        if not self.interlude_items:
            label.config(text="插播列表")
            return
        total = sum(
            d for d in (item.get("duration") for item in self.interlude_items)
            if isinstance(d, (int, float))
        )
        label.config(text=f"插播列表 ({format_hms(total)})")

    def _on_interlude_select(self) -> None:
        """插播列表选择变化：移动模式下锁定选中项，否则刷新按钮。"""
        if self._il_move_mode and self._move_locked_index is not None:
            # 移动模式下强制选中锁定项，禁止点击其他歌曲
            self.interlude_list.selection_clear(0, "end")
            self.interlude_list.selection_set(self._move_locked_index)
        self._refresh_il_buttons()

    def _on_interlude_focus_out(self, _event: tk.Event) -> None:
        """插播列表失焦后清空选择（移动模式下不处理）。"""
        if self._il_move_mode:
            return
        self.interlude_list.selection_clear(0, "end")
        self._refresh_il_buttons()

    def _interlude_add(self) -> None:
        """将当前查看的歌曲添加到插播列表末尾。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        item = dict(self.audio_items[self.viewed_song_index])
        self.interlude_items.append(item)
        self._refresh_interlude_list()

    def _interlude_insert(self) -> None:
        """将当前查看的歌曲插入到插播列表选中位置之前。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        sel = self.interlude_list.curselection()
        pos = sel[0] if sel else len(self.interlude_items)
        item = dict(self.audio_items[self.viewed_song_index])
        self.interlude_items.insert(pos, item)
        self._refresh_interlude_list()
        self._refresh_il_buttons()

    def _interlude_remove(self) -> None:
        """移除插播列表中选中的条目（无确认弹窗）。"""
        sel = self.interlude_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.interlude_items):
            del self.interlude_items[idx]
            self._refresh_interlude_list()
            self._refresh_il_buttons()

    # ---- 清除插播队列（二次确认）----

    def _il_clear_confirm(self) -> None:
        """清除插播队列：点击→亮红确认，再点击→清除，3秒自动恢复。"""
        if self._clear_confirm_active:
            self.interlude_items.clear()
            self._refresh_interlude_list()
            self._reset_clear_button()
            self._refresh_il_buttons()
            return
        self._clear_confirm_active = True
        self._btn_c.config(text="确定？再点", fg="#FF4444",
                          font=self.button_font, width=7,
                          command=self._il_clear_confirm)
        self._clear_confirm_timer = self.root.after(
            3000, self._reset_clear_button)

    # ---- 移动模式 ----

    def _il_enter_move_mode(self) -> None:
        """进入移动模式：锁定当前选中项，禁用失焦清除。"""
        sel = self.interlude_list.curselection()
        if not sel:
            return
        self._il_move_mode = True
        self._move_locked_index = sel[0]
        self._reset_clear_button()
        self._refresh_il_buttons()

    def _il_move_confirm(self) -> None:
        """确认移动，退出移动模式，恢复失焦清除。"""
        self._il_move_mode = False
        self._move_locked_index = None
        self.interlude_list.selection_clear(0, "end")
        self._refresh_il_buttons()

    def _il_move_up(self) -> None:
        """移动模式下将锁定项上移。"""
        if self._move_locked_index is None or self._move_locked_index <= 0:
            return
        items = self.interlude_items
        idx = self._move_locked_index
        items[idx], items[idx - 1] = items[idx - 1], items[idx]
        self._move_locked_index = idx - 1
        self._refresh_interlude_list()
        self.interlude_list.selection_set(self._move_locked_index)
        self._ensure_interlude_visible(self._move_locked_index)

    def _il_move_down(self) -> None:
        """移动模式下将锁定项下移。"""
        if (self._move_locked_index is None
                or self._move_locked_index >= len(self.interlude_items) - 1):
            return
        items = self.interlude_items
        idx = self._move_locked_index
        items[idx], items[idx + 1] = items[idx + 1], items[idx]
        self._move_locked_index = idx + 1
        self._refresh_interlude_list()
        self.interlude_list.selection_set(self._move_locked_index)
        self._ensure_interlude_visible(self._move_locked_index)

    def _ensure_interlude_visible(self, index: int) -> None:
        """仅当目标项不可见或部分可见时，以最小幅度滚动使其完全可见；已可见则不动。"""
        if not self.interlude_items or index < 0 or index >= len(self.interlude_items):
            return

        lb = self.interlude_list
        try:
            list_height = lb.winfo_height()
            if list_height <= 0:
                return
            yv_top, yv_bottom = lb.yview()
        except tk.TclError:
            return

        visible_ratio = max(yv_bottom - yv_top, 0.0001)
        total_height = list_height / visible_ratio

        # 内容没有超出视口，无需滚动
        if total_height <= list_height:
            return

        # 尝试直接获取目标项的像素位置（相对 Listbox 窗口）
        try:
            bbox = lb.bbox(index)
        except tk.TclError:
            bbox = None

        if bbox:
            _x, item_top, _w, item_h = bbox
            item_bottom = item_top + item_h
        else:
            # bbox 不可用：目标项完全不可见，按均匀行高估算
            row_height = total_height / len(self.interlude_items)
            item_top = index * row_height - yv_top * total_height
            item_bottom = item_top + row_height

        # 已经完整可见，不滚动
        if item_top >= 0 and item_bottom <= list_height:
            return

        # 计算需要滚动的比例（正数向下，负数向上）
        if item_top < 0:
            # 目标项顶部被遮住：向上滚动使其顶部贴住视口顶部
            delta_fraction = item_top / total_height
        else:
            # 目标项底部超出：向下滚动使其底部贴住视口底部
            delta_fraction = (item_bottom - list_height) / total_height

        new_top_fraction = yv_top + delta_fraction

        # 钳制在合法范围
        new_top_fraction = max(0.0, min(1.0 - visible_ratio, new_top_fraction))

        # 忽略极小的浮点变化
        if abs(new_top_fraction - yv_top) < 0.0001:
            return

        try:
            lb.yview_moveto(new_top_fraction)
        except tk.TclError:
            pass

    # ==================================================================
    # 音频控件状态
    # ==================================================================

    def _disable_audio_controls(self) -> None:
        """音频后端不可用时禁用相关按钮。"""
        for btn in (self.play_pause_btn, self.stop_btn):
            btn.config(state="disabled")

    def _disable_op_controls(self) -> None:
        """非 sounddevice 后端时禁用倍速/保音高/音高/平衡/增益控件（音量保留）。"""
        for w in (self.op.speed_scale, self.op.pitch_btn, self.op.pitch_scale,
                  self.op.balance_scale, self.op.gain_scale):
            try:
                w.config(state="disabled")
            except Exception:
                pass

    def _update_controls_state(self) -> None:
        """根据是否已加载文件/文件夹统一启用/禁用播放相关控件。

        加载任何文件或扫描任何文件夹后，除 sounddevice 专属控件（pygame 后端禁用）外全部启用；
        完全未加载时（刚启动）全部禁用。
        """
        loaded = self.audio_path is not None or bool(self.audio_items)
        state = "normal" if loaded else "disabled"

        for w in (self.play_pause_btn, self.stop_btn,
                  self.back_10s_btn, self.forward_10s_btn,
                  self.prev_btn, self.next_btn,
                  self.seek_scale):
            w.config(state=state)

        # 倍速/保音高/音高/平衡/增益：仅 sounddevice 后端可用
        op_widgets = (self.op.speed_scale, self.op.pitch_btn,
                      self.op.pitch_scale, self.op.balance_scale,
                      self.op.gain_scale)
        if self.engine.backend == "sounddevice":
            for w in op_widgets:
                try:
                    w.config(state=state)
                except Exception:
                    pass
        else:
            for w in op_widgets:
                try:
                    w.config(state="disabled")
                except Exception:
                    pass

        # 右栏按钮状态由 _refresh_il_buttons 维护
        self._refresh_il_buttons()

    # ==================================================================
    # 状态栏更新
    # ==================================================================

    def _read_audio_info(self) -> str:
        """读取当前音频输出设备名称/参数信息。"""
        if not self.engine.ready:
            return "音频: 未就绪"
        if self.engine.backend == "sounddevice":
            try:
                import sounddevice as sd
                dev = sd.query_devices(kind="output")
                name = str(dev.get("name", "")).strip()
                if name:
                    return f"音频: {name}"
                sr = int(dev.get("default_samplerate", 0) or 0)
                ch = int(dev.get("max_output_channels", 2) or 2)
                ch_name = {1: "Mono", 2: "Stereo"}.get(ch, f"{ch}ch")
                return f"音频: {sr}Hz {ch_name}" if sr > 0 else "音频: 就绪"
            except Exception:
                return "音频: 就绪"
        try:
            import pygame
            info = pygame.mixer.get_init()
            if info:
                freq, fmt, ch = info
                ch_name = {1: "Mono", 2: "Stereo"}.get(ch, f"{ch}ch")
                bits = 16 if fmt < 0 else 8
                return f"音频: pygame {freq}Hz {bits}bit {ch_name}"
        except Exception:
            pass
        return "音频: 就绪"

    def _refresh_status_bar(self) -> None:
        """刷新状态栏所有字段（主线程安全）。"""
        # 状态栏可能在 UI 构建中途被调用（_refresh_song_list），此时控件尚未创建
        if not hasattr(self, "_liststat_var") or not hasattr(self, "_ilst_var"):
            return
        # 歌曲统计（仅当前选中文件夹）
        if self.audio_items:
            total = len(self.audio_items)
            cur = (self.current_song_index or 0) + 1 if self.current_song_index is not None else 0
            self._liststat_var.set(f"共{total}首" + (f" 第{cur}首" if cur else ""))
        else:
            self._liststat_var.set("共 0 首")

        # 插播统计
        n = len(self.interlude_items)
        self._ilst_var.set(f"插播: {n}首" if n else "插播: 0")

    def _set_scan_progress(self, done: int, total: int) -> None:
        """更新扫描进度（主线程安全）。"""
        pct = done / total * 100 if total > 0 else 0
        bar = make_progress_bar(pct)
        self._prog_var.set(f"解析中 {bar}")

    def _set_status_ready(self) -> None:
        """设置状态为就绪。"""
        self._prog_var.set("就绪")
        self._refresh_status_bar()

    def _on_scan_finished(self, folder: str) -> None:
        """主线程：登记某文件夹扫描结束；全部扫描结束则置状态为就绪。"""
        if folder:
            self._scanning_paths.discard(
                os.path.normcase(os.path.abspath(folder)))
        if not self._scanning_paths:
            self._prog_var.set("就绪")
            self._refresh_status_bar()

    # ==================================================================
    # 歌曲列表
    # ==================================================================

    def _scan_folder(self) -> None:
        """GUI：「打开文件夹」→ 追加到多文件夹列表并扫描（同路径仅切换缓存）。"""
        folder = filedialog.askdirectory(title="选择歌曲文件夹")
        if not folder:
            return
        self._add_folder(folder)

    def _start_folder_scan(self, index: int) -> None:
        """启动 folder_tabs[index] 的后台扫描。

        per-folder 世代（self._folder_scan_gen[path]），允许多个文件夹并发扫描；
        文件夹被删除时其世代条目移除，进行中的扫描自动退出。
        """
        if index < 0 or index >= len(self.folder_tabs):
            return
        tab = self.folder_tabs[index]
        folder = tab.get("path")
        if not folder:
            return
        tab["status"] = "loading"
        gen = self._folder_scan_gen.get(folder, 0) + 1
        self._folder_scan_gen[folder] = gen
        self._scanning_paths.add(os.path.normcase(os.path.abspath(folder)))

        # 若该文件夹是当前选中：右侧歌曲列表显示扫描占位
        if index == self.active_folder_index:
            self.audio_items = []
            self.current_song_index = None
            self.viewed_song_index = None
            self.song_list.selection_clear(0, "end")
            self.song_list.delete(0, "end")
            self.song_list.insert("end", "正在扫描文件夹...")
            self._clear_song_info()
            self._update_controls_state()
            self._prog_var.set(
                f"正在扫描文件夹... {os.path.basename(folder)}")

        threading.Thread(
            target=self._scan_thread, args=(folder, gen), daemon=True
        ).start()

    def _scan_thread(self, folder: str, generation: int) -> None:
        """后台扫描线程包装：无论正常/过期退出，都在主线程登记该文件夹扫描结束。"""
        try:
            self._scan_thread_impl(folder, generation)
        finally:
            try:
                self.root.after(
                    0, lambda f=folder: self._on_scan_finished(f))
            except Exception:
                pass

    def _scan_thread_impl(self, folder: str, generation: int) -> None:
        """后台扫描线程：先枚举音频路径，再读元数据；per-folder 世代过期则退出。"""
        # 阶段一：枚举音频文件路径
        audio_paths: list[str] = []
        try:
            for root_dir, _, files in os.walk(folder):
                if self._folder_scan_gen.get(folder) != generation:
                    return
                for name in files:
                    if self._folder_scan_gen.get(folder) != generation:
                        return
                    if os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                        continue
                    audio_paths.append(os.path.join(root_dir, name))
                    if len(audio_paths) % 50 == 0:
                        n = len(audio_paths)
                        self.root.after(
                            0, lambda c=n: self._prog_var.set(f"正在扫描... {c} 个"))
        except Exception:
            pass
        if self._folder_scan_gen.get(folder) != generation:
            return
        total = len(audio_paths)

        # 阶段二：读元数据 + 匹配 LRC，实时进度
        items: list[dict[str, str | int | None]] = []
        for i, path in enumerate(audio_paths):
            if self._folder_scan_gen.get(folder) != generation:
                return
            lrc_path = self._find_lrc_for_audio(path)
            track_number, album_name, artist_name = self._get_metadata(path)
            rel = os.path.relpath(path, folder)
            items.append({
                "path": path,
                "lrc": lrc_path,
                "display": rel,
                "track": track_number,
                "album": album_name,
                "artist": artist_name,
                "duration": None,
            })
            if (i + 1) % 5 == 0 or i + 1 == total:
                done, tot = i + 1, total
                self.root.after(
                    0, lambda d=done, t=tot: self._set_scan_progress(d, t))

        if self._folder_scan_gen.get(folder) != generation:
            return
        items.sort(key=self._song_sort_key)
        self.root.after(
            0, lambda: self._apply_folder_scan(folder, items, generation))

    def _apply_folder_scan(self, folder: str, items: list,
                           generation: int) -> None:
        """主线程：把某文件夹扫描结果写入对应 tab（缓存）；若为当前选中则刷新列表。"""
        if self._folder_scan_gen.get(folder) != generation:
            return
        # 按 path 定位 tab（删除后索引会变，不能直接用传入索引）
        key = os.path.normcase(os.path.abspath(folder))
        tab_index = None
        for i, tab in enumerate(self.folder_tabs):
            if os.path.normcase(os.path.abspath(
                    tab.get("path") or "")) == key:
                tab_index = i
                break
        if tab_index is None:
            return
        tab = self.folder_tabs[tab_index]
        tab["items"] = items
        tab["status"] = "ready"

        if tab_index == self.active_folder_index:
            self.audio_items = items
            self.audio_root = folder
            self.current_song_index = None
            self.viewed_song_index = None
            self._refresh_song_list()
            self._update_controls_state()
            self._start_bg_cache()
        self._refresh_folder_list()

    def _start_bg_cache(self) -> None:
        """启动后台时长缓存（针对当前 audio_items，静默后台，不占用主状态条）。

        每次调用 _bg_cache_gen 自增，切换文件夹后旧缓存线程自动退出。
        """
        self._scan_total = len(self.audio_items)
        self._scan_done = 0
        self._bg_cache_gen += 1
        gen = self._bg_cache_gen
        threading.Thread(
            target=self._bg_cache_thread, args=(gen,), daemon=True
        ).start()

    def _bg_cache_thread(self, generation: int) -> None:
        """后台子线程：用 mutagen 获取时长，不触碰 pygame 避免音频撕裂。"""
        items_snapshot = self.audio_items
        total = len(items_snapshot)
        for i, item in enumerate(items_snapshot):
            if generation != self._bg_cache_gen:
                return
            path = item.get("path")
            if path:
                dur = get_duration_mutagen(path)
                item["duration"] = dur
            self.root.after(0, lambda idx=i, d=item.get("duration"):
                           self._on_duration_cached(idx, d))
        # 时长缓存静默后台执行：不再占用主状态条（状态“就绪”由扫描完成统一置位）

    def _on_duration_cached(self, index: int, duration: float | None) -> None:
        """主线程回调：更新信息面板中的时长（若仍在查看该歌曲）。"""
        if self.viewed_song_index != index:
            return
        if "时长" in self.song_info_labels:
            self.song_info_labels["时长"].set(
                format_time(duration) if duration else "未知")

    @staticmethod
    def _song_sort_key(item: dict) -> tuple:
        """排序：目录层级 → 专辑名称 → 音轨号 → 文件名。"""
        display = item.get("display") or ""
        # 提取目录层级（根目录文件优先，子目录按名称排序）
        dir_path = os.path.dirname(display).replace("\\", "/")
        if dir_path in ("", "."):
            dir_parts = ()
        else:
            dir_parts = tuple(dir_path.split("/"))
        album = (item.get("album") or "").lower()
        track = item.get("track")
        track_key = track if isinstance(track, int) and track > 0 else 999999
        filename = os.path.basename(display).lower()
        return (dir_parts, album, track_key, filename)

    @staticmethod
    def _get_metadata(path: str) -> tuple[int | None, str, str]:
        """从音频元数据中读取音轨号（整数）、专辑名和歌手名。

        Returns:
            (track_number, album_name, artist_name) —— track_number 为 None 表示读取失败。
        """
        try:
            from mutagen import File as MutagenFile
        except Exception:
            return None, "", ""
        try:
            audio = MutagenFile(path, easy=True)
        except Exception:
            return None, "", ""
        if not audio or not audio.tags:
            return None, "", ""
        import re
        tags = {k.lower(): v for k, v in audio.tags.items()}
        # 读取专辑
        album = ""
        album_values = tags.get("album")
        if album_values:
            album = str(album_values[0]) if isinstance(album_values, (list, tuple)) else str(album_values)
        # 读取歌手
        artist = ""
        artist_values = tags.get("artist")
        if artist_values:
            artist = str(artist_values[0]) if isinstance(artist_values, (list, tuple)) else str(artist_values)
        # 读取音轨号
        for key in ("tracknumber", "track", "trck"):
            if key not in tags:
                continue
            values = tags.get(key)
            if not values:
                continue
            raw = str(values[0]) if isinstance(values, (list, tuple)) else str(values)
            match = re.search(r"\d+", raw)
            if match:
                try:
                    return int(match.group(0)), album, artist
                except ValueError:
                    return None, album, artist
        return None, album, artist

    def _refresh_song_list(self) -> None:
        """刷新 Listbox 中的歌曲显示（空态文案按当前文件夹状态区分）。"""
        self.song_list.delete(0, "end")
        if not self.audio_items:
            if (self.active_folder_index is not None
                    and 0 <= self.active_folder_index < len(self.folder_tabs)
                    and self.folder_tabs[self.active_folder_index].get("status")
                    == "loading"):
                self.song_list.insert("end", "正在扫描文件夹...")
            elif self.folder_tabs:
                self.song_list.insert("end", "文件夹内暂无歌曲")
            else:
                self.song_list.insert("end", "尚未加载文件夹")
            self._refresh_status_bar()
            return
        for item in self.audio_items:
            self.song_list.insert("end", item.get("display") or "")
        self._refresh_status_bar()

    # ==================================================================
    # 歌曲列表交互
    # ==================================================================

    def _on_song_select(self, _event: tk.Event) -> None:
        """单击歌曲列表项 → 仅更新右侧歌曲信息面板，不切歌。"""
        if not self.audio_items:
            return
        selection = self.song_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.audio_items):
            return
        self.viewed_song_index = index
        self._show_song_info_for_index(index)
        self._refresh_il_buttons()

    # ==================================================================
    # 歌曲信息面板
    # ==================================================================

    def _show_song_info_for_index(self, index: int) -> None:
        """点击歌曲 → 立即显示"正在加载…"，子线程加载信息，支持取消。"""
        if index < 0 or index >= len(self.audio_items):
            self._clear_song_info()
            return

        # 立即标记"正在加载"
        for var in self.song_info_labels.values():
            var.set("正在加载...")

        # 取消上一个未完成的加载请求
        self._info_req_id += 1
        req_id = self._info_req_id

        # 捕获当前快照传给子线程
        item = self.audio_items[index]
        threading.Thread(
            target=self._load_info_thread,
            args=(index, req_id, item),
            daemon=True,
        ).start()

    def _load_info_thread(self, index: int, request_id: int,
                          item: dict) -> None:
        """子线程：收集歌曲信息，完成后投递到主线程。"""
        path = item.get("path") or ""

        info: dict[str, str] = {}
        info["文件名"] = os.path.basename(path)

        # 时长（已缓存直接用，否则显示"计算中"）
        dur = item.get("duration")
        info["时长"] = format_time(dur) if dur is not None else "计算中..."

        # 格式
        _, ext = os.path.splitext(path)
        info["格式"] = ext.upper() if ext else "未知"

        # 文件大小
        try:
            size = os.path.getsize(path)
            if size < 1024:
                info["文件大小"] = f"{size} B"
            elif size < 1048576:
                info["文件大小"] = f"{size / 1024:.1f} KB"
            else:
                info["文件大小"] = f"{size / 1048576:.1f} MB"
        except OSError:
            info["文件大小"] = "未知"

        # 专辑
        album = item.get("album")
        info["专辑"] = album if album else "无"

        # 音轨号
        track = item.get("track")
        info["音轨号"] = str(track) if track is not None else "无"

        # 歌词
        lrc = item.get("lrc")
        if lrc and os.path.exists(lrc):
            info["歌词"] = f"已匹配"
        else:
            info["歌词"] = "未匹配"

        # 回到主线程应用结果
        self.root.after(0, lambda: self._apply_info(index, info, request_id))

    def _apply_info(self, index: int, info: dict[str, str],
                    request_id: int) -> None:
        """主线程：若请求未过期且用户仍在查看该歌曲，应用信息。"""
        if request_id != self._info_req_id:
            return  # 已被更新的请求取消
        if self.viewed_song_index != index:
            return  # 用户已切换到其他歌曲
        for field, var in self.song_info_labels.items():
            var.set(info.get(field, "-"))

    def _clear_song_info(self) -> None:
        """清空歌曲信息面板。"""
        self.viewed_song_index = None
        for var in self.song_info_labels.values():
            var.set("-")
        self._refresh_il_buttons()

    def _play_viewed_song(self) -> None:
        """播放当前在信息面板中查看的歌曲。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        self._load_track_by_index(self.viewed_song_index, autoplay=True)

    # ==================================================================
    # 打开文件 / 加载
    # ==================================================================

    def _open_file(self) -> None:
        """打开单个音频文件并自动匹配 LRC。"""
        if not self.engine.ready:
            return
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", "*.mp3 *.wav *.ogg *.flac"), ("所有文件", "*.*")],
        )
        if not path:
            return
        if not self._load_audio_file(path):
            messagebox.showerror("加载音频", "无法加载音频文件。")
            return
        self._auto_load_lrc(path)

    def _load_audio_file(self, path: str) -> bool:
        """加载音频文件到引擎。成功返回 True。"""
        if not self.engine.ready:
            return False
        success = self.engine.load(path)
        if not success:
            return False
        self.audio_path = path
        self._sync_song_list_selection(path)
        self.duration = self.engine.get_duration(path)
        self._configure_seek_range()
        self._reset_playback_state()
        self._update_info()
        self._update_cover(path)
        self._update_controls_state()
        return True

    def _sync_song_list_selection(self, path: str) -> None:
        """若打开的文件在歌曲列表中，同步选中状态。"""
        self.current_song_index = None
        self.song_list.selection_clear(0, "end")
        for index, item in enumerate(self.audio_items):
            if item.get("path") == path:
                self.current_song_index = index
                self.song_list.selection_set(index)
                self.song_list.see(index)
                break

    def _load_track_by_index(self, index: int, autoplay: bool = True) -> None:
        """按索引加载并播放歌曲列表中的曲目。"""
        if index < 0 or index >= len(self.audio_items):
            return
        path = self.audio_items[index].get("path")
        if not path:
            return
        self.current_song_index = index
        self.viewed_song_index = index
        self.song_list.selection_clear(0, "end")
        self.song_list.selection_set(index)
        self.song_list.see(index)
        self._load_audio_file(path)
        lrc_path = self.audio_items[index].get("lrc")
        if lrc_path and os.path.exists(lrc_path):
            self._load_lrc_file(lrc_path)
        else:
            self._apply_placeholder_lrc(path)
        self._refresh_status_bar()
        if autoplay:
            self._play()

    # ==================================================================
    # LRC 歌词
    # ==================================================================

    def _auto_load_lrc(self, audio_path: str) -> None:
        """自动查找并加载与音频同目录同名的 .lrc 文件；缺失时用占位歌词。"""
        candidate = self._find_lrc_for_audio(audio_path)
        if candidate:
            self._load_lrc_file(candidate)
        else:
            self._apply_placeholder_lrc(audio_path)

    def _apply_placeholder_lrc(self, audio_path: str) -> None:
        """无歌词文件时的内存占位歌词（[00:00.00]歌曲名），不写盘。"""
        name = os.path.splitext(os.path.basename(audio_path))[0] or "未命名"
        self.lrc_lines = [(0.0, name)]
        self.lrc_times = [0.0]
        self.current_lrc_index = -1
        self.lrc_path = None          # 无真实歌词文件
        self._refresh_lyric_display()
        self._update_info()

    @staticmethod
    def _find_lrc_for_audio(audio_path: str) -> str | None:
        """查找与音频文件同名的 .lrc 文件。"""
        base, _ = os.path.splitext(audio_path)
        candidate = f"{base}.lrc"
        return candidate if os.path.exists(candidate) else None

    def _load_lrc_file(self, path: str) -> None:
        """加载并解析 LRC 歌词文件。"""
        content = read_text_file(path)
        if content is None:
            messagebox.showerror("加载歌词", "无法读取 LRC 文件。")
            self._clear_lrc()
            return
        self.lrc_lines = LrcParser.parse(content)
        self.lrc_times = [ts for ts, _ in self.lrc_lines]
        self.current_lrc_index = -1
        self.lrc_path = path
        self._refresh_lyric_display()
        self._update_info()

    def _clear_lrc(self) -> None:
        """清除当前歌词数据。"""
        self.lrc_lines.clear()
        self.lrc_times.clear()
        self.current_lrc_index = -1
        self.lrc_path = None
        self._refresh_lyric_display()
        self._update_info()

    def _refresh_lyric_display(self) -> None:
        """刷新歌词显示区（同时刷新桌面歌词条）。"""
        if not self.lrc_lines:
            self.now_line_var.set(
                "未加载歌词" if self.lrc_path is None else "未找到时间轴歌词")
            self._update_lyric_overlay()
            self._update_lyric_bar()
            return
        self.now_line_var.set(self.lrc_lines[0][1])
        self._update_lyric_overlay()
        self._update_lyric_bar()

    # ==================================================================
    # 信息行
    # ==================================================================

    def _update_info(self) -> None:
        """更新「音频: xxx | 歌词: xxx/xxx」信息行。"""
        audio_name = os.path.basename(self.audio_path) if self.audio_path else "未选择"
        current_lyric_count = (self.current_lrc_index + 1) if self.lrc_lines else "---"
        total_lyric_count = len(self.lrc_lines) if self.lrc_lines else "---"
        self.info_var.set(f"音频: {audio_name} | 歌词: {current_lyric_count} / {total_lyric_count}")

    # ==================================================================
    # 进度条
    # ==================================================================

    def _configure_seek_range(self) -> None:
        """根据时长设置进度条的取值范围。"""
        if self.duration and self.duration > 0:
            self.seek_scale.config(from_=0.0, to=self.duration, state="normal")
        else:
            self.seek_scale.config(from_=0.0, to=100.0, state="disabled")
        self.seek_var.set(0.0)

    def _on_seek_press(self, _event: tk.Event) -> None:
        """进度条拖动开始：覆盖取消条到按钮区上方。"""
        if not self.engine.ready or not self.audio_path or self.duration is None:
            return
        self.user_seeking = True
        self._cancel_frame.place(in_=self._btn_container,
                                 relx=0, rely=0, relwidth=1, relheight=1)

    def _on_seek_release(self, _event: tk.Event) -> None:
        """进度条拖动结束：在取消条上则不跳转（继续当前播放），否则跳转。"""
        if not self.engine.ready or not self.audio_path or self.duration is None:
            return
        target = float(self.seek_var.get())
        self.user_seeking = False
        self._cancel_frame.place_forget()
        if not self._is_pointer_in_cancel():
            self._seek_to(target)

    def _is_pointer_in_cancel(self,
                              cancel_widget: tk.Widget | None = None) -> bool:
        """判断当前鼠标指针是否在取消条范围内。"""
        widget = cancel_widget or self._cancel_frame
        try:
            x = self.root.winfo_pointerx() - widget.winfo_rootx()
            y = self.root.winfo_pointery() - widget.winfo_rooty()
            return (0 <= x <= widget.winfo_width()
                    and 0 <= y <= widget.winfo_height())
        except Exception:
            return False

    def _on_seek_changed(self, value: str) -> None:
        """进度条拖动中，实时更新时间与歌词预览。"""
        if not self.user_seeking:
            return
        try:
            target = float(value)
        except ValueError:
            return
        self.time_var.set(
            f"{format_time(target)} / {format_time(self.duration)}")
        self._sync_lyrics(target)

    # ==================================================================
    # 播放控制
    # ==================================================================

    def _play(self) -> None:
        """开始播放（内部方法）。"""
        if not self.engine.ready or not self.audio_path:
            return
        start_at = self.base_time if self.base_time > 0 else 0.0
        self.engine.play(start=start_at)
        self.is_playing = True
        self.is_paused = False
        self.play_started_at = time.monotonic() - start_at
        self.play_pause_btn.config(text="暂停")

    def _toggle_play_pause(self) -> None:
        """播放/暂停切换按钮回调。"""
        if not self.engine.ready or not self.audio_path:
            return
        if not self.is_playing:
            # 停止 → 播放
            self._play()
            self.play_pause_btn.config(text="暂停")
        elif self.is_paused:
            # 暂停 → 恢复
            self.engine.unpause()
            self.is_paused = False
            self.play_started_at = time.monotonic() - self.base_time
            self.play_pause_btn.config(text="暂停")
        else:
            # 播放中 → 暂停
            self.engine.pause()
            self.base_time = self._current_time()
            self.is_paused = True
            self.play_pause_btn.config(text="播放")

    def _stop(self) -> None:
        """停止播放。"""
        if not self.engine.ready:
            return
        self.engine.stop()
        self._reset_playback_state()

    def _seek_to(self, seconds: float) -> None:
        """跳转到指定秒数。"""
        if self.duration is None:
            return
        seconds = max(0.0, min(seconds, self.duration))
        self.base_time = seconds
        if self.is_playing or self.is_paused:
            self.engine.seek(seconds)
            if self.is_paused:
                self.engine.pause()
            self.play_started_at = time.monotonic() - seconds
            self.is_playing = True
        self.seek_var.set(seconds)
        self.time_var.set(
            f"{format_time(seconds)} / {format_time(self.duration)}")
        self._sync_lyrics(seconds)

    def _seek_back_10s(self) -> None:
        """后退 10 秒。"""
        if not self.audio_path or self.duration is None:
            return
        current = self._current_time()
        self._seek_to(max(0.0, current - 10.0))

    def _seek_forward_10s(self) -> None:
        """前进 10 秒。"""
        if not self.audio_path or self.duration is None:
            return
        current = self._current_time()
        self._seek_to(min(self.duration, current + 10.0))

    # ==================================================================
    # 上一曲 / 下一曲
    # ==================================================================

    def _prev_track(self) -> None:
        """切换到上一首歌曲。"""
        if not self.audio_items:
            return
        if self.current_song_index is None:
            idx = 0
        elif self.current_song_index > 0:
            idx = self.current_song_index - 1
        else:
            idx = len(self.audio_items) - 1
        self._load_track_by_index(idx, autoplay=True)

    def _next_track(self) -> None:
        """下一曲：插播优先，否则按播放模式切歌（随机模式下随机选曲）。"""
        if self.interlude_items:
            self._stop()
            self._play_next_interlude()
            return
        if not self.audio_items:
            return
        if self.play_mode == "shuffle":
            # 随机模式：与曲目自然结束的随机逻辑一致
            if self.current_song_index is None:
                idx = random.randrange(len(self.audio_items))
            elif len(self.audio_items) == 1:
                idx = 0
            else:
                choices = [
                    i for i in range(len(self.audio_items))
                    if i != self.current_song_index
                ]
                idx = random.choice(choices)
        elif self.current_song_index is None:
            idx = 0
        elif self.current_song_index < len(self.audio_items) - 1:
            idx = self.current_song_index + 1
        else:
            idx = 0
        self._load_track_by_index(idx, autoplay=True)

    # ==================================================================
    # 播放模式
    # ==================================================================

    def _set_play_mode(self, mode: str) -> None:
        """直接设置播放模式（选项区按钮 / 控制台 `set mode` 共用）。"""
        idx = next((i for i, (_, m) in enumerate(PLAY_MODES) if m == mode), 0)
        self.play_mode_index = idx
        self.play_mode = PLAY_MODES[idx][1]
        self.op.refresh_mode_buttons()
        self._schedule_config_save()

    # ==================================================================
    # 配置持久化（_internal/config/data.json）
    # ==================================================================

    def _collect_config(self) -> dict:
        """收集当前操作栏/播放模式/置顶配置。"""
        try:
            speed = round(float(self.op.speed_var.get()), 2)
        except Exception:
            speed = 1.0
        try:
            volume = int(round(float(self.op.vol_var.get())))
        except Exception:
            volume = 100
        try:
            pitch = int(round(float(self.op.pitch_var.get())))
        except Exception:
            pitch = 0
        return {
            "speed": speed,
            "pitch_fix": bool(self.engine.get_pitch_fix()),
            "pitch_shift": pitch,
            "volume": volume,
            "lrc_offset": round(float(self.lrc_offset or 0), 1),
            "balance": round(float(self.engine.get_balance()), 2),
            "gain": round(float(self.engine.get_gain()), 2),
            "play_mode": self.play_mode,
            "always_on_top": bool(self.always_on_top),
            "lyric_bar": self._lyric_bar_mode,
            "lyric_anchor": self._lyric_bar_anchor,
            "lyric_x": self._lyric_bar_x,
            "lyric_y": self._lyric_bar_y,
            "lyric_alpha": self._lyric_bar_alpha,
            "lyric_width": self._lyric_bar_width_pct,
            "lyric_font": self._lyric_bar_font_size,
        }

    def _collect_persist(self) -> dict:
        """收集完整持久化数据：配置 + 已加载文件夹 + 当前选中文件夹。"""
        data = {
            "config": self._collect_config(),
            "folders": [t.get("path") for t in self.folder_tabs],
            "active_folder": None,
        }
        if (self.active_folder_index is not None
                and 0 <= self.active_folder_index < len(self.folder_tabs)):
            data["active_folder"] = self.folder_tabs[
                self.active_folder_index].get("path")
        return data

    def _schedule_config_save(self) -> None:
        """改动即时保存（合并短时间内的多次改动；退出时 on_close 兜底）。"""
        if getattr(self, "_config_save_pending", False):
            return
        self._config_save_pending = True
        try:
            self.root.after(250, self._flush_config_save)
        except Exception:
            self._flush_config_save()

    def _flush_config_save(self) -> None:
        """把挂起的配置写入 data.json（幂等）。"""
        if not getattr(self, "_config_save_pending", False):
            return
        self._config_save_pending = False
        config_store.save(self._collect_persist())

    def _apply_config_values(self, cfg: dict) -> None:
        """把配置数值应用到引擎与 UI 控件（启动恢复 / 重置默认共用，不触发保存）。"""
        try:
            v = float(cfg.get("speed", 1.0))
            v = max(0.1, min(3.0, v))
        except Exception:
            v = 1.0
        self.engine.set_speed(v)

        pf = bool(cfg.get("pitch_fix", False))
        self.engine.set_pitch_fix(pf)

        try:
            ps = int(round(float(cfg.get("pitch_shift", 0))))
        except Exception:
            ps = 0
        ps = max(-12, min(12, ps))
        self.engine.set_pitch_shift(ps)

        try:
            vol = int(round(float(cfg.get("volume", 100))))
        except Exception:
            vol = 100
        vol = max(0, min(100, vol))
        self.engine.set_volume(vol / 100.0)

        try:
            lo = round(float(cfg.get("lrc_offset", 0)), 1)
        except Exception:
            lo = 0.0
        lo = max(-60.0, min(60.0, lo))
        self.lrc_offset = lo

        try:
            bal = round(float(cfg.get("balance", 0.0)), 2)
        except Exception:
            bal = 0.0
        bal = max(-1.0, min(1.0, bal))
        self.engine.set_balance(bal)

        try:
            g = round(float(cfg.get("gain", 1.0)), 2)
        except Exception:
            g = 1.0
        g = max(0.0, min(2.0, g))
        self.engine.set_gain(g)

        # 歌词条参数（锚点 / 位置 / 透明度 / 默认宽度 / 字体大小）
        an = str(cfg.get("lyric_anchor", "c"))
        if an not in LYRIC_ANCHOR_FRAC:
            an = "c"
        self._set_lyric_bar_anchor(an, persist=False)
        try:
            lx = int(round(float(cfg.get("lyric_x", 0))))
        except Exception:
            lx = 0
        try:
            ly = int(round(float(cfg.get("lyric_y", 0))))
        except Exception:
            ly = 0
        self._set_lyric_bar_x(lx, persist=False)
        self._set_lyric_bar_y(ly, persist=False)
        try:
            la = int(round(float(cfg.get("lyric_alpha", 85))))
        except Exception:
            la = 85
        la = max(30, min(100, la))
        try:
            lw = int(round(float(cfg.get("lyric_width", 25))))
        except Exception:
            lw = 25
        lw = max(10, min(100, lw))
        try:
            lf = int(round(float(cfg.get("lyric_font", 18))))
        except Exception:
            lf = 18
        lf = max(12, min(30, lf))
        self._set_lyric_bar_alpha(la, persist=False)
        self._set_lyric_bar_width(lw, persist=False)
        self._set_lyric_bar_font_size(lf, persist=False)
        self._finalize_lyric_bar_width()   # 宽度/字号恢复后重算 X/Y 范围

        # 控件显示同步（操作区面板）
        self.op.apply_values(speed=v, pitch_fix=pf, pitch_shift=ps,
                             volume=vol, lrc_offset=lo, balance=bal, gain=g,
                             lyric_alpha=la, lyric_width=lw, lyric_font=lf)

        mode = str(cfg.get("play_mode", "loop_all"))
        idx = next((i for i, (_, m) in enumerate(PLAY_MODES) if m == mode), 0)
        self.play_mode_index = idx
        self.play_mode = PLAY_MODES[idx][1]
        self.op.refresh_mode_buttons()

        # 置顶（直接设置，不走 _set_topmost 以免触发保存）
        top = bool(cfg.get("always_on_top", False))
        self.always_on_top = top
        self.root.attributes("-topmost", top)
        self.op.refresh_topmost_buttons()

        # 桌面歌词条模式（persist=False：不触发保存）
        self._set_lyric_bar_mode(str(cfg.get("lyric_bar", "off")),
                                  persist=False)

    def _load_persisted_state(self) -> None:
        """启动时读取 data.json：应用配置；计划恢复文件夹（后台自动扫描，跳过缺失）。"""
        data = config_store.load()
        self._apply_config_values(data.get("config") or {})
        self.root.after(0, self._restore_saved_folders)

    def _restore_saved_folders(self) -> None:
        """恢复上次打开的文件夹：逐个添加并后台扫描；不存在的跳过（不提示）。"""
        data = config_store.load()
        folders = data.get("folders") or []
        active = data.get("active_folder")
        added = False
        for fp in folders:
            if isinstance(fp, str) and os.path.isdir(fp):
                self._add_folder(fp)   # 内部已做存在性/重复检查
                added = True
        if not added:
            return
        # 恢复上次选中：优先 active_folder（若仍在列表），否则选中第一个
        if isinstance(active, str) and active:
            key = os.path.normcase(os.path.abspath(active))
            for i, tab in enumerate(self.folder_tabs):
                if os.path.normcase(os.path.abspath(
                        tab.get("path") or "")) == key:
                    self._select_folder_by_index(i)
                    return
        if self.folder_tabs:
            self._select_folder_by_index(0)

    # ==================================================================
    # 重置：清除配置 / 文件夹 / 播放与插播状态（文件不删除）
    # ==================================================================

    def _toggle_reset(self) -> None:
        """点「重置应用」：弹出全屏覆盖确认框（确认需点 2 次）。"""
        if getattr(self, "_reset_overlay", None) is not None:
            return
        overlay = tk.Frame(self.root, bg=BG_COLOR)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._reset_overlay = overlay
        mask = tk.Frame(overlay, bg="#14171C")
        mask.place(relx=0, rely=0, relwidth=1, relheight=1)
        mask.bind("<Button-1>", lambda e: self._close_reset_overlay())

        dlg = tk.Frame(overlay, bg=SUBTLE_COLOR, padx=24, pady=20,
                       highlightthickness=1, highlightbackground=ACCENT_COLOR)
        dlg.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(dlg, text="重置所有设置", bg=SUBTLE_COLOR, fg=ACCENT_COLOR,
                 font=self.title_font).pack(pady=(0, 12))
        tk.Label(dlg,
                 text="所有配置和打开的文件夹都会被清除\n"
                      "（文件不会被删除）\n\n是否继续？",
                 bg=SUBTLE_COLOR, fg=FG_COLOR, font=self.info_font,
                 justify="center").pack(pady=(0, 16))

        row = tk.Frame(dlg, bg=SUBTLE_COLOR)
        row.pack()
        self._reset_confirm_btn = tk.Button(
            row, text="重置", command=self._on_reset_confirm,
            bg=BG_COLOR, fg=FG_COLOR, font=self.button_font,
            activebackground=ACCENT_COLOR, activeforeground=BG_COLOR,
            relief="flat", padx=18, pady=5)
        self._reset_confirm_btn.pack(side="left", padx=8)
        tk.Button(row, text="取消", command=self._close_reset_overlay,
                  bg=BG_COLOR, fg=FG_COLOR, font=self.button_font,
                  activebackground=ACCENT_COLOR, activeforeground=BG_COLOR,
                  relief="flat", padx=18, pady=5).pack(side="left", padx=8)
        self._reset_confirm_timer = None
        self._reset_confirm_active = False

    def _on_reset_confirm(self) -> None:
        """确认按钮：需点 2 次（第二次变红后执行）。"""
        if self._reset_confirm_active:
            self._do_reset_all()
            self._close_reset_overlay()
            return
        self._reset_confirm_active = True
        self._reset_confirm_btn.config(text="确定？再点一次", fg="#FF5555")
        if self._reset_confirm_timer:
            self.root.after_cancel(self._reset_confirm_timer)
        self._reset_confirm_timer = self.root.after(
            3000, self._reset_reset_confirm)

    def _reset_reset_confirm(self) -> None:
        """3 秒未二次确认则恢复按钮。"""
        self._reset_confirm_active = False
        self._reset_confirm_timer = None
        if self._reset_confirm_btn is not None:
            self._reset_confirm_btn.config(text="重置", fg=FG_COLOR)

    def _close_reset_overlay(self) -> None:
        """关闭重置确认浮层。"""
        if self._reset_confirm_timer:
            try:
                self.root.after_cancel(self._reset_confirm_timer)
            except Exception:
                pass
            self._reset_confirm_timer = None
        self._reset_confirm_active = False
        ov = getattr(self, "_reset_overlay", None)
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass
            self._reset_overlay = None

    def _do_reset_all(self) -> None:
        """执行重置：清除配置、文件夹、当前播放/插播状态（不删除文件）。"""
        # 关闭歌词浮层
        if self._lyric_overlay is not None:
            try:
                self._close_lyric_overlay()
            except Exception:
                pass
        # 停止播放并清空当前播放上下文
        try:
            self.engine.stop()
        except Exception:
            pass
        self.audio_path = None
        self.lrc_path = None
        self.duration = None
        self.lrc_lines.clear()
        self.lrc_times.clear()
        self.current_lrc_index = -1
        self._reset_playback_state()
        self._refresh_lyric_display()
        self._clear_song_info()
        self._show_default_cover()
        # 清空插播
        self.interlude_items.clear()
        self._refresh_interlude_list()
        self._reset_clear_button()
        # 清空文件夹（中止扫描）
        self.folder_tabs.clear()
        self.active_folder_index = None
        self.audio_root = None
        self.audio_items = []
        self.current_song_index = None
        self.viewed_song_index = None
        self._folder_scan_gen.clear()
        self._refresh_folder_list()
        self._refresh_song_list()
        self._update_songlist_title()
        self._refresh_il_buttons()
        self._update_controls_state()
        # 配置恢复默认并持久化（文件夹已空）
        self._apply_default_config()
        config_store.save(self._collect_persist())
        self._refresh_status_bar()
        self._prog_var.set("已重置")

    def _apply_default_config(self) -> None:
        """把操作栏/模式/置顶恢复为默认值。"""
        self._apply_config_values(config_store.DEFAULTS.get("config") or {})

    # ==================================================================
    # 置顶
    # ==================================================================

    def _set_topmost(self, on: bool) -> None:
        """设置窗口置顶状态（选项区按钮 / 控制台共用入口）。

        全屏时 Windows 会自动置顶；这里把"用户主动改过置顶"记为标记，
        供退出全屏时决定是否保留（避免被还原成进入全屏前的旧状态）。
        """
        self.always_on_top = bool(on)
        self.root.attributes("-topmost", self.always_on_top)
        self.op.refresh_topmost_buttons()
        if bool(self.root.attributes("-fullscreen")):
            self._topmost_edited_in_fullscreen = True
        self._schedule_config_save()

    # ==================================================================
    # 播放状态
    # ==================================================================

    def _reset_playback_state(self) -> None:
        """重置播放状态到「停止」。"""
        self.is_playing = False
        self.is_paused = False
        self.base_time = 0.0
        self.play_started_at = None
        self.user_seeking = False
        self.seek_var.set(0.0)
        dur_str = format_time(self.duration)
        self.time_var.set(f"00:00 / {dur_str}")
        self._highlight_line(-1)
        self.play_pause_btn.config(text="播放")

    def _current_time(self) -> float:
        """根据播放状态计算当前播放位置（秒）。"""
        if not self.is_playing:
            return 0.0
        if self.is_paused:
            return self.base_time
        # sounddevice 后端：位置由引擎按倍速真实推进
        if self.engine.backend == "sounddevice":
            pos = self.engine.get_position()
            if pos is not None:
                return max(0.0, pos)
        if self.play_started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.play_started_at)

    # ==================================================================
    # 歌词同步
    # ==================================================================

    def _sync_lyrics(self, current_time: float) -> None:
        """根据当前播放时间更新高亮歌词行（考虑歌词偏移）。"""
        if not self.lrc_times:
            return
        lookup = current_time - self.lrc_offset / 100  # 正值偏移 → 歌词延后
        index = bisect_right(self.lrc_times, lookup) - 1
        if index != self.current_lrc_index:
            self.current_lrc_index = index
            self._highlight_line(index)
            self._update_info()

    def _highlight_line(self, index: int) -> None:
        """高亮指定索引的歌词行，并同步歌词浮层与桌面歌词条。"""
        if not self.lrc_lines:
            self.now_line_var.set(
                "未加载歌词" if self.lrc_path is None else "未找到时间轴歌词")
            self._update_lyric_overlay()
            self._update_lyric_bar()
            return
        if index < 0:
            self.now_line_var.set(self.lrc_lines[0][1])
            self._update_lyric_overlay()
            self._update_lyric_bar()
            return
        if index >= len(self.lrc_lines):
            self._update_lyric_overlay()
            self._update_lyric_bar()
            return
        self.now_line_var.set(self.lrc_lines[index][1])
        self._update_lyric_overlay()
        self._update_lyric_bar()

    # ==================================================================
    # 全窗口歌词浮层（点击封面打开）
    # ==================================================================

    def _open_lyric_overlay(self, _event: tk.Event | None = None) -> None:
        """点击专辑封面：打开全窗口歌词浮层。"""
        if self._lyric_overlay is not None or not self.audio_path:
            return
        self._build_lyric_overlay()
        self._refresh_overlay_header()
        self._update_lyric_overlay()

    def _build_lyric_overlay(self) -> None:
        """构建全窗口浮层：左栏放大封面+歌曲信息，右栏七行歌词。"""
        overlay = tk.Frame(self.root, bg=BG_COLOR)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        self._lyric_overlay = overlay

        # 左上角返回按钮（小正方形、与背景同色、FLAT）
        close_btn = tk.Button(
            overlay, text="↓", command=self._close_lyric_overlay,
            bg=BG_COLOR, fg=FG_COLOR, font=self.button_font_sm,
            activebackground=BG_COLOR, activeforeground=ACCENT_COLOR,
            relief="flat", bd=0, highlightthickness=0, padx=6, pady=3)
        close_btn.place(x=10, y=10)

        # 左右两栏定宽占比 9:11（uniform 保证严格比例，内容不会顶开布局）
        overlay.columnconfigure(0, weight=9, uniform="ovl")
        overlay.columnconfigure(1, weight=11, uniform="ovl")
        overlay.rowconfigure(0, weight=1)

        # ---- 左栏：内容垂直居中 ----
        left = tk.Frame(overlay, bg=BG_COLOR)
        left.grid(row=0, column=0, sticky="nsew")

        tk.Frame(left, bg=BG_COLOR).pack(expand=True)  # 顶部弹性（垂直居中）

        self._overlay_cover_label = tk.Label(
            left, bg=BG_COLOR, fg=FG_COLOR, text="专辑封面",
            font=self.list_font)
        self._overlay_cover_label.pack(anchor="s", pady=(0, 16))

        self._overlay_name_var = tk.StringVar(value="")
        self._overlay_name_label = tk.Label(
            left, textvariable=self._overlay_name_var, bg=BG_COLOR,
            fg=FG_COLOR, font=self.overlay_name_font,
            anchor="center", justify="center")
        self._overlay_name_label.pack()

        self._overlay_artist_var = tk.StringVar(value="")
        self._overlay_artist_label = tk.Label(
            left, textvariable=self._overlay_artist_var, bg=BG_COLOR,
            fg=ACCENT_COLOR, font=self.overlay_artist_font,
            anchor="center", justify="center")
        self._overlay_artist_label.pack(pady=(6, 0))

        # 初始换行宽度（不依赖 Configure 事件，保证首次显示即生效）
        init_wrap = max(80, int(self.root.winfo_width() * 9 / 20) - 60)
        self._overlay_name_label.config(wraplength=init_wrap)
        self._overlay_artist_label.config(wraplength=init_wrap)

        tk.Frame(left, bg=BG_COLOR).pack(expand=True)  # 底部弹性（垂直居中）

        # ---- 右栏：歌词（行数随窗口高度动态变化，中间为当前行大字）----
        right = tk.Frame(overlay, bg=BG_COLOR)
        right.grid(row=0, column=1, sticky="nsew")

        tk.Frame(right, bg=BG_COLOR).pack(expand=True)  # 顶部弹性空间
        self._overlay_lyric_frame = tk.Frame(right, bg=BG_COLOR)
        self._overlay_lyric_frame.pack(fill="x")
        tk.Frame(right, bg=BG_COLOR).pack(expand=True)  # 底部弹性空间

        self._overlay_lyric_wrap = max(
            80, int(self.root.winfo_width() * 11 / 20) - 48)
        self._overlay_lyric_vars = []
        self._overlay_lyric_labels = []
        self._overlay_row_count = 0
        self._overlay_rows_timer = None
        # 初次按当前窗口高度决定行数
        self._rebuild_overlay_lyric_rows(
            self._overlay_target_row_count(
                max(self.root.winfo_height(),
                    self.root.winfo_screenheight() // 2)))

        # 浮层尺寸变化时更新换行宽度并按高度调整行数（防抖）
        overlay.bind("<Configure>", self._on_overlay_configure)
        # 后创建的两栏 Frame 会盖住先创建的返回按钮，必须提升到最上层
        close_btn.lift()

    def _overlay_target_row_count(self, height: int) -> int:
        """按窗口高度估算可显示的歌词行数（奇数、中间为当前行、最少 3 行）。"""
        try:
            small_ls = int(self.overlay_lyric_small_font.metrics("linespace"))
            big_ls = int(self.overlay_lyric_big_font.metrics("linespace"))
        except Exception:
            small_ls, big_ls = 20, 34
        row_small = max(1, small_ls + 12)     # 小字行高 + 上下 pady(6+6)
        middle_row = max(1, big_ls + 20)      # 中间大字行高 + 上下 pady(10+10)
        avail = max(60, int(height) - 48)     # 上下留边距
        n = 1 + int(max(0, avail - middle_row) // row_small)
        if n % 2 == 0:
            n -= 1
        return max(3, n - 4)

    def _rebuild_overlay_lyric_rows(self, count: int) -> None:
        """重建浮层歌词行（行数变化时调用）：中间行大字，其余小字。"""
        frame = getattr(self, "_overlay_lyric_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self._overlay_lyric_vars = []
        self._overlay_lyric_labels = []
        self._overlay_row_count = count
        middle = count // 2
        wrap = getattr(self, "_overlay_lyric_wrap", 0) or max(
            80, int(self.root.winfo_width() * 11 / 20) - 48)
        for i in range(count):
            is_middle = (i == middle)
            var = tk.StringVar(value="")
            lbl = tk.Label(
                frame, textvariable=var, bg=BG_COLOR,
                fg=ACCENT_COLOR if is_middle else FG_COLOR,
                font=(self.overlay_lyric_big_font if is_middle
                      else self.overlay_lyric_small_font),
                anchor="w", justify="left", wraplength=wrap)
            lbl.pack(fill="x", padx=24,
                     pady=(10, 10) if is_middle else 6)
            self._overlay_lyric_vars.append(var)
            self._overlay_lyric_labels.append(lbl)

    def _on_overlay_configure(self, event: tk.Event) -> None:
        """浮层尺寸变化：更新换行宽度，并按高度防抖调整歌词行数。"""
        left_wrap = max(80, int(event.width * 9 / 20) - 60)
        lyric_wrap = max(80, int(event.width * 11 / 20) - 48)
        self._overlay_lyric_wrap = lyric_wrap
        if hasattr(self, "_overlay_name_label"):
            self._overlay_name_label.config(wraplength=left_wrap)
            self._overlay_artist_label.config(wraplength=left_wrap)
        for lbl in getattr(self, "_overlay_lyric_labels", []):
            lbl.config(wraplength=lyric_wrap)
        # 行数随高度变化：防抖 200ms 后重建，避免拖动窗口时频繁重建闪烁
        timer = getattr(self, "_overlay_rows_timer", None)
        if timer:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
        height = event.height
        self._overlay_rows_timer = self.root.after(
            200, lambda: self._apply_overlay_rows(height))

    def _apply_overlay_rows(self, height: int) -> None:
        """按高度计算目标行数；若变化则重建歌词行并刷新内容。"""
        self._overlay_rows_timer = None
        if self._lyric_overlay is None:
            return
        count = self._overlay_target_row_count(height)
        if count == getattr(self, "_overlay_row_count", 0):
            return
        self._rebuild_overlay_lyric_rows(count)
        self._update_lyric_overlay()

    def _close_lyric_overlay(self) -> None:
        """销毁歌词浮层并清理引用。"""
        if self._lyric_overlay is None:
            return
        timer = getattr(self, "_overlay_rows_timer", None)
        if timer:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
            self._overlay_rows_timer = None
        self._lyric_overlay.destroy()
        self._lyric_overlay = None
        self._overlay_cover_photo = None
        self._overlay_lyric_vars = []
        self._overlay_lyric_labels = []
        self._overlay_lyric_frame = None
        self._overlay_row_count = 0

    def _refresh_overlay_header(self) -> None:
        """刷新浮层左栏：放大封面、歌曲名、歌手名。"""
        if self._lyric_overlay is None:
            return
        path = self.audio_path
        if not path:
            return
        # 封面（按窗口高度与左栏宽度动态放大，避免溢出）
        left_w = int(self.root.winfo_width() * 9 / 20)
        size = max(120, min(420, self.root.winfo_height() - 260, left_w - 40))
        data = extract_cover_art(path)
        photo = cover_to_tk_image(data, size) if data else None
        if photo:
            self._overlay_cover_photo = photo
            self._overlay_cover_label.config(image=photo, text="")
        else:
            self._overlay_cover_photo = None
            self._overlay_cover_label.config(image="", text="无封面")
        # 歌曲名（去扩展名）与歌手名
        name = os.path.splitext(os.path.basename(path))[0]
        self._overlay_name_var.set(name)
        artist = self._get_current_artist()
        self._overlay_artist_var.set(artist if artist else "")

    def _get_current_artist(self) -> str:
        """获取当前播放曲目的歌手名（优先扫描缓存，否则现场读标签）。"""
        if not self.audio_path:
            return ""
        if (self.current_song_index is not None
                and self.current_song_index < len(self.audio_items)):
            item = self.audio_items[self.current_song_index]
            if item.get("path") == self.audio_path:
                artist = item.get("artist")
                if artist:
                    return artist
        try:
            _, _, artist = self._get_metadata(self.audio_path)
            return artist
        except Exception:
            return ""

    @staticmethod
    def _build_lyric_stem(lines: list[tuple[float, str]]) -> list[int]:
        """提取主干歌词行（剔除逐字展开的中间态）。

        若某行去除空白后是下一行的前缀，则为逐字展开的中间态，跳过；
        返回保留行的原始索引列表。
        """
        def _norm(text: str) -> str:
            return text.replace("\u3000", "").replace(" ", "").strip()

        stem: list[int] = []
        for i, (_, text) in enumerate(lines):
            cur = _norm(text)
            if i + 1 < len(lines):
                nxt = _norm(lines[i + 1][1])
                if cur and nxt.startswith(cur) and nxt != cur:
                    continue
            stem.append(i)
        return stem

    def _update_lyric_overlay(self) -> None:
        """刷新浮层歌词行：中间为当前逐字行，上下为该行前后主干歌词。"""
        vars_ = self._overlay_lyric_vars
        if self._lyric_overlay is None or not vars_:
            return
        middle = len(vars_) // 2
        for var in vars_:
            var.set("")
        if not self.lrc_lines:
            vars_[middle].set("未加载歌词" if self.lrc_path is None
                              else "未找到时间轴歌词")
            return

        # 当前行（-1 时按首行处理，与主窗口一致）
        cur = self.current_lrc_index if self.current_lrc_index >= 0 else 0
        stem_indices = self._build_lyric_stem(self.lrc_lines)

        # 定位中间行所属的主干分组
        g = bisect_right(stem_indices, cur) - 1
        if cur not in stem_indices:
            g += 1  # 逐字中间态属于下一个主干分组

        # 上下各 middle 行：主干完整歌词（去尾部占位空白）
        for offset in range(-middle, middle + 1):
            if offset == 0:
                continue
            idx = g + offset
            if 0 <= idx < len(stem_indices):
                text = self.lrc_lines[stem_indices[idx]][1].rstrip(" \u3000")
                vars_[middle + offset].set(text)

        # 中间行与现有解析一致（逐字原样）
        vars_[middle].set(self.lrc_lines[cur][1])

    # ==================================================================
    # 桌面歌词条（独立置顶小窗：关闭 / 顶部 / 底部）
    # ==================================================================

    def _set_lyric_bar_mode(self, mode: str, persist: bool = True) -> None:
        """切换桌面歌词条模式（off / top / bottom / custom）。

        persist=False 供启动恢复与重置复用（不触发保存）。
        """
        if mode not in ("off", "top", "bottom", "custom"):
            mode = "off"
        self._lyric_bar_mode = mode
        if mode == "off":
            self._destroy_lyric_bar()
        else:
            self._ensure_lyric_bar()
            self._update_lyric_bar()
        self.op.refresh_lyric_mode_buttons()
        self._sync_lyric_custom_rows()
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_alpha(self, pct: float, persist: bool = True) -> None:
        """歌词条窗口透明度（30~100%，默认 85）。"""
        v = int(round(max(30.0, min(100.0, float(pct)))))
        self._lyric_bar_alpha = v
        if self._lyric_bar is not None:
            try:
                self._lyric_bar.attributes("-alpha", v / 100.0)
            except tk.TclError:
                pass
        self._sync_lyric_opt_display("alpha", v)
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_width(self, pct: float, persist: bool = True) -> None:
        """歌词条初始宽度（占屏幕 10~100%，默认 25）。

        拖动中实时改变条宽；X/Y 范围与数值由拖动完成后的
        `_finalize_lyric_bar_width` 统一更新（避免逐帧重算卡顿）。
        """
        v = int(round(max(10.0, min(100.0, float(pct)))))
        self._lyric_bar_width_pct = v
        self._update_lyric_bar(rebuild_text=False)
        self._sync_lyric_opt_display("width", v)
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_font_size(self, px: float, persist: bool = True) -> None:
        """歌词条字体大小（12~30 px，默认 18；随小屏缩放）。

        拖动中实时生效；不重算 X/Y 范围（按约定仅在锚点/宽度变化时计算），
        也不重排控件行（避免拖动中闪烁）。
        """
        v = int(round(max(12.0, min(30.0, float(px)))))
        self._lyric_bar_font_size = v
        try:
            self.lyric_bar_font.configure(size=self._font_size(v))
        except tk.TclError:
            pass
        self._update_lyric_bar(rebuild_text=False)
        self._sync_lyric_opt_display("font", v)
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_anchor(self, anchor: str, persist: bool = True) -> None:
        """设置自定义锚点（九宫格）：X/Y 重置为 0，并重算滑块范围。"""
        if anchor not in LYRIC_ANCHOR_FRAC:
            anchor = "c"
        self._lyric_bar_anchor = anchor
        self._lyric_bar_x = 0
        self._lyric_bar_y = 0
        self._refresh_lyric_xy_range()
        self._position_lyric_bar()
        self._sync_lyric_custom_rows()
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_x(self, px: float, persist: bool = True) -> None:
        """自定义模式 X 偏移（px，相对锚点）。

        按缓存范围钳制后轻量移动歌词条（不重建文本/不重排行）；
        范围仅在切换锚点或宽度调整完成时更新。
        """
        xmin, xmax, _, _ = self._lyric_bar_xy_ranges()
        v = int(round(max(float(xmin), min(float(xmax), float(px)))))
        self._lyric_bar_x = v
        self._position_lyric_bar()
        if persist:
            self._schedule_config_save()

    def _set_lyric_bar_y(self, px: float, persist: bool = True) -> None:
        """自定义模式 Y 偏移（px，相对锚点）。

        按缓存范围钳制后轻量移动歌词条（不重建文本/不重排行）；
        范围仅在切换锚点或宽度调整完成时更新。
        """
        _, _, ymin, ymax = self._lyric_bar_xy_ranges()
        v = int(round(max(float(ymin), min(float(ymax), float(px)))))
        self._lyric_bar_y = v
        self._position_lyric_bar()
        if persist:
            self._schedule_config_save()

    def _refresh_lyric_xy_range(self) -> None:
        """重算并缓存 X/Y 可调范围（仅切换锚点或宽度调整完成时调用）。

        条宽只按「默认宽度」（屏幕宽 × 宽度%）计算，不考虑歌词变长等
        实际加宽因素；按约定不做溢出补偿（极端情况下条可部分超出屏幕）。
        """
        fx, fy = LYRIC_ANCHOR_FRAC.get(self._lyric_bar_anchor, (0.5, 0.5))
        w = int(self.screen_w * self._lyric_bar_width_pct / 100)
        try:
            h = self.lyric_bar_font.metrics("linespace") + self._px(18)
        except Exception:
            h = self._px(40)
        xpad = max(0, self.screen_w - w)
        ypad = max(0, self.screen_h - h)
        self._lyric_xy_range = (
            int(round(-fx * xpad)), int(round((1.0 - fx) * xpad)),
            int(round(-fy * ypad)), int(round((1.0 - fy) * ypad)))

    def _lyric_bar_xy_ranges(self) -> tuple[int, int, int, int]:
        """X/Y 可调范围（缓存值；未计算过时惰性计算一次）。"""
        if self._lyric_xy_range is None:
            self._refresh_lyric_xy_range()
        return self._lyric_xy_range or (0, 0, 0, 0)

    def _finalize_lyric_bar_width(self) -> None:
        """宽度调整完成：重算 X/Y 范围、钳制并刷新滑块范围与数值。"""
        self._refresh_lyric_xy_range()
        xmin, xmax, ymin, ymax = self._lyric_bar_xy_ranges()
        cx = max(xmin, min(xmax, self._lyric_bar_x))
        cy = max(ymin, min(ymax, self._lyric_bar_y))
        if cx != self._lyric_bar_x or cy != self._lyric_bar_y:
            self._lyric_bar_x = cx
            self._lyric_bar_y = cy
            self._position_lyric_bar()
        op = getattr(self, "op", None)
        if op is not None:
            try:
                op.refresh_lyric_xy_ui()
            except Exception:
                pass

    def _sync_lyric_custom_rows(self) -> None:
        """刷新「歌词选项」区行显隐与锚点/XY 显示（面板未创建时忽略）。"""
        op = getattr(self, "op", None)
        if op is None:
            return
        try:
            op.sync_lyric_custom_rows()
        except Exception:
            pass

    def _sync_lyric_opt_display(self, which: str, v: int) -> None:
        """同步「歌词选项」滑块显示（面板未创建时忽略）。"""
        op = getattr(self, "op", None)
        if op is None:
            return
        fn = {"alpha": "set_lyric_alpha_display",
              "width": "set_lyric_width_display",
              "font": "set_lyric_font_display"}.get(which)
        if fn is None:
            return
        try:
            getattr(op, fn)(v)
        except Exception:
            pass

    def _ensure_lyric_bar(self) -> None:
        """创建桌面歌词条窗口：无修饰符 + 置顶 + 半透明 + 不占任务栏。"""
        if self._lyric_bar is not None:
            return
        bar = tk.Toplevel(self.root)
        bar.overrideredirect(True)   # 无窗口修饰符
        bar.configure(bg=BG_COLOR)
        for attr, val in (("-topmost", True),
                          ("-alpha", self._lyric_bar_alpha / 100.0),
                          ("-toolwindow", True)):
            try:
                bar.attributes(attr, val)
            except tk.TclError:
                pass
        self._lyric_bar_var = tk.StringVar(value="")
        self._lyric_bar_label = tk.Label(
            bar, textvariable=self._lyric_bar_var, bg=BG_COLOR,
            fg=ACCENT_COLOR, font=self.lyric_bar_font,
            anchor="center", justify="center")
        self._lyric_bar_label.pack(fill="both", expand=True)
        self._lyric_bar = bar
        # 点击穿透须在窗口映射后设置到顶层窗口（延迟一轮事件循环）
        try:
            bar.after(0,
                      lambda b=bar: self._enable_lyric_bar_click_through(b))
        except Exception:
            pass

    def _enable_lyric_bar_click_through(self, bar: tk.Toplevel) -> None:
        """Windows：为歌词条添加鼠标点击穿透（不遮挡下方窗口的点击操作）。"""
        if os.name != "nt":
            return
        try:
            import ctypes
            import ctypes.wintypes as wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.restype = wintypes.HWND
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                              ctypes.c_long]
            inner = bar.winfo_id()
            hwnd = (user32.GetAncestor(inner, 2)      # GA_ROOT 顶层窗口
                    or user32.GetParent(inner) or inner)
            gwl_exstyle = -20
            style = user32.GetWindowLongW(hwnd, gwl_exstyle) & 0xFFFFFFFF
            user32.SetWindowLongW(
                hwnd, gwl_exstyle,
                style | 0x00000020    # WS_EX_TRANSPARENT 鼠标穿透
                | 0x00080000          # WS_EX_LAYERED（与 -alpha 配合）
                | 0x08000000)         # WS_EX_NOACTIVATE 点击不激活
        except Exception:
            pass

    def _destroy_lyric_bar(self) -> None:
        """销毁桌面歌词条窗口（幂等）。"""
        bar = self._lyric_bar
        self._lyric_bar = None
        self._lyric_bar_label = None
        if bar is not None:
            try:
                bar.destroy()
            except tk.TclError:
                pass

    def _current_stem_line_text(self) -> str:
        """当前播放位置对应的**完整主干行**文本（跳过逐字展开中间态）。"""
        if not self.lrc_lines:
            return ("未加载歌词" if self.lrc_path is None
                    else "未找到时间轴歌词")
        cur = self.current_lrc_index if self.current_lrc_index >= 0 else 0
        cur = max(0, min(cur, len(self.lrc_lines) - 1))
        stem = self._build_lyric_stem(self.lrc_lines)
        if not stem:
            return self.lrc_lines[cur][1].rstrip(" \u3000")
        g = bisect_right(stem, cur) - 1
        if cur not in stem:
            g += 1   # 逐字中间态属于下一个主干分组
        g = max(0, min(g, len(stem) - 1))
        return self.lrc_lines[stem[g]][1].rstrip(" \u3000")

    def _lyric_bar_xy_for(self, width: int, height: int) -> tuple[int, int]:
        """按当前模式/锚点/偏移计算条坐标（自定义模式不做屏内钳制）。"""
        xpad = self.screen_w - width
        ypad = self.screen_h - height
        if self._lyric_bar_mode == "custom":
            fx, fy = LYRIC_ANCHOR_FRAC.get(self._lyric_bar_anchor,
                                           (0.5, 0.5))
            return (int(round(xpad * fx)) + self._lyric_bar_x,
                    int(round(ypad * fy)) + self._lyric_bar_y)
        return xpad // 2, (0 if self._lyric_bar_mode == "top" else ypad)

    def _place_lyric_bar(self, width: int, height: int) -> None:
        """应用歌词条窗口尺寸与位置（兼容负坐标：超出屏幕时仍可定位）。"""
        x, y = self._lyric_bar_xy_for(width, height)
        try:
            self._lyric_bar.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self._lyric_bar.lift()
        except tk.TclError:
            pass

    def _position_lyric_bar(self) -> None:
        """轻量移动歌词条（X/Y 拖动专用：不重建文本、不重新测量）。"""
        if self._lyric_bar is None:
            return
        width, height = self._lyric_bar_size
        if width <= 0 or height <= 0:
            self._update_lyric_bar()
            return
        self._place_lyric_bar(width, height)

    def _update_lyric_bar(self, rebuild_text: bool = True) -> None:
        """刷新歌词条内容与几何：宽度随歌词实时变化。

        初始宽度为屏幕的「默认宽度」比例（默认 25%）；歌词较长时加宽，
        上限为屏幕全宽。位置：顶部/底部模式贴屏幕上下边居中；自定义模式为
        锚点九宫格基础位置 + X/Y 偏移（锚点处为原点；按约定不做屏内钳制，
        极端情况下允许部分超出屏幕）。
        rebuild_text=False：宽度/字体拖动中复用缓存文本（免重建主干行）。
        """
        if self._lyric_bar is None or self._lyric_bar_var is None:
            return
        if rebuild_text or self._lyric_bar_text is None:
            self._lyric_bar_text = self._current_stem_line_text()
            self._lyric_bar_var.set(self._lyric_bar_text)
        text = self._lyric_bar_text or ""
        try:
            need = self.lyric_bar_font.measure(text) + self._px(40)
        except Exception:
            need = 0
        base = int(self.screen_w * self._lyric_bar_width_pct / 100)
        width = min(self.screen_w, max(base, need))
        height = self.lyric_bar_font.metrics("linespace") + self._px(18)
        self._lyric_bar_size = (width, height)
        self._place_lyric_bar(width, height)

    # ==================================================================
    # 主循环
    # ==================================================================

    def _tick(self) -> None:
        """定时器回调：更新进度条、时间、歌词同步，检测曲目结束。"""
        if self.engine.ready and self.is_playing and not self.is_paused:
            if not self.engine.is_busy():
                self._handle_track_end()
            elif not self.user_seeking:
                current = self._current_time()
                if self.duration:
                    self.seek_var.set(current)
                self.time_var.set(
                    f"{format_time(current)} / {format_time(self.duration)}")
                self._sync_lyrics(current)
        elif self.is_paused and not self.user_seeking:
            current = self._current_time()
            self.time_var.set(
                f"{format_time(current)} / {format_time(self.duration)}")
        self.root.after(TICK_INTERVAL_MS, self._tick)

    def _handle_track_end(self) -> None:
        """曲目播放结束后：优先播放插播列表队首，否则按模式导航。"""
        # ---- 插播优先（除「仅一首」模式外）----
        if self.play_mode != "single" and self.interlude_items:
            self._play_next_interlude()
            return

        if self.play_mode == "loop_one":
            if self.audio_path:
                self._seek_to(0.0)
                return
            self._reset_playback_state()
            return

        if self.play_mode == "single":
            self._reset_playback_state()
            return

        if not self.audio_items or self.current_song_index is None:
            self._reset_playback_state()
            return

        if self.play_mode == "shuffle":
            if len(self.audio_items) == 1:
                next_idx = self.current_song_index
            else:
                choices = [
                    i for i in range(len(self.audio_items))
                    if i != self.current_song_index
                ]
                next_idx = random.choice(choices)
            self._load_track_by_index(next_idx, autoplay=True)
            return

        # loop_all
        next_idx = (self.current_song_index + 1) % len(self.audio_items)
        self._load_track_by_index(next_idx, autoplay=True)

    def _play_next_interlude(self) -> None:
        """播放插播列表队首歌曲，并将其从列表中移除。"""
        if not self.interlude_items:
            return

        # 若处于移动模式，队首被消费需调整锁定索引
        if self._il_move_mode and self._move_locked_index is not None:
            if self._move_locked_index == 0:
                # 锁定项即队首，将被消费，退出移动模式
                self._il_move_confirm()
            else:
                # 队首移除后，所有后续索引前移一位
                self._move_locked_index -= 1

        item = self.interlude_items.pop(0)
        self._refresh_interlude_list()

        # 移动模式下恢复选中状态
        if self._il_move_mode and self._move_locked_index is not None:
            self.interlude_list.selection_set(self._move_locked_index)

        path = item.get("path")
        if not path or not os.path.exists(path):
            return
        # 同步 current_song_index 到此曲在主列表中的位置
        for i, main_item in enumerate(self.audio_items):
            if main_item.get("path") == path:
                self.current_song_index = i
                break
        # 加载并播放
        self.engine.load(path)
        self.audio_path = path
        self.duration = item.get("duration") or self.engine.get_duration(path)
        self._configure_seek_range()
        self._reset_playback_state()
        self._update_info()
        self._update_cover(path)
        lrc = item.get("lrc")
        if lrc and os.path.exists(lrc):
            self._load_lrc_file(lrc)
        else:
            self._apply_placeholder_lrc(path)
        self._play()

    # ==================================================================
    # 关闭
    # ==================================================================

    def on_close(self) -> None:
        """窗口关闭时的清理。"""
        self._flush_config_save()   # 兜底保存最新配置/文件夹
        self._destroy_lyric_bar()   # 先销毁独立歌词条小窗
        self.engine.quit()
        self.root.destroy()
