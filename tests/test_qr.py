"""QR encoder tests.

The decoder below is written straight from ISO/IEC 18004 and shares no code
with timetrace.qr: it reads and BCH-checks the format information, unmasks, walks
the zigzag, de-interleaves the blocks, checks every Reed-Solomon syndrome is
zero and parses the byte-mode segment. A reference matrix produced by an
independent encoder (Apple CoreImage CIQRCodeGenerator) pins the exact output.
"""
import unittest

from timetrace import qr

# (version, level) -> (ec codewords per block, [data codewords per block])
BLOCKS = {
    (1, "L"): (7, [19]), (2, "L"): (10, [34]), (3, "L"): (15, [55]),
    (4, "L"): (20, [80]), (5, "L"): (26, [108]), (6, "L"): (18, [68, 68]),
    (1, "M"): (10, [16]), (2, "M"): (16, [28]), (3, "M"): (26, [44]),
    (4, "M"): (18, [32, 32]), (5, "M"): (24, [43, 43]), (6, "M"): (16, [27, 27, 27, 27]),
}
LEVEL_BITS = {1: "L", 0: "M", 3: "Q", 2: "H"}

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def syndromes(codeword, n):
    out = []
    for k in range(n):
        acc = 0
        for c in codeword:
            acc = gf_mul(acc, EXP[k]) ^ c
        out.append(acc)
    return out


def bch_remainder(value, poly, degree):
    value <<= degree
    for shift in range(value.bit_length() - 1, degree - 1, -1):
        if value >> shift & 1:
            value ^= poly << (shift - degree)
    return value


def reserved(size, version):
    fn = [[False] * size for _ in range(size)]

    def box(r0, c0, h, w):
        for r in range(max(0, r0), min(size, r0 + h)):
            for c in range(max(0, c0), min(size, c0 + w)):
                fn[r][c] = True

    box(0, 0, 9, 9)
    box(0, size - 8, 9, 8)
    box(size - 8, 0, 8, 9)
    for i in range(size):
        fn[6][i] = fn[i][6] = True
    if version >= 2:  # versions 2-6 have a single alignment pattern
        last = size - 7
        box(last - 2, last - 2, 5, 5)
    return fn


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def decode(matrix):
    size = len(matrix)
    assert all(len(row) == size for row in matrix)
    version = (size - 17) // 4
    assert 1 <= version <= 6 and size == 17 + 4 * version
    # Format info, copy 1: row 8 columns 0..5,7,8 then column 8 rows 7,5..0.
    cells = [(8, c) for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [(r, 8) for r in (7, 5, 4, 3, 2, 1, 0)]
    raw = 0
    for r, c in cells:
        raw = raw << 1 | int(matrix[r][c])
    # Copy 2: column 8 rows size-1..size-7, then row 8 columns size-8..size-1.
    cells2 = [(r, 8) for r in range(size - 1, size - 8, -1)] + [(8, c) for c in range(size - 8, size)]
    raw2 = 0
    for r, c in cells2:
        raw2 = raw2 << 1 | int(matrix[r][c])
    assert raw == raw2, "format copies differ"
    fmt = raw ^ 0x5412
    assert bch_remainder(fmt >> 10, 0x537, 10) == fmt & 0x3FF, "format BCH mismatch"
    level = LEVEL_BITS[fmt >> 13]
    mask = MASKS[fmt >> 10 & 7]
    assert matrix[size - 8][8], "dark module missing"

    fn = reserved(size, version)
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not fn[r][c]:
                    bits.append(int(matrix[r][c]) ^ int(mask(r, c)))
        upward = not upward
        col -= 2
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]

    ec_len, data_lens = BLOCKS[(version, level)]
    total = sum(data_lens) + ec_len * len(data_lens)
    codewords = codewords[:total]
    blocks = [[] for _ in data_lens]
    i = 0
    for k in range(max(data_lens)):
        for b, n in enumerate(data_lens):
            if k < n:
                blocks[b].append(codewords[i])
                i += 1
    for k in range(ec_len):
        for b in range(len(data_lens)):
            blocks[b].append(codewords[i])
            i += 1
    data = []
    for b, n in enumerate(data_lens):
        assert syndromes(blocks[b], ec_len) == [0] * ec_len, "RS syndromes non-zero"
        data.extend(blocks[b][:n])
    stream = "".join(format(d, "08b") for d in data)
    assert stream[:4] == "0100", "not byte mode"
    length = int(stream[4:12], 2)
    payload = bytes(int(stream[12 + 8 * j:20 + 8 * j], 2) for j in range(length))
    return payload.decode("utf-8"), version, level


def parse(rows):
    return [[ch == "#" for ch in row] for row in rows]


# Apple CoreImage CIQRCodeGenerator, message "keji", correction level M.
REFERENCE_KEJI_M = parse([
    "#######.....#.#######",
    "#.....#...###.#.....#",
    "#.###.#.###...#.###.#",
    "#.###.#.#..#..#.###.#",
    "#.###.#.##.##.#.###.#",
    "#.....#.###.#.#.....#",
    "#######.#.#.#.#######",
    "........#.###........",
    "#.#####.....#.#####..",
    "...###..#...#..#.##.#",
    "###...#.####.#..####.",
    "#...##....#....######",
    "####..#..###.#..#...#",
    "........##.####.....#",
    "#######.....#.##...#.",
    "#.....#.#..####..##..",
    "#.###.#.###.#..#...#.",
    "#.###.#.##..#..#.....",
    "#.###.#.####.#..###..",
    "#.....#...#....##.#..",
    "#######.#..#.#..#.##.",
])


class QrEncodeTest(unittest.TestCase):
    def test_round_trips_through_an_independent_decoder(self):
        for text in ("timetrace", "timetrace://pair?code=ABCD1234&platform=darwin&v=1",
                     "timetrace://pair?code=ABCD1234&name=Alex%E7%9A%84%20MacBook%20Pro&platform=darwin&v=1",
                     "x" * 100):
            with self.subTest(text=text):
                matrix = qr.encode(text)
                decoded, version, level = decode(matrix)
                self.assertEqual(decoded, text)
                self.assertIn(level, ("M", "L"))

    def test_prefers_level_m_and_the_smallest_version(self):
        self.assertEqual(decode(qr.encode("timetrace"))[1:], (1, "M"))
        self.assertEqual(len(qr.encode("a" * 14)), 21)
        self.assertEqual(len(qr.encode("a" * 15)), 25)   # 15 bytes do not fit 1-M
        self.assertEqual(decode(qr.encode("a" * 106))[1:], (6, "M"))
        self.assertEqual(decode(qr.encode("a" * 120))[1:], (6, "L"))

    def test_rejects_payloads_beyond_version_6(self):
        with self.assertRaises(ValueError):
            qr.encode("a" * 135)

    def test_matches_reference_matrix(self):
        self.assertEqual(qr.encode("keji"), REFERENCE_KEJI_M)

    def test_finder_and_timing_patterns(self):
        m = qr.encode("timetrace://pair?code=ABCD1234&platform=darwin&v=1")
        size = len(m)
        finder = parse(["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"])
        for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
            self.assertEqual([row[c0:c0 + 7] for row in m[r0:r0 + 7]], finder)
        for i in range(8, size - 8):
            self.assertEqual(m[6][i], i % 2 == 0)
            self.assertEqual(m[i][6], i % 2 == 0)

    def test_half_block_rendering_packs_two_rows_per_line(self):
        m = [[True, False], [False, True], [True, True]]
        lines = qr.render_half_blocks(m, border=0)
        # light modules are drawn (light-on-dark), dark modules are blank
        self.assertEqual(lines, ["▄▀", "▄▄"])
        bordered = qr.render_half_blocks(qr.encode("timetrace"), border=2)
        self.assertEqual(len(bordered), (21 + 4 + 1) // 2)
        self.assertTrue(all(len(line) == 25 for line in bordered))
        self.assertEqual(bordered[0], "█" * 25)

    def test_dark_rendering_is_the_inverse_and_the_default_quiet_zone_is_four(self):
        m = [[True, False], [False, True], [True, True]]
        # dark modules drawn; the padding row below an odd height stays light
        self.assertEqual(qr.render_half_blocks(m, border=0, dark=True), ["▀▄", "▀▀"])
        lines = qr.render_half_blocks(qr.encode("timetrace"), dark=True)
        self.assertEqual(len(lines), (21 + 8 + 1) // 2)
        self.assertTrue(all(len(line) == 29 for line in lines))
        self.assertEqual(lines[:2], [" " * 29] * 2)
        self.assertEqual(lines[-1], " " * 29)

    def test_levels_can_be_restricted_to_m(self):
        with self.assertRaises(ValueError):
            qr.encode("a" * 120, levels=("M",))
        self.assertEqual(decode(qr.encode("a" * 106, levels=("M",)))[1:], (6, "M"))


if __name__ == "__main__":
    unittest.main()
