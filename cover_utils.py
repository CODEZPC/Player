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
        return base64.b64decode(pics[0])
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
