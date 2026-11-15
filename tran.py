"""歌词自动翻译（V1.14.0）：外语检测 + 翻译引擎封装。

引擎（`ENGINES`）：
- `bing`：`translators` 包（默认；`translators.translate_text(..., translator="bing")`）；
- `bilibili`：`https://index-translate.bilibili.com/v1/chat/completions`
  （OpenAI 兼容 chat/completions，无需鉴权，模型 `Index-Translate-35B-A3B`）。

本模块保持轻量：重量级依赖（translators / requests）在函数内导入，
`import tran` 本身不加载任何网络库。
"""

import re
import unicodedata

DEFAULT_ENGINE = "bing"
ENGINES = ("bing", "bilibili")
ENGINE_LABELS = {"bing": "Bing", "bilibili": "Bilibili"}

# 歌词正文中中文字符占比低于该值即视为「外语歌词」（少量中文词不算中文歌）
FOREIGN_CJK_RATIO = 0.15

BILIBILI_URL = "https://index-translate.bilibili.com/v1/chat/completions"
BILIBILI_MODEL = "Index-Translate-35B-A3B"
BILIBILI_TIMEOUT = 20


# ----------------------------------------------------------------------
# 语言判定
# ----------------------------------------------------------------------

def cjk_ratio(text: str) -> float:
    """文本中中文字符（CJK 统一表意）占「有效字符」的比例（0~1）。"""
    total = 0
    cjk = 0
    for ch in text or "":
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S", "Z", "C") or ch.isspace():
            continue        # 标点 / 符号 / 分隔 / 控制字符不计入
        total += 1
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
    return cjk / total if total else 1.0


def is_foreign_lyric(lines) -> bool:
    """判断歌词是否为外语（少量单词不算）。

    lines 为歌词纯文本行序列；有效字符过少（<2）时返回 False（无法判定）。
    注：V1.14.16 起逐行判定改用 `needs_translate()`，本函数仅保留备用。
    """
    text = "".join(lines or [])
    if len(re.sub(r"\s", "", text)) < 2:
        return False
    return cjk_ratio(text) < FOREIGN_CJK_RATIO


# 日文假名（平假名 / 片假名 / 半角片假名）与日文符号
_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9d\u30fc]")
# 中文汉字（CJK 统一表意文字）
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _effective_text(text: str) -> str:
    """去掉数字、标点、符号、空白后的正文（用于语言判定，V1.14.17）。

    需求：除了数字与符号之外全是外语的整句也要翻；反之只剩数字/符号的
    句子去掉后为空 → 不需要翻译。
    """
    out = []
    for ch in text or "":
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S", "Z", "C"):
            continue            # 标点 / 符号 / 分隔 / 控制
        if cat[0] == "N":
            continue            # 数字（含全角、罗马数字、分数）
        out.append(ch)
    return "".join(out)


def is_chinese_sentence(text: str) -> bool:
    """判断一句是否属于「中文」（V1.14.16）。

    规则（日语里的汉字**不算**中文）：
    - 含假名 → 日语 → 不是中文；
    - 否则含 CJK 汉字 → 中文；
    - 既无假名又无汉字（英文/韩文/数字等）→ 不是中文。
    """
    s = (text or "").strip()
    if not s:
        return False
    if _KANA.search(s):
        return False
    return bool(_HAN.search(s))


def needs_translate(text: str) -> bool:
    """该行是否需要翻译：**去掉数字与符号后仍无任何中文**才需要（V1.14.16/17）。

    - 纯中文句（含仅有汉字无假名的日文句）→ 不翻（译文为空）；
    - 只剩数字/符号的句（如 “12345”“!!!”）→ 不翻（去掉后无正文）；
    - 其余（英文/日语假名句等）→ 翻译。
    """
    s = _effective_text(text)
    if not s:
        return False
    return not is_chinese_sentence(s)


# ----------------------------------------------------------------------
# 翻译引擎
# ----------------------------------------------------------------------

def translate_bing(text: str, to_lang: str = "zh") -> str:
    """Bing 翻译（`translators` 包）。"""
    import translators

    return translators.translate_text(
        text,
        translator="bing",
        from_language="auto",
        to_language=to_lang,
    )


def translate_bilibili(text: str, to_lang: str = "zh") -> str:
    """Bilibili Index-Translate（OpenAI 兼容接口，无需鉴权）。"""
    import requests

    target = {"zh": "中文", "en": "英语", "ja": "日语"}.get(to_lang, "中文")
    prompt = (f"请将以下文本翻译为{target}，直接输出翻译结果，"
              f"不要进行任何解释。\n\n{text}")
    resp = requests.post(
        BILIBILI_URL,
        json={
            "model": BILIBILI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={"Content-Type": "application/json"},
        timeout=BILIBILI_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return (content or "").strip()


def translate(text: str, engine: str = DEFAULT_ENGINE,
              to_lang: str = "zh") -> tuple[bool, str]:
    """翻译单行文本。返回 (是否成功, 译文 或 错误信息)。"""
    text = (text or "").strip()
    if not text:
        return False, "空文本"
    engine = engine if engine in ENGINES else DEFAULT_ENGINE
    try:
        if engine == "bilibili":
            out = translate_bilibili(text, to_lang)
        else:
            out = translate_bing(text, to_lang)
    except Exception as exc:      # 网络/解析/依赖缺失统一兜底
        return False, f"{type(exc).__name__}: {exc}"
    out = (out or "").strip()
    if not out:
        return False, "翻译结果为空"
    return True, out
