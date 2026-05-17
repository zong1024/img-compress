#!/usr/bin/env python3
"""Batch-compress images for easy sharing."""

from __future__ import annotations

import argparse
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
MIN_TARGET_MB = 1.0
MAX_TARGET_MB = 5.0
DEFAULT_TARGET_MB = 2.0
DEFAULT_MAX_DIM = 4096
DEFAULT_WORKERS = 4
MIN_JPEG_QUALITY = 25
MAX_JPEG_QUALITY = 95
QUALITY_STEP = 5
RESIZE_FACTOR = 0.85


@dataclass(frozen=True)
class CompressionResult:
    source: Path
    output: Path | None
    original_size: int
    compressed_size: int | None
    warning: str | None = None

    @property
    def ratio(self) -> float | None:
        if not self.compressed_size or self.original_size <= 0:
            return None
        return self.original_size / self.compressed_size


def bytes_to_mb(num_bytes: int) -> float:
    return num_bytes / (1024 * 1024)


def discover_images(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def normalize_image(image: Image.Image) -> Image.Image:
    """Apply EXIF orientation and ensure JPEG-compatible color mode."""
    image = ImageOps.exif_transpose(image)
    if image.mode == "P":
        image = image.convert("RGBA") if "transparency" in image.info else image.convert("RGB")
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        background.paste(image.convert("RGB"), mask=alpha)
        return background
    if image.mode not in {"RGB", "L"}:
        return image.convert("RGB")
    return image


def resize_to_max_dim(image: Image.Image, max_dim: int) -> Image.Image:
    longest_side = max(image.size)
    if longest_side <= max_dim:
        return image

    scale = max_dim / longest_side
    new_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(new_size, Image.Resampling.LANCZOS)


def encode_jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()


def compress_image(source: Path, output_dir: Path, target_bytes: int, max_dim: int) -> CompressionResult:
    original_size = source.stat().st_size
    # Keep outputs unique even when a folder contains photo.jpg and photo.png.
    output_path = output_dir / f"{source.name}.jpg"

    try:
        with Image.open(source) as opened:
            opened.load()
            image = normalize_image(opened)

        image = resize_to_max_dim(image, max_dim)

        best_bytes: bytes | None = None
        working = image

        while True:
            for quality in range(MAX_JPEG_QUALITY, MIN_JPEG_QUALITY - 1, -QUALITY_STEP):
                candidate = encode_jpeg(working, quality)
                if best_bytes is None or len(candidate) < len(best_bytes):
                    best_bytes = candidate
                if len(candidate) <= target_bytes:
                    output_path.write_bytes(candidate)
                    return CompressionResult(source, output_path, original_size, len(candidate))

            # If quality alone is not enough, shrink dimensions and retry.
            new_width = max(1, round(working.width * RESIZE_FACTOR))
            new_height = max(1, round(working.height * RESIZE_FACTOR))
            if (new_width, new_height) == working.size:
                break
            working = working.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Safety valve for pathological files; still write the best available result.
            if max(working.size) <= 256:
                break

        if best_bytes is None:
            raise RuntimeError("no compressed output could be produced")

        output_path.write_bytes(best_bytes)
        return CompressionResult(source, output_path, original_size, len(best_bytes))

    except (UnidentifiedImageError, OSError, ValueError, RuntimeError) as exc:
        return CompressionResult(source, None, original_size, None, warning=str(exc))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def target_size_mb(value: str) -> float:
    parsed = float(value)
    if not MIN_TARGET_MB <= parsed <= MAX_TARGET_MB:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_TARGET_MB:g} and {MAX_TARGET_MB:g} MB"
        )
    return parsed


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-compress images into a compressed/ subfolder for easy sharing."
    )
    parser.add_argument("directory", type=Path, help="Directory containing images to compress")
    parser.add_argument(
        "--target-size",
        type=target_size_mb,
        default=DEFAULT_TARGET_MB,
        metavar="MB",
        help=f"Target output size in MB ({MIN_TARGET_MB:g}-{MAX_TARGET_MB:g}, default: {DEFAULT_TARGET_MB:g})",
    )
    parser.add_argument(
        "--max-dim",
        type=positive_int,
        default=DEFAULT_MAX_DIM,
        metavar="PX",
        help=f"Maximum longest-side dimension in pixels (default: {DEFAULT_MAX_DIM})",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=f"Number of concurrent workers (default: {DEFAULT_WORKERS})",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    directory = args.directory.expanduser().resolve()

    if not directory.exists() or not directory.is_dir():
        print(f"Error: not a directory: {directory}", file=sys.stderr)
        return 2

    images = discover_images(directory)
    total = len(images)
    if total == 0:
        print(f"No supported images found in {directory}")
        return 0

    output_dir = directory / "compressed"
    output_dir.mkdir(exist_ok=True)
    target_bytes = int(args.target_size * 1024 * 1024)

    print(
        f"Processing {total} image(s) with {args.workers} worker(s) "
        f"→ target {args.target_size:g} MB, max dimension {args.max_dim}px"
    )

    completed = 0
    warnings = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(compress_image, image, output_dir, target_bytes, args.max_dim): image
            for image in images
        }

        for future in as_completed(futures):
            completed += 1
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive: keep the batch moving no matter what
                warnings += 1
                print(f"[{completed}/{total}] WARNING {source.name}: unexpected error: {exc}")
                continue

            if result.warning:
                warnings += 1
                print(f"[{completed}/{total}] WARNING {source.name}: skipped ({result.warning})")
                continue

            ratio = result.ratio or 0.0
            print(
                f"[{completed}/{total}] {source.name}: "
                f"{bytes_to_mb(result.original_size):.2f} MB → "
                f"{bytes_to_mb(result.compressed_size or 0):.2f} MB "
                f"({ratio:.2f}x smaller)"
            )

    print(f"Done. Wrote compressed images to {output_dir}")
    if warnings:
        print(f"Completed with {warnings} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
