"""Pillow adapter (F-007): 8 image tools.

Handlers run INSIDE the sandbox worker. `ws` is the workspace confinement
object passed by the worker; all paths resolve under the workspace root.
Limits are declared in tool descriptions and enforced by the input models
(boundary-inclusive, F-020).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from adapters.base import CORRUPT_FILE, VALIDATION_ERROR, ToolError, ToolSpec

MAX_DIM = 8192
_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


def _load(ws, path: str):
    from PIL import Image

    resolved = ws.resolve(path, must_exist=True)
    try:
        img = Image.open(resolved)
        img.load()
    except Exception as exc:
        raise ToolError(CORRUPT_FILE, f"cannot decode image: {exc}", {"path": path}) from exc
    return img, resolved


def _save(ws, img, out_path: str | None, fmt: str, suggested: str, **save_kw) -> dict:
    target = ws.resolve(out_path) if out_path else ws.new_output_path(suggested)
    img.save(target, format=fmt, **save_kw)
    return {"output": ws.resource_ref(target, _MIME.get(fmt, "application/octet-stream"))}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageInfoIn(_In):
    path: str = Field(description="Image file path, relative to the workspace root")


class ImageResizeIn(_In):
    path: str
    width: int = Field(gt=0, le=MAX_DIM, description="Target width px (1–8192)")
    height: int = Field(gt=0, le=MAX_DIM, description="Target height px (1–8192)")
    out_path: str | None = Field(default=None, description="Optional output path in workspace")


class ImageCropIn(_In):
    path: str
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    right: int = Field(gt=0)
    bottom: int = Field(gt=0)
    out_path: str | None = None


class ImageRotateIn(_In):
    path: str
    degrees: float = Field(description="Counter-clockwise degrees")
    expand: bool = Field(default=True, description="Expand canvas to fit rotated image")
    out_path: str | None = None


class ImageConvertIn(_In):
    path: str
    format: Literal["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"]
    out_path: str | None = None


class ImageThumbnailIn(_In):
    path: str
    max_size: int = Field(gt=0, le=MAX_DIM, description="Longest-side cap px (1–8192)")
    out_path: str | None = None


class ImageCompressIn(_In):
    path: str
    quality: int = Field(ge=1, le=95, description="JPEG quality 1–95")
    out_path: str | None = None


class ImageStripIn(_In):
    path: str
    out_path: str | None = None


def image_info(ws, path: str) -> dict[str, Any]:
    img, resolved = _load(ws, path)
    return {
        "width": img.width,
        "height": img.height,
        "format": img.format,
        "mode": img.mode,
        "has_alpha": "A" in img.getbands(),
        "size_bytes": Path(resolved).stat().st_size,
        "has_exif": bool(img.getexif()),
    }


def image_resize(ws, path: str, width: int, height: int, out_path: str | None = None) -> dict:
    from PIL import Image

    img, _ = _load(ws, path)
    resized = img.resize((width, height), Image.LANCZOS)
    fmt = img.format or "PNG"
    return _save(ws, resized, out_path, fmt, f"resized-{width}x{height}.{fmt.lower()}")


def image_crop(
    ws, path: str, left: int, top: int, right: int, bottom: int, out_path: str | None = None
) -> dict:
    img, _ = _load(ws, path)
    if right > img.width or bottom > img.height or right <= left or bottom <= top:
        raise ToolError(
            VALIDATION_ERROR,
            "crop box outside image bounds",
            {"image": [img.width, img.height], "box": [left, top, right, bottom]},
        )
    cropped = img.crop((left, top, right, bottom))
    fmt = img.format or "PNG"
    return _save(ws, cropped, out_path, fmt, f"cropped.{fmt.lower()}")


def image_rotate(ws, path: str, degrees: float, expand: bool = True, out_path: str | None = None) -> dict:
    img, _ = _load(ws, path)
    rotated = img.rotate(degrees, expand=expand)
    fmt = img.format or "PNG"
    return _save(ws, rotated, out_path, fmt, f"rotated.{fmt.lower()}")


def image_convert_format(ws, path: str, format: str, out_path: str | None = None) -> dict:
    img, _ = _load(ws, path)
    warnings: list[str] = []
    out = img
    if format == "JPEG" and "A" in img.getbands():
        out = img.convert("RGB")
        warnings.append("alpha channel dropped: JPEG does not support transparency")
    result = _save(ws, out, out_path, format, f"converted.{format.lower()}")
    if warnings:
        result["warnings"] = warnings
    return result


def image_thumbnail(ws, path: str, max_size: int, out_path: str | None = None) -> dict:
    img, _ = _load(ws, path)
    thumb = img.copy()
    thumb.thumbnail((max_size, max_size))
    fmt = img.format or "PNG"
    result = _save(ws, thumb, out_path, fmt, f"thumbnail.{fmt.lower()}")
    result["width"], result["height"] = thumb.width, thumb.height
    return result


def image_compress_jpeg(ws, path: str, quality: int, out_path: str | None = None) -> dict:
    img, _ = _load(ws, path)
    out = img.convert("RGB") if "A" in img.getbands() else img
    return _save(ws, out, out_path, "JPEG", "compressed.jpg", quality=quality, optimize=True)


def image_strip_metadata(ws, path: str, out_path: str | None = None) -> dict:
    from PIL import Image

    img, _ = _load(ws, path)
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    fmt = img.format or "PNG"
    result = _save(ws, clean, out_path, fmt, f"stripped.{fmt.lower()}")
    result["metadata_removed"] = True
    return result


TOOLS = [
    ToolSpec(
        "image_info",
        "Read image metadata (dimensions, format, mode, EXIF presence). Input: workspace image path.",
        ImageInfoIn,
        image_info,
    ),
    ToolSpec(
        "image_resize",
        f"Resize an image to width×height px (each 1–{MAX_DIM}). Output: workspace file resource.",
        ImageResizeIn,
        image_resize,
        limits={"max_dim": MAX_DIM},
    ),
    ToolSpec(
        "image_crop",
        "Crop an image to a pixel box (left, top, right, bottom); box must lie inside the image.",
        ImageCropIn,
        image_crop,
    ),
    ToolSpec(
        "image_rotate",
        "Rotate an image by degrees counter-clockwise; expand keeps the full rotated image.",
        ImageRotateIn,
        image_rotate,
    ),
    ToolSpec(
        "image_convert_format",
        "Convert an image to PNG/JPEG/WEBP/GIF/BMP/TIFF. JPEG drops alpha (a warning is returned).",
        ImageConvertIn,
        image_convert_format,
    ),
    ToolSpec(
        "image_thumbnail",
        f"Create a thumbnail with longest side ≤ max_size px (1–{MAX_DIM}).",
        ImageThumbnailIn,
        image_thumbnail,
        limits={"max_dim": MAX_DIM},
    ),
    ToolSpec(
        "image_compress_jpeg",
        "Re-encode an image as JPEG with quality 1–95 (boundary-inclusive).",
        ImageCompressIn,
        image_compress_jpeg,
        limits={"quality_max": 95},
    ),
    ToolSpec(
        "image_strip_metadata",
        "Remove EXIF/GPS metadata; output contains no EXIF segment.",
        ImageStripIn,
        image_strip_metadata,
    ),
]
