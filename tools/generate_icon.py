from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SCALE = 4


def scaled_box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(value * SCALE for value in values)


def build_icon() -> Image.Image:
    image = Image.new("RGBA", (256 * SCALE, 256 * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        scaled_box((16, 16, 240, 240)),
        radius=44 * SCALE,
        fill="#174a5b",
    )
    white = "#ffffff"
    line_width = 12 * SCALE
    draw.line(scaled_box((90, 72, 166, 72)), fill=white, width=line_width)
    draw.line(scaled_box((128, 73, 128, 121)), fill=white, width=line_width)
    draw.ellipse(
        scaled_box((53, 53, 91, 91)),
        fill="#64c88a",
        outline=white,
        width=7 * SCALE,
    )
    draw.ellipse(
        scaled_box((165, 53, 203, 91)),
        fill="#62c5df",
        outline=white,
        width=7 * SCALE,
    )
    draw.line(
        [(112 * SCALE, 108 * SCALE), (128 * SCALE, 126 * SCALE), (144 * SCALE, 108 * SCALE)],
        fill=white,
        width=line_width,
        joint="curve",
    )
    draw.rounded_rectangle(
        scaled_box((48, 126, 208, 208)),
        radius=11 * SCALE,
        outline=white,
        width=13 * SCALE,
    )
    draw.line(scaled_box((49, 154, 207, 154)), fill=white, width=line_width)
    draw.line(
        scaled_box((111, 180, 145, 180)),
        fill="#64c88a",
        width=line_width,
    )
    return image.resize((256, 256), Image.Resampling.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    icon.resize((64, 64), Image.Resampling.LANCZOS).save(
        ASSETS / "smsi_archive_64.png"
    )
    icon.save(
        ASSETS / "smsi_archive.ico",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
