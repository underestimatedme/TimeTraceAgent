"""The pairing screen `timetrace cloud login` / `setup` print in the terminal.

    ╭───────────────────────────────────────────────╮
    │  扫码绑定这台电脑                             │
    │  电脑：Alex 的 Mac                            │
    │  2 分钟内有效（剩余 1:45）                    │
    ╰───────────────────────────────────────────────╯
    <QR code, half blocks, 4-module quiet zone>
    配对链接（扫不了码时复制到 iPhone 打开）：
    timetrace://pair?...

On a colour terminal the code is drawn black-on-white whatever the theme; the
countdown line is rewritten in place (cursor up / down) without reprinting
the code.
"""
import os
import sys
import unicodedata
from urllib.parse import parse_qs, urlsplit
from typing import List, Mapping, Optional

from timetrace import qr

RESET = "\x1b[0m"
MAX_WIDTH = 78  # fits an 80-column terminal
TITLE = "扫码绑定这台电脑"
HINT = "打开 刻迹 → 我的电脑 → 绑定新电脑"


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        if used + char_width(ch) > width - 1:
            break
        out += ch
        used += char_width(ch)
    return out + "…"


def countdown_text(total_seconds: int, remaining_seconds: float) -> str:
    """「2 分钟内有效（剩余 1:45）」"""
    minutes = max(1, int(total_seconds) // 60)
    left = max(0, int(remaining_seconds + 0.999))  # 1:44.2 left still reads 1:45
    return "%d 分钟内有效（剩余 %d:%02d）" % (minutes, left // 60, left % 60)


def color_supported(stream=None, environ: Optional[Mapping[str, str]] = None) -> bool:
    """ANSI colours: only on a TTY, never with NO_COLOR (no-color.org) or TERM=dumb."""
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    if environ.get("NO_COLOR"):
        return False
    if environ.get("TERM", "") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def black_on_white(environ: Optional[Mapping[str, str]] = None) -> str:
    """Pure black on pure white. The 256-colour cube is not remapped by
    terminal themes the way the 16 basic colours often are."""
    environ = os.environ if environ is None else environ
    if "256" in environ.get("TERM", "") or environ.get("COLORTERM"):
        return "\x1b[38;5;16;48;5;231m"
    return "\x1b[30;107m"


def encode(link: str) -> qr.Matrix:
    """Level M, the smallest version it fits (L only for a link M cannot hold)."""
    try:
        return qr.encode(link, levels=("M",))
    except ValueError:
        return qr.encode(link)


def qr_lines(matrix: qr.Matrix, ansi: bool, colours: str = "\x1b[30;107m") -> List[str]:
    """The code as text lines. With ANSI the dark modules are drawn in black on
    a white background; without, the light modules are drawn (a light-on-dark
    terminal then shows dark modules on light, which is what a camera needs)."""
    if not ansi:
        return qr.render_half_blocks(matrix, border=qr.QUIET_ZONE, dark=False)
    return [colours + line + RESET for line in qr.render_half_blocks(matrix, border=qr.QUIET_ZONE, dark=True)]


class PairScreen:
    """Lines of one pairing screen plus in-place countdown updates."""

    def __init__(self, link: str, computer: str, user_code: str, total_seconds: int,
                 ansi: bool = False, colours: str = "\x1b[30;107m"):
        self.link, self.user_code, self.total = link, user_code, int(total_seconds)
        matrix = encode(link)
        self.qr = qr_lines(matrix, ansi, colours)
        qr_width = len(matrix) + 2 * qr.QUIET_ZONE
        self.inner = min(MAX_WIDTH - 4, max(qr_width - 4, display_width(TITLE),
                                            display_width(countdown_text(self.total, self.total)) + 2))
        self.computer = truncate("电脑：" + computer, self.inner)
        self.countdown_row = 3

    def _row(self, text: str) -> str:
        return "│ " + text + " " * (self.inner - display_width(text)) + " │"

    def countdown_row_text(self, remaining_seconds: float) -> str:
        return self._row(countdown_text(self.total, remaining_seconds))

    def lines(self, remaining_seconds: Optional[float] = None) -> List[str]:
        phone_pair = parse_qs(urlsplit(self.link).query).get('v') == ['2']
        remaining = self.total if remaining_seconds is None else remaining_seconds
        header = [
            "╭" + "─" * (self.inner + 2) + "╮",
            self._row(TITLE),
            self._row(self.computer),
            self.countdown_row_text(remaining),
            "╰" + "─" * (self.inner + 2) + "╯",
        ]
        footer = [
            "",
            "配对链接（用于刻迹 App 内扫码）：" if phone_pair else "配对链接（扫不了码时复制到 iPhone 打开）：",
            self.link,
            "或在 App 中手动输入授权码：%s" % self.user_code,
            "打开 刻迹 → AI → 绑定电脑" if phone_pair else HINT,
        ]
        return header + self.qr + footer

    def rows_below_countdown(self) -> int:
        """Lines between the countdown and the cursor once lines() is printed."""
        return len(self.lines()) - self.countdown_row

    def update_sequence(self, remaining_seconds: float, extra_below: int = 0) -> str:
        """Escape sequence rewriting the countdown in place, leaving the cursor
        where it was (at the start of the line after the screen)."""
        up = self.rows_below_countdown() + extra_below
        return "\x1b[%dA\r%s\x1b[%dB\r" % (up, self.countdown_row_text(remaining_seconds), up)
