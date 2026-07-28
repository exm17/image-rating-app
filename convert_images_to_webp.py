"""Convert image-rating source images to 640x480 WebP at quality 90.

The original files are preserved. Converted files keep the same relative path
and filename stem, with only the extension changed to .webp.
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps


SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def convert_image(
    source: Path,
    destination: Path,
    size: tuple[int, int],
    quality: int,
    force: bool,
) -> tuple[int, int, bool]:
    """Convert one image and return input bytes, output bytes, and conversion status."""
    if destination.exists() and not force:
        return source.stat().st_size, destination.stat().st_size, False

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        image.save(
            destination,
            format="WEBP",
            quality=quality,
            method=4,
            optimize=True,
        )

    return source.stat().st_size, destination.stat().st_size, True


def convert_job(job: tuple[Path, Path, tuple[int, int], int, bool]) -> tuple[int, int, bool]:
    """Process-pool entry point for one conversion job."""
    return convert_image(*job)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert image-rating images to 640x480 WebP q90."
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        default="cg+",
        type=Path,
        help="Directory containing source images (default: cg+)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: write WebP files beside source images)",
    )
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Number of parallel conversion workers (default: up to 8)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace WebP files that already exist",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = (args.output_dir or input_dir).resolve()
    if not input_dir.is_dir():
        parser.error(f"input directory does not exist: {input_dir}")
    if not 1 <= args.quality <= 100:
        parser.error("quality must be between 1 and 100")
    if args.workers < 1:
        parser.error("workers must be at least 1")

    sources = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not sources:
        print(f"No supported images found in {input_dir}")
        return 0

    input_bytes = 0
    output_bytes = 0
    converted = 0
    skipped = 0

    jobs = [
        (
            source,
            output_dir / source.relative_to(input_dir).with_suffix(".webp"),
            (args.width, args.height),
            args.quality,
            args.force,
        )
        for source in sources
    ]

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for index, (before, after, did_convert) in enumerate(
            executor.map(convert_job, jobs, chunksize=8),
            start=1,
        ):
            input_bytes += before
            output_bytes += after
            if did_convert:
                converted += 1
            else:
                skipped += 1
            if index % 100 == 0 or index == len(jobs):
                print(
                    f"Progress: {index}/{len(jobs)} "
                    f"({index / len(jobs) * 100:.1f}%)"
                )

    reduction = 0 if input_bytes == 0 else (1 - output_bytes / input_bytes) * 100
    print(
        f"Done: {converted} converted, {skipped} skipped; "
        f"{input_bytes / 1024 / 1024:.2f} MiB -> "
        f"{output_bytes / 1024 / 1024:.2f} MiB "
        f"({reduction:.1f}% smaller)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
