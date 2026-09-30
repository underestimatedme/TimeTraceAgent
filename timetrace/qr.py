"""Minimal QR Code encoder (ISO/IEC 18004), standard library only.

Byte mode, error correction M (falling back to L when M does not fit),
versions 1-6 — plenty for a timetrace://pair link. `encode` returns a square
matrix of booleans (True = dark); `render_half_blocks` turns it into lines
of Unicode half blocks so two module rows fit in one terminal line (see
timetrace.pairscreen for the pairing screen built on it).
"""
from typing import List

Matrix = List[List[bool]]

# (version, level) -> (ec codewords per block, number of blocks, data codewords per block)
_BLOCKS = {
    (1, "M"): (10, 1, 16), (2, "M"): (16, 1, 28), (3, "M"): (26, 1, 44),
    (4, "M"): (18, 2, 32), (5, "M"): (24, 2, 43), (6, "M"): (16, 4, 27),
    (1, "L"): (7, 1, 19), (2, "L"): (10, 1, 34), (3, "L"): (15, 1, 55),
    (4, "L"): (20, 1, 80), (5, "L"): (26, 1, 108), (6, "L"): (18, 2, 68),
}
_LEVEL_FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 6

_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i] = _value
    _LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        poly = nxt
    return poly


def _rs_remainder(data: List[int], degree: int) -> List[int]:
    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(degree):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _codewords(payload: bytes, version: int, level: str) -> List[int]:
    ec_len, block_count, data_len = _BLOCKS[(version, level)]
    capacity = block_count * data_len
    bits = "0100" + format(len(payload), "08b") + "".join(format(b, "08b") for b in payload)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks = [data[i * data_len:(i + 1) * data_len] for i in range(block_count)]
    ecs = [_rs_remainder(block, ec_len) for block in blocks]
    out = [block[i] for i in range(data_len) for block in blocks]
    out += [ec[i] for i in range(ec_len) for ec in ecs]
    return out


def _choose(payload: bytes, levels=("M", "L")):
    for level in levels:
        for version in range(1, MAX_VERSION + 1):
            _, block_count, data_len = _BLOCKS[(version, level)]
            if 12 + 8 * len(payload) <= block_count * data_len * 8:
                return version, level
    raise ValueError("payload too long for a version %d QR code: %d bytes" % (MAX_VERSION, len(payload)))


class _Grid:
    def __init__(self, version: int):
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = dark
        self.function[row][col] = True

    def draw_function_patterns(self, version: int) -> None:
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for r0, c0 in ((3, 3), (3, size - 4), (size - 4, 3)):
            for dr in range(-4, 5):
                for dc in range(-4, 5):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < size and 0 <= c < size:
                        ring = max(abs(dr), abs(dc))
                        self.set(r, c, ring not in (2, 4))
        if version >= 2:  # versions 2-6 carry one alignment pattern
            center = size - 7
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    self.set(center + dr, center + dc, max(abs(dr), abs(dc)) != 1)
        self.draw_format(0, "M")  # reserve; overwritten once the mask is chosen

    def draw_format(self, mask: int, level: str) -> None:
        data = _LEVEL_FORMAT_BITS[level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        size = self.size
        for i in range(6):
            self.set(i, 8, bit[i])
        self.set(7, 8, bit[6])
        self.set(8, 8, bit[7])
        self.set(8, 7, bit[8])
        for i in range(9, 15):
            self.set(8, 14 - i, bit[i])
        for i in range(8):
            self.set(8, size - 1 - i, bit[i])
        for i in range(8, 15):
            self.set(size - 15 + i, 8, bit[i])
        self.set(size - 8, 8, True)

    def place_data(self, codewords: List[int]) -> None:
        bits = [(byte >> (7 - i)) & 1 == 1 for byte in codewords for i in range(8)]
        index = 0
        size = self.size
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    col = right - j
                    upward = ((right + 1) & 2) == 0
                    row = size - 1 - vert if upward else vert
                    if not self.function[row][col] and index < len(bits):
                        self.dark[row][col] = bits[index]
                        index += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for r in range(self.size):
            for c in range(self.size):
                if not self.function[r][c] and _MASKS[mask](r, c):
                    self.dark[r][c] = not self.dark[r][c]


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _line_penalty(line: List[bool]) -> int:
    """Rules 1 and 3 along one row or column."""
    score = 0
    run = 1
    for i in range(1, len(line) + 1):
        if i < len(line) and line[i] == line[i - 1]:
            run += 1
            continue
        if run >= 5:
            score += 3 + run - 5
        run = 1
    padded = [False] * 4 + line + [False] * 4
    finder = [True, False, True, True, True, False, True]
    for i in range(len(padded) - 6):
        if padded[i:i + 7] == finder:
            if padded[i - 4:i] == [False] * 4:
                score += 40
            if padded[i + 7:i + 11] == [False] * 4:
                score += 40
    return score


def _penalty(m: Matrix) -> int:
    size = len(m)
    score = sum(_line_penalty(row) for row in m)
    score += sum(_line_penalty([m[r][c] for r in range(size)]) for c in range(size))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + max(0, k) * 10


def encode(text: str, levels=("M", "L")) -> Matrix:
    """Smallest version at the first level of `levels` that fits (M, then L)."""
    payload = text.encode("utf-8")
    version, level = _choose(payload, tuple(levels))
    codewords = _codewords(payload, version, level)
    best = None
    for mask in range(8):
        grid = _Grid(version)
        grid.draw_function_patterns(version)
        grid.place_data(codewords)
        grid.apply_mask(mask)
        grid.draw_format(mask, level)
        score = _penalty(grid.dark)
        if best is None or score < best[0]:
            best = (score, grid.dark)
    return best[1]


QUIET_ZONE = 4  # modules of light margin the standard asks for on every side


def render_half_blocks(matrix: Matrix, border: int = QUIET_ZONE, dark: bool = False) -> List[str]:
    """Two module rows per text line, `border` light modules around the code.

    dark=False draws the light modules and leaves dark ones blank: for a
    terminal whose own colours are light-on-dark. dark=True draws the dark
    modules: for black-on-white colours set by the caller (ANSI). A code with
    an odd row count gets one more light row at the bottom."""
    rows = len(matrix)
    cols = len(matrix[0]) if matrix else 0
    height, width = rows + 2 * border, cols + 2 * border

    def drawn(r: int, c: int) -> bool:
        light = True
        if r < height:
            r, c = r - border, c - border
            if 0 <= r < rows and 0 <= c < cols:
                light = not matrix[r][c]
        return light != dark

    glyphs = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return ["".join(glyphs[(drawn(r, c), drawn(r + 1, c))] for c in range(width))
            for r in range(0, height, 2)]
