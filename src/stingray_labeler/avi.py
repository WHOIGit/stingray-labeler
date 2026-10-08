"""Direct frame access for uncompressed AVI video.

Every frame of an uncompressed AVI has the same size, so frame N is one seek to
``first_frame + N * stride`` and one read; nothing else in the file is decoded.
Frame N is the Nth non-empty video chunk counting from 0, which is the order
OpenCV reads frames in (it skips the empty chunk some recorders write first).
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

GREY_PALETTE = bytes(value for value in range(256) for _channel in range(3))


class VideoFormatError(ValueError):
    """The file is not an uncompressed AVI this reader can address frame by frame."""


@dataclass(frozen=True)
class AviInfo:
    path: Path
    width: int
    height: int
    bit_count: int
    bottom_up: bool
    palette: bytes | None  # RGB triples for 8-bit video; None when it is plain greyscale
    first_frame: int  # file offset of frame 0's pixel data
    frame_bytes: int
    stride: int  # distance from one frame's pixel data to the next
    frame_count: int
    fps: float

    @property
    def row_bytes(self) -> int:
        return (self.width * self.bit_count + 31) // 32 * 4

    def frame_name(self, index: int) -> str:
        """Project image name for a frame, matching the pipeline's ``<video stem>_<frame>.png``."""
        return f"{self.path.stem}_{index}.png"

    def to_image(self, data: bytes) -> Image.Image:
        size = (self.width, self.height)
        orientation = -1 if self.bottom_up else 1
        if self.bit_count == 24:
            return Image.frombytes("RGB", size, data, "raw", "BGR", self.row_bytes, orientation)
        if self.palette is None:
            return Image.frombytes("L", size, data, "raw", "L", self.row_bytes, orientation)
        image = Image.frombytes("P", size, data, "raw", "P", self.row_bytes, orientation)
        image.putpalette(self.palette)
        return image


@dataclass(frozen=True)
class VideoFrameRef:
    """One frame of a video, used where an image file path would otherwise be."""

    video: AviInfo
    index: int

    @property
    def name(self) -> str:
        return f"{self.video.path.name} frame {self.index}"

    def __str__(self) -> str:
        return f"{self.video.path} (frame {self.index})"

    def save_png(self, target: Path) -> None:
        AviReader(self.video).read_frame(self.index).save(target, format="PNG")


def read_avi_info(path: Path) -> AviInfo:
    """Read only the AVI headers and the position of frame 0; no pixels are read."""
    try:
        return _read_avi_info(path)
    except struct.error:
        raise VideoFormatError("The AVI header is incomplete") from None


def _read_avi_info(path: Path) -> AviInfo:
    with open(path, "rb") as stream:
        file_size = os.fstat(stream.fileno()).st_size
        head = stream.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"AVI ":
            raise VideoFormatError("Not an AVI file")
        # The RIFF length is ignored: recorders that never finalise the file leave it at 0.
        stream_header = bitmap = None
        palette = b""
        total_frames = 0
        offset = 12
        movi_start = None
        while offset + 8 <= file_size:
            stream.seek(offset)
            chunk_id, size = struct.unpack("<4sI", stream.read(8))
            if chunk_id == b"LIST":
                list_type = stream.read(4)
                if list_type == b"movi":
                    movi_start = offset + 12
                    break
                if list_type == b"hdrl":
                    header = stream.read(max(0, size - 4))
                    total_frames, stream_header, bitmap, palette = _parse_hdrl(header)
            offset += 8 + size + (size & 1)
        if movi_start is None or bitmap is None:
            raise VideoFormatError("No video stream or frame data found")

        _bi_size, width, height, _planes, bit_count, compression, _image_size = struct.unpack_from(
            "<IiiHHII", bitmap
        )
        if compression != 0 or bit_count not in (8, 24):
            raise VideoFormatError(
                "Only uncompressed 8-bit or 24-bit AVI video is supported "
                f"(this file: {bit_count}-bit, compression {_fourcc(compression)})"
            )
        if width <= 0 or height == 0:
            raise VideoFormatError("Invalid frame size in the AVI header")
        bottom_up = height > 0
        height = abs(height)
        row_bytes = (width * bit_count + 31) // 32 * 4
        frame_bytes = row_bytes * height

        # Frame 0 is the first video chunk with pixels; empty chunks before it are not frames.
        skipped = 0
        first_frame = None
        offset = movi_start
        for _ in range(64):
            if offset + 8 > file_size:
                break
            stream.seek(offset)
            chunk_id, size = struct.unpack("<4sI", stream.read(8))
            if chunk_id == b"LIST":
                offset += 12  # step into a 'rec ' group
                continue
            if chunk_id[2:] in (b"db", b"dc"):
                if size == frame_bytes:
                    first_frame = offset + 8
                    break
                if size == 0:
                    skipped += 1
                else:
                    raise VideoFormatError("Video frames are not stored uncompressed at a fixed size")
            offset += 8 + size + (size & 1)
        if first_frame is None:
            raise VideoFormatError("No video frames found")

    stride = 8 + frame_bytes + (frame_bytes & 1)
    available = file_size - first_frame
    frame_count = 0 if available < frame_bytes else 1 + (available - frame_bytes) // stride
    header_count = total_frames - skipped
    if header_count > 0:
        frame_count = min(frame_count, header_count)
    if frame_count <= 0:
        raise VideoFormatError("The video contains no complete frames")

    scale, rate = struct.unpack_from("<II", stream_header, 20)
    fps = rate / scale if scale else 0.0
    rgb_palette = None
    if bit_count == 8:
        colours = palette[:256 * 4]  # RGBQUAD entries: blue, green, red, reserved
        rgb_palette = b"".join(
            bytes((colours[i + 2], colours[i + 1], colours[i])) for i in range(0, len(colours) - 3, 4)
        )
        if not rgb_palette or rgb_palette == GREY_PALETTE:
            rgb_palette = None
    return AviInfo(
        Path(path), width, height, bit_count, bottom_up, rgb_palette,
        first_frame, frame_bytes, stride, frame_count, fps,
    )


def _parse_hdrl(header: bytes) -> tuple[int, bytes | None, bytes | None, bytes]:
    """Return (total frames, video stream header, BITMAPINFOHEADER, palette) from the hdrl list."""
    total_frames = 0
    offset = 0
    while offset + 8 <= len(header):
        chunk_id, size = struct.unpack_from("<4sI", header, offset)
        body = header[offset + 8:offset + 8 + size]
        if chunk_id == b"avih" and len(body) >= 20:
            total_frames = struct.unpack_from("<I", body, 16)[0]
        elif chunk_id == b"LIST" and body[:4] == b"strl":
            stream_header = bitmap = None
            inner = 4
            while inner + 8 <= len(body):
                inner_id, inner_size = struct.unpack_from("<4sI", body, inner)
                inner_body = body[inner + 8:inner + 8 + inner_size]
                if inner_id == b"strh":
                    stream_header = inner_body
                elif inner_id == b"strf":
                    bitmap = inner_body
                inner += 8 + inner_size + (inner_size & 1)
            if stream_header is not None and stream_header[:4] == b"vids" and bitmap and len(bitmap) >= 40:
                stream_length = struct.unpack_from("<I", stream_header, 32)[0]
                bi_size = struct.unpack_from("<I", bitmap, 0)[0]
                return max(total_frames, stream_length), stream_header, bitmap, bitmap[bi_size:]
        offset += 8 + size + (size & 1)
    return total_frames, None, None, b""


def _fourcc(value: int) -> str:
    text = struct.pack("<I", value).decode("ascii", "replace").strip()
    return repr(text) if value > 255 else str(value)


class AviReader:
    """Reads single frames; the file can be kept open while stepping through one video."""

    def __init__(self, info: AviInfo):
        self.info = info
        self._file = None
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            if self._file is None:
                self._file = open(self.info.path, "rb")

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def read_frame(self, index: int) -> Image.Image:
        info = self.info
        if not 0 <= index < info.frame_count:
            raise IndexError(f"Frame {index} is outside 0–{info.frame_count - 1}")
        with self._lock:
            if self._file is not None:
                data = self._read(self._file, index)
            else:
                with open(info.path, "rb") as stream:
                    data = self._read(stream, index)
        return info.to_image(data)

    def _read(self, stream, index: int) -> bytes:
        info = self.info
        stream.seek(info.first_frame + index * info.stride - 8)
        header = stream.read(8)
        chunk_id, size = struct.unpack("<4sI", header) if len(header) == 8 else (b"", 0)
        if chunk_id[2:] not in (b"db", b"dc") or size != info.frame_bytes:
            raise VideoFormatError(
                f"Frame {index} is not where a fixed-size uncompressed frame should be; "
                "this video cannot be read frame by frame"
            )
        data = stream.read(info.frame_bytes)
        if len(data) != info.frame_bytes:
            raise VideoFormatError(f"Frame {index} is incomplete")
        return data
