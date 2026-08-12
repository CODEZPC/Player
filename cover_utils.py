"""专辑封面提取工具 —— 从音频文件中读取内嵌封面图片。"""

import os

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


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
    """将封面字节数据转换为 tkinter PhotoImage。"""
    if not PIL_AVAILABLE or not data:
        return None
    try:
        from io import BytesIO
        image = Image.open(BytesIO(data))
        image = image.resize((size, size), Image.LANCZOS)
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
