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
    """
    text = "".join(lines or [])
    if len(re.sub(r"\s", "", text)) < 2:
        return False
    return cjk_ratio(text) < FOREIGN_CJK_RATIO


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
