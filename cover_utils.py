"""专辑封面提取工具 —— 从音频文件中读取内嵌封面图片。"""

import os

# PEP 810 延迟导入（Python 3.15+，V1.9.0）：PIL 首次转图时才真正加载。
# lazy 不能放 try/except 内，可用性改为「首次探测 + 缓存」。
lazy from PIL import Image, ImageTk

_PIL_STATE: bool | None = None


def _pil_available() -> bool:
    """PIL 是否可用（首次调用触发真实导入）。"""
    global _PIL_STATE
    if _PIL_STATE is None:
        try:
            _ = (Image, ImageTk)
            _PIL_STATE = True
        except Exception:
            _PIL_STATE = False
    return _PIL_STATE


def extract_cover_art(path: str) -> bytes | None:
    """从音频文件中提取专辑封面图片的原始字节数据。

    支持 MP3 (ID3 APIC)、FLAC (pictures)、OGG (metadata_block_picture)、WAV (ID3)。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            return _extract_mp3(path)
        elif ext == ".flac":
            return _extract_flac(path)
        elif ext == ".ogg":
            return _extract_ogg(path)
        elif ext == ".wav":
            return _extract_wav(path)
    except Exception:
        pass
    return None


def cover_to_tk_image(data: bytes, size: int = 220) -> "ImageTk.PhotoImage | None":
    """将封面字节数据转换为 tkinter PhotoImage（强制为 size×size 正方形）。"""
    if not _pil_available() or not data:
        return None
    try:
        from io import BytesIO
        image = Image.open(BytesIO(data))
        image = image.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(image)
    except Exception:
        return None


def _cover_content_bbox(image) -> tuple[int, int, int, int]:
    """主要内容 bbox：忽略四周透明边，并忽略边角零星非透明像素（V1.12.7）。

    部分封面被人工调整过——实际图像只在中间，四周是大量透明像素；另有
    些图的边角残留几个非透明像素（`getbbox()` 会被它们撑大，导致内容看清
    很小）。因此按**行/列非透明像素数量**判断：达到阈值（边长 1%，至少 2 像素）
    才算内容行/列，从四边向内收缩。
    """
    width, height = image.size
    try:
        alpha = image.getchannel("A")
    except Exception:
        return (0, 0, width, height)
    try:
        import numpy as np
        mask = np.asarray(alpha) > 0
    except Exception:
        return image.getbbox() or (0, 0, width, height)
    if not mask.any():
        return (0, 0, width, height)
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    row_thr = max(2, int(width * 0.01))
    col_thr = max(2, int(height * 0.01))
    top = 0
    while top < height and rows[top] < row_thr:
        top += 1
    bottom = height - 1
    while bottom > top and rows[bottom] < row_thr:
        bottom -= 1
    left = 0
    while left < width and cols[left] < col_thr:
        left += 1
    right = width - 1
    while right > left and cols[right] < col_thr:
        right -= 1
    if right <= left or bottom <= top:
        return (0, 0, width, height)
    return (left, top, right + 1, bottom + 1)


def cover_to_fill_image(data: bytes, max_w: int, max_h: int
                        ) -> "ImageTk.PhotoImage | None":
    """封面 → 全屏展示图（V1.12.7）。

    与 `cover_to_tk_image` 的区别：**不拉伸**——忽略透明边后按**原比例**
    等比缩放到 `max_w × max_h` 内（允许放大到铺满可用区域），非 1:1 封面
    不会变形。
    """
    if not _pil_available() or not data or max_w <= 0 or max_h <= 0:
        return None
    try:
        from io import BytesIO
        image = Image.open(BytesIO(data)).convert("RGBA")
        box = _cover_content_bbox(image)
        if box != (0, 0, image.width, image.height):
            image = image.crop(box)
        width, height = image.size
        if width <= 0 or height <= 0:
            return None
        scale = min(max_w / width, max_h / height)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        image = image.resize((new_w, new_h), Image.LANCZOS)
        return ImageTk.PhotoImage(image)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _extract_mp3(path: str) -> bytes | None:
    from mutagen.mp3 import MP3
    from mutagen.id3 import APIC
    audio = MP3(path)
    if audio.tags:
        for tag in audio.tags.values():
            if isinstance(tag, APIC):
                return tag.data
    return None


def _extract_flac(path: str) -> bytes | None:
    from mutagen.flac import FLAC
    audio = FLAC(path)
    if audio.pictures:
        return audio.pictures[0].data
    return None


def _extract_ogg(path: str) -> bytes | None:
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(path)
    pics = audio.get("metadata_block_picture", [])
    if pics:
        import base64
        raw = base64.b64decode(pics[0])
        return _parse_flac_picture_block(raw)
    return None


def _parse_flac_picture_block(data: bytes) -> bytes | None:
    """解析 FLAC 图片块，提取其中的原始图片数据。

    FLAC picture block 结构（大端序）：
      4 bytes  - 图片类型
      4 bytes  - MIME 类型长度
      N bytes  - MIME 类型字符串
      4 bytes  - 描述长度
      M bytes  - 描述字符串
      4 bytes  - 宽度
      4 bytes  - 高度
      4 bytes  - 色彩深度
      4 bytes  - 颜色数
      4 bytes  - 图片数据长度
      K bytes  - 图片数据
    """
    import struct
    try:
        pos = 0
        # 跳过图片类型 (4 bytes)
        pos += 4
        # 读取并跳过 MIME 类型
        mime_len = struct.unpack_from(">I", data, pos)[0]
        pos += 4 + mime_len
        # 读取并跳过描述
        desc_len = struct.unpack_from(">I", data, pos)[0]
        pos += 4 + desc_len
        # 跳过宽、高、色彩深度、颜色数 (4 × 4 bytes)
        pos += 16
        # 读取图片数据长度
        pic_len = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        return data[pos:pos + pic_len]
    except (struct.error, IndexError):
        return None


def _extract_wav(path: str) -> bytes | None:
    """WAV 文件封面（ID3 标签中的 APIC 帧）。"""
    from mutagen.wave import WAVE
    from mutagen.id3 import APIC
    audio = WAVE(path)
    if audio.tags:
        for tag in audio.tags.values():
            if isinstance(tag, APIC):
                return tag.data
    return None
