"""Генерирует фирменную иконку rentbot (packaging/rentbot.ico).

Скруглённый квадрат с градиентом сине-фиолетовый + белая молния. Многоразмерный .ico.
Запуск:  .venv/Scripts/python packaging/make_icon.py
"""
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "rentbot.ico")
S = 256  # базовый размер


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int) -> Image.Image:
    # рисуем крупно и уменьшаем (сглаживание)
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # диагональный градиент accent(#4f8cff) -> violet(#9b7bff)
    c1, c2 = (79, 140, 255), (155, 123, 255)
    grad = Image.new("RGB", (big, big))
    gp = grad.load()
    for y in range(big):
        for x in range(big):
            t = (x + y) / (2 * big)
            gp[x, y] = _lerp(c1, c2, t)

    # маска: скруглённый квадрат
    radius = int(big * 0.22)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # молния (белая) по центру
    w = big
    bolt = [
        (0.56 * w, 0.14 * w), (0.34 * w, 0.54 * w), (0.48 * w, 0.54 * w),
        (0.44 * w, 0.86 * w), (0.68 * w, 0.44 * w), (0.52 * w, 0.44 * w),
    ]
    d.polygon(bolt, fill=(255, 255, 255, 240))

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base = render(256)  # Pillow сам уменьшит до всех размеров из списка
    base.save(OUT, format="ICO", sizes=sizes)
    print("icon ->", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
