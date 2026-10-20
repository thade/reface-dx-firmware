#!/usr/bin/env python3
"""Software model of the reface DX LCD for emulation checks.

The firmware draws by calling 0x020291E8(bitmap, rect) where rect is four
32-bit words {x0, y0, x1, y1} (inclusive) and bitmap is 1bpp, row-major,
MSB-first, with a row stride of ceil(width/8) bytes.  verify_menu captures
those calls; this module replays them into a 128x64 framebuffer and renders
it as text so page layouts can be eyeballed and compared.
"""
WIDTH, HEIGHT = 128, 64

class LCD:
    def __init__(self):
        self.px = [[0] * WIDTH for _ in range(HEIGHT)]
        self.blits = []

    def blit(self, bitmap_bytes, rect):
        x0, y0, x1, y1 = [v - (1 << 32) if v & 0x80000000 else v for v in rect]
        w, h = x1 - x0 + 1, y1 - y0 + 1
        stride = (w + 7) // 8
        self.blits.append((x0, y0, w, h))
        for yy in range(h):
            row = bitmap_bytes[yy * stride:(yy + 1) * stride]
            for xx in range(w):
                bit = (row[xx >> 3] >> (7 - (xx & 7))) & 1 if xx >> 3 < len(row) else 0
                X, Y = x0 + xx, y0 + yy
                if 0 <= X < WIDTH and 0 <= Y < HEIGHT:
                    self.px[Y][X] = bit

    def text(self, on="#", off="."):
        return "\n".join("".join(on if p else off for p in row) for row in self.px)

    def ink(self, x0, y0, x1, y1):
        """Number of lit pixels inside an inclusive rectangle."""
        return sum(self.px[y][x] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))

    def column_ink(self, col):
        """Lit pixels in 32-px column `col` below the title row (y >= 12)."""
        return self.ink(col * 32, 12, col * 32 + 31, HEIGHT - 1)
