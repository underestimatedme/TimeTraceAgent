"""Pairing screen: header box, QR code (half blocks, quiet zone), link and hint."""
import io
import unittest

from timetrace import pairscreen, qr
from tests.test_qr import decode

LINK = "timetrace://pair?code=ABCD1234"

# Snapshot: no ANSI (light modules drawn, for a light-on-dark terminal).
PLAIN = [
    '╭───────────────────────────────────╮',
    '│ 扫码绑定这台电脑                  │',
    '│ 电脑：Test Mac                    │',
    '│ 2 分钟内有效（剩余 1:45）         │',
    '╰───────────────────────────────────╯',
    '█████████████████████████████████████',
    '█████████████████████████████████████',
    '████ ▄▄▄▄▄ █ █▄▄▄ ▄▄▀▀▄█▄█ ▄▄▄▄▄ ████',
    '████ █   █ █▀█ ▀▀█▀▀▄█▄  █ █   █ ████',
    '████ █▄▄▄█ ██▀ ▄ ▀▄▀  █  █ █▄▄▄█ ████',
    '████▄▄▄▄▄▄▄█ █ █▄█▄▀ ▀▄█▄█▄▄▄▄▄▄▄████',
    '████ ▀ ▄▀▄▄ ▀█▄▄  ▀ ██  ▀█ ▀▀ █  ████',
    '████▄█    ▄ █▄ ▀▀▄██ █▀▀ █▀▀ ▄▄ ▀████',
    '█████ ▀ ▀▄▄▀▄▄▀███ ▄▀▀█▀██▀▀█  █ ████',
    '█████▀▄▀  ▄█ ▀▄▀▀▄▀▄ █   █▄ ▄ █▄▄████',
    '██████▄█▄█▄▄▄▄▄▄▄█ ▄ █▀▄ ▄█▄▄ ██▄████',
    '████▄█▄█▄▄▄▀ █ █▄  ▄▄▀ ▀  ▄▀▀▀ ██████',
    '█████▄█▄██▄▄▀██ ▀▄▄▀ ▄█▄ ▄▄▄ ▄▄▄ ████',
    '████ ▄▄▄▄▄ █  ███▀  ███▄ █▄█ ▄▄▄█████',
    '████ █   █ █▀ █▄█▀███  ▀▄ ▄▄ █ ▀▄████',
    '████ █▄▄▄█ █▄ ▀ ▀ ▄▀ ▄█▀  █▀▀▀▄▀▄████',
    '████▄▄▄▄▄▄▄█▄█▄█▄▄██▄███▄█▄████▄█████',
    '█████████████████████████████████████',
    '█████████████████████████████████████',
    '',
    '配对链接（扫不了码时复制到 iPhone 打开）：',
    'timetrace://pair?code=ABCD1234',
    '或在 App 中手动输入授权码：ABCD1234',
    '打开 刻迹 → 我的电脑 → 绑定新电脑',
]

# Snapshot of the QR part with ANSI: dark modules drawn black on white.
ANSI_QR = [
    '\x1b[30;107m                                     \x1b[0m',
    '\x1b[30;107m                                     \x1b[0m',
    '\x1b[30;107m    █▀▀▀▀▀█ █ ▀▀▀█▀▀▄▄▀ ▀ █▀▀▀▀▀█    \x1b[0m',
    '\x1b[30;107m    █ ███ █ ▄ █▄▄ ▄▄▀ ▀██ █ ███ █    \x1b[0m',
    '\x1b[30;107m    █ ▀▀▀ █  ▄█▀█▄▀▄██ ██ █ ▀▀▀ █    \x1b[0m',
    '\x1b[30;107m    ▀▀▀▀▀▀▀ █ █ ▀ ▀▄█▄▀ ▀ ▀▀▀▀▀▀▀    \x1b[0m',
    '\x1b[30;107m    █▄█▀▄▀▀█▄ ▀▀██▄█  ██▄ █▄▄█ ██    \x1b[0m',
    '\x1b[30;107m    ▀ ████▀█ ▀█▄▄▀  █ ▄▄█ ▄▄█▀▀█▄    \x1b[0m',
    '\x1b[30;107m     █▄█▄▀▀▄▀▀▄   █▀▄▄ ▄  ▄▄ ██ █    \x1b[0m',
    '\x1b[30;107m     ▄▀▄██▀ █▄▀▄▄▀▄▀█ ███ ▀█▀█ ▀▀    \x1b[0m',
    '\x1b[30;107m      ▀ ▀ ▀▀▀▀▀▀▀ █▀█ ▄▀█▀ ▀▀█  ▀    \x1b[0m',
    '\x1b[30;107m    ▀ ▀ ▀▀▀▄█ █ ▀██▀▀▄█▄██▀▄▄▄█      \x1b[0m',
    '\x1b[30;107m     ▀ ▀  ▀▀▄  █▄▀▀▄█▀ ▀█▀▀▀█▀▀▀█    \x1b[0m',
    '\x1b[30;107m    █▀▀▀▀▀█ ██   ▄██   ▀█ ▀ █▀▀▀     \x1b[0m',
    '\x1b[30;107m    █ ███ █ ▄█ ▀ ▄   ██▄▀█▀▀█ █▄▀    \x1b[0m',
    '\x1b[30;107m    █ ▀▀▀ █ ▀█▄█▄█▀▄█▀ ▄██ ▄▄▄▀▄▀    \x1b[0m',
    '\x1b[30;107m    ▀▀▀▀▀▀▀ ▀ ▀ ▀▀  ▀   ▀ ▀    ▀     \x1b[0m',
    '\x1b[30;107m                                     \x1b[0m',
    '\x1b[30;107m                                     \x1b[0m',
]

COLOURS = "\x1b[30;107m"
RESET = "\x1b[0m"


def unpack(lines, dark_drawn):
    """Half-block lines -> module rows (True = dark)."""
    rows = []
    for line in lines:
        top = [ch in "█▀" for ch in line]
        bottom = [ch in "█▄" for ch in line]
        rows.append(top if dark_drawn else [not x for x in top])
        rows.append(bottom if dark_drawn else [not x for x in bottom])
    return rows


class PairScreenTest(unittest.TestCase):
    def screen(self, ansi=False, computer="Test Mac", link=LINK, total=120):
        return pairscreen.PairScreen(link, computer, "ABCD1234", total, ansi=ansi)

    def test_plain_snapshot(self):
        self.assertEqual(self.screen().lines(105), PLAIN)

    def test_ansi_snapshot(self):
        lines = self.screen(ansi=True).lines(105)
        self.assertEqual(lines[:5], PLAIN[:5])        # header box has no colour codes
        self.assertEqual(lines[-5:], PLAIN[-5:])      # nor has the footer
        self.assertEqual(lines[5:-5], ANSI_QR)
        for line in ANSI_QR:
            self.assertTrue(line.startswith(COLOURS) and line.endswith(RESET))

    def test_both_renderings_decode_to_the_link(self):
        plain = unpack(PLAIN[5:-5], dark_drawn=False)
        coloured = unpack([l[len(COLOURS):-len(RESET)] for l in ANSI_QR], dark_drawn=True)
        self.assertEqual(plain, coloured)
        q = qr.QUIET_ZONE
        size = len(plain[0]) - 2 * q
        matrix = [row[q:q + size] for row in plain[q:q + size]]
        self.assertEqual(decode(matrix), (LINK, 3, "M"))  # 30 bytes: version 3-M (2-M holds 26)

    def test_quiet_zone_is_four_light_modules_on_every_side(self):
        rows = unpack(PLAIN[5:-5], dark_drawn=False)
        size = len(rows[0]) - 8
        for r in range(4):
            self.assertFalse(any(rows[r]))
            self.assertFalse(any(rows[4 + size + r]))
        for row in rows[4:4 + size]:
            self.assertFalse(any(row[:4]) or any(row[-4:]))
        self.assertTrue(rows[4][4])  # finder pattern corner right after the margin

    def test_dimensions_half_height_and_fit_80_columns(self):
        link = ("timetrace://pair?code=ABCD1234&name=Alex%20%E7%9A%84%20MacBook%20Pro"
                "&exp=1790000600&platform=darwin&v=1")
        screen = self.screen(link=link, computer="Alex 的 MacBook Pro")
        size = len(qr.encode(link, levels=("M",)))
        self.assertEqual(size, 41)  # 103 bytes: version 6, level M
        self.assertEqual(len(screen.qr), (size + 8 + 1) // 2)
        self.assertTrue(all(pairscreen.display_width(l) == size + 8 for l in screen.qr))
        header = screen.lines()[:5]
        self.assertEqual({pairscreen.display_width(l) for l in header}, {size + 8})
        for line in screen.lines():
            if line != link:  # the link itself is printed whole for copy/paste
                self.assertLessEqual(pairscreen.display_width(line), 80, line)

    def test_long_computer_name_is_truncated_inside_the_box(self):
        lines = self.screen(computer="很长的电脑名字" * 10).lines()
        self.assertEqual({pairscreen.display_width(l) for l in lines[:5]}, {pairscreen.display_width(lines[0])})
        self.assertIn("…", lines[2])

    def test_countdown_formatting(self):
        f = pairscreen.countdown_text
        self.assertEqual(f(120, 105), "2 分钟内有效（剩余 1:45）")
        self.assertEqual(f(120, 104.2), "2 分钟内有效（剩余 1:45）")
        self.assertEqual(f(120, 120), "2 分钟内有效（剩余 2:00）")
        self.assertEqual(f(600, 9), "10 分钟内有效（剩余 0:09）")
        self.assertEqual(f(120, -3), "2 分钟内有效（剩余 0:00）")
        self.assertEqual(f(30, 30), "1 分钟内有效（剩余 0:30）")

    def test_countdown_update_rewrites_only_the_countdown_row(self):
        seq = self.screen().update_sequence(59, extra_below=1)
        up = len(PLAIN) - 3 + 1
        self.assertEqual(seq, "\x1b[%dA\r%s\x1b[%dB\r" % (up, PLAIN[3].replace("1:45", "0:59"), up))
        self.assertNotIn("█", seq)

    def test_colour_detection(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        self.assertTrue(pairscreen.color_supported(Tty(), {"TERM": "xterm-256color"}))
        self.assertFalse(pairscreen.color_supported(Tty(), {"TERM": "xterm", "NO_COLOR": "1"}))
        self.assertFalse(pairscreen.color_supported(Tty(), {"TERM": "dumb"}))
        self.assertFalse(pairscreen.color_supported(io.StringIO(), {"TERM": "xterm"}))
        self.assertEqual(pairscreen.black_on_white({"TERM": "xterm-256color"}), "\x1b[38;5;16;48;5;231m")
        self.assertEqual(pairscreen.black_on_white({"TERM": "xterm"}), "\x1b[30;107m")

    def test_display_width_counts_cjk_as_two(self):
        self.assertEqual(pairscreen.display_width("电脑：Mac"), 9)
        self.assertEqual(pairscreen.display_width("（剩余 1:45）"), 13)


class LiveCountdownTest(unittest.TestCase):
    def test_countdown_ticks_every_second_between_polls(self):
        from unittest.mock import patch
        from timetrace import cli

        class Cloud:
            polls = 0

            def poll_device_authorization(self, device_code):
                Cloud.polls += 1
                return {"status": "pending"} if Cloud.polls < 3 else {"status": "expired"}

        clock = [1000.0]
        ticks = []
        with patch("timetrace.cli.time.time", side_effect=lambda: clock[0]), \
             patch("timetrace.cli.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            result = cli._await_pairing(Cloud(), {"device_code": "d", "expires_in": 120, "interval": 3},
                                        {}, None, None, lambda *a: None, tick=ticks.append)
        self.assertIsNone(result)
        self.assertEqual(Cloud.polls, 3)
        self.assertEqual(ticks, [120, 119, 118, 117, 116, 115])


if __name__ == "__main__":
    unittest.main()
