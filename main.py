"""QR Reader —— 透明取景框，拖到二维码上就出链接。

用法:
    start.cmd                        双击启动（无控制台）
    debug.cmd                        带控制台启动，看报错
    python main.py --selftest        自检：抓屏 → 识别 全链路

操作:
    拖动框内任意位置      移动
    拖动四边 / 四角        缩放
    右键                  菜单
    Esc                   退出
    右上角                最小化 / 关闭

识别全自动：框停住就扫，扫到边框变绿并弹出结果条。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import cv2
import numpy as np
from PySide6.QtCore import (
    QLineF,
    QLockFile,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #

HEADER_H = 30                 # 顶栏高度（拖动 + 两个按钮）
EDGE_GRAB = 7                 # 距边缘多少像素内算「拖边框缩放」
MIN_SCAN = 120                # 取景区最小边长
MAX_SCAN = 900

SCAN_ACTIVE_MS = 200          # 刚放下框：抢响应
SCAN_IDLE_MS = 650            # 之后：省电
SCAN_FOUND_MS = 900           # 已经识别到：只留余光
IDLE_BEFORE_SCAN_MS = 120     # 停止移动后多久开始扫
EAGER_MS = 3500               # 这段时间内多试几种算法、翻查分块
TILES_PER_TICK = 2            # 每次扫描额外翻查几块

ANIM_MS = 16                  # 动效帧率 ≈60fps
SWEEP_MS = 1900               # 扫光走完一趟

ACCENT = QColor(56, 189, 248)      # 待识别：蓝
ACCENT_OK = QColor(74, 222, 128)   # 已识别：绿


# --------------------------------------------------------------------------- #
# 屏幕抓取（GDI BitBlt）
# --------------------------------------------------------------------------- #
#
# 不用 Qt 的 grabWindow：整屏抓再裁，一次 80ms 往上，轮询起来光截图就吃掉三分
# 之一核。BitBlt 只抓那块矩形，16ms。
#
# 直接用窗口句柄的物理坐标（GetWindowRect），不碰逻辑坐标换算——高 DPI、分数
# 缩放、多显示器这些坑一次性绕开。这台机器 250% 缩放，逻辑 240 的框物理是 600。

_SRCCOPY = 0x00CC0020
_gdi32 = None
_user32 = None
_wintypes = None
_ctypes = None
_BITMAPINFOHEADER = None

if sys.platform == "win32":
    try:
        import ctypes as _c
        from ctypes import wintypes as _wt

        _ctypes = _c
        _wintypes = _wt
        _user32 = _c.WinDLL("user32", use_last_error=True)
        _gdi32 = _c.WinDLL("gdi32", use_last_error=True)

        class _BMI(_c.Structure):
            _fields_ = [
                ("biSize", _c.c_uint32),
                ("biWidth", _c.c_int32),
                ("biHeight", _c.c_int32),
                ("biPlanes", _c.c_uint16),
                ("biBitCount", _c.c_uint16),
                ("biCompression", _c.c_uint32),
                ("biSizeImage", _c.c_uint32),
                ("biXPelsPerMeter", _c.c_int32),
                ("biYPelsPerMeter", _c.c_int32),
                ("biClrUsed", _c.c_uint32),
                ("biClrImportant", _c.c_uint32),
            ]

        _BITMAPINFOHEADER = _BMI

        _user32.GetDC.argtypes = [_wt.HWND]
        _user32.GetDC.restype = _wt.HDC
        _user32.ReleaseDC.argtypes = [_wt.HWND, _wt.HDC]
        _user32.GetWindowRect.argtypes = [_wt.HWND, _c.c_void_p]
        _user32.GetWindowRect.restype = _wt.BOOL

        _gdi32.CreateCompatibleDC.argtypes = [_wt.HDC]
        _gdi32.CreateCompatibleDC.restype = _wt.HDC
        _gdi32.CreateCompatibleBitmap.argtypes = [_wt.HDC, _c.c_int, _c.c_int]
        _gdi32.CreateCompatibleBitmap.restype = _wt.HBITMAP
        _gdi32.SelectObject.argtypes = [_wt.HDC, _wt.HGDIOBJ]
        _gdi32.SelectObject.restype = _wt.HGDIOBJ
        _gdi32.BitBlt.argtypes = [
            _wt.HDC, _c.c_int, _c.c_int, _c.c_int, _c.c_int,
            _wt.HDC, _c.c_int, _c.c_int, _wt.DWORD,
        ]
        _gdi32.BitBlt.restype = _wt.BOOL
        _gdi32.GetDIBits.argtypes = [
            _wt.HDC, _wt.HBITMAP, _wt.UINT, _wt.UINT,
            _c.c_void_p, _c.c_void_p, _wt.UINT,
        ]
        _gdi32.GetDIBits.restype = _c.c_int
        _gdi32.DeleteObject.argtypes = [_wt.HGDIOBJ]
        _gdi32.DeleteObject.restype = _wt.BOOL
        _gdi32.DeleteDC.argtypes = [_wt.HDC]
        _gdi32.DeleteDC.restype = _wt.BOOL
    except Exception:  # pragma: no cover
        _gdi32 = None
        _user32 = None


def hwnd_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口在屏幕上的物理像素矩形。"""
    if _user32 is None:
        return None
    r = _wintypes.RECT()
    if not _user32.GetWindowRect(_wintypes.HWND(hwnd), _ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    return r.left, r.top, w, h


class _GdiSurface:
    """复用的内存 DC + 位图。

    每帧重建 CreateCompatibleDC / CreateCompatibleBitmap 要十几毫秒，比 BitBlt
    本身还贵。尺寸不变时复用，只在缩放时重建。
    """

    def __init__(self):
        self.hdc = None
        self.hbmp = None
        self.old = None
        self.buf = None
        self.bmi = None
        self.size = (0, 0)

    def ensure(self, hdc_screen, w: int, h: int) -> bool:
        if self.size == (w, h) and self.hdc and self.hbmp and self.buf:
            return True
        self.release()

        self.hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        if not self.hdc:
            return False
        self.hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        if not self.hbmp:
            self.release()
            return False
        self.old = _gdi32.SelectObject(self.hdc, self.hbmp)

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = _ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h          # 负数 = 自上而下，省一次翻转
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0      # BI_RGB

        self.bmi = bmi
        self.buf = _ctypes.create_string_buffer(w * h * 4)
        self.size = (w, h)
        return True

    def release(self):
        if self.hdc and self.old:
            _gdi32.SelectObject(self.hdc, self.old)
        if self.hbmp:
            _gdi32.DeleteObject(self.hbmp)
        if self.hdc:
            _gdi32.DeleteDC(self.hdc)
        self.hdc = self.hbmp = self.old = self.buf = self.bmi = None
        self.size = (0, 0)


_SURFACE: "_GdiSurface | None" = None


def grab_rect(x: int, y: int, w: int, h: int) -> np.ndarray | None:
    """抓屏幕上的一块物理像素矩形，返回 BGR。"""
    global _SURFACE

    if _gdi32 is None or _user32 is None:
        return None
    if w <= 0 or h <= 0:
        return None

    hdc_screen = _user32.GetDC(None)
    if not hdc_screen:
        return None

    try:
        if _SURFACE is None:
            _SURFACE = _GdiSurface()
        if not _SURFACE.ensure(hdc_screen, w, h):
            return None
        if not _gdi32.BitBlt(_SURFACE.hdc, 0, 0, w, h, hdc_screen, x, y, _SRCCOPY):
            _SURFACE.release()
            return None

        got = _gdi32.GetDIBits(
            _SURFACE.hdc, _SURFACE.hbmp, 0, h,
            _ctypes.cast(_SURFACE.buf, _ctypes.c_void_p),
            _ctypes.cast(_ctypes.byref(_SURFACE.bmi), _ctypes.c_void_p),
            0,
        )
        if got == 0:
            _SURFACE.release()
            return None

        bgra = np.frombuffer(_SURFACE.buf, dtype=np.uint8).reshape(h, w, 4)
        # 必须拷一份：buf 下一帧会被覆盖
        return np.ascontiguousarray(bgra[:, :, :3])

    finally:
        _user32.ReleaseDC(None, hdc_screen)


def scan_rect_physical(hwnd: int, top_logical: int,
                       width_logical: int, height_logical: int):
    """窗口里「取景区」那块在屏幕上的物理像素矩形（要减掉顶栏）。"""
    r = hwnd_rect(hwnd)
    if r is None or width_logical <= 0 or height_logical <= 0:
        return None
    x, y, w, h = r
    dpr = h / height_logical
    if dpr <= 0:
        return None
    dy = int(round(top_logical * dpr))
    if h - dy <= 8:
        return None
    return x, y + dy, w, h - dy


def pixmap_to_bgr(pm: QPixmap) -> np.ndarray | None:
    """Qt 抓图的兜底路径。每行有 padding，必须按 bytesPerLine 展平再裁。"""
    if pm is None or pm.isNull():
        return None
    img = pm.toImage().convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None
    try:
        buf = np.frombuffer(img.constBits(), dtype=np.uint8)
        stride = img.bytesPerLine()
        if buf.size < stride * h:
            raise ValueError("short buffer")
        rows = buf[: stride * h].reshape(h, stride)
        return cv2.cvtColor(
            np.ascontiguousarray(rows[:, : w * 3].reshape(h, w, 3)),
            cv2.COLOR_RGB2BGR,
        )
    except Exception:
        tmp = os.path.join(tempfile.gettempdir(), "_qr_grab.png")
        if not img.save(tmp, "PNG"):
            return None
        return cv2.imread(tmp)


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #

_DETECTOR = None
MAX_SIDE = 420   # 超过就先压到这个长度再解码


def _detector() -> "cv2.QRCodeDetector":
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = cv2.QRCodeDetector()
    return _DETECTOR


def decode_gray(gray: np.ndarray, eager: bool = False,
                max_side: int = MAX_SIDE) -> str:
    """从灰度图里读二维码，读不到返回空串。

    第一遍永远是原图。其余变体只在 eager（刚放下框 / 双击）时才试。

    变体是实测选的：1.5x LANCZOS 在四种取景框下全中，2x 和 Otsu 各有一半失手。
    """
    if gray is None or gray.size == 0:
        return ""

    long_side = max(gray.shape[:2])
    if long_side > max_side:
        s = max_side / long_side
        gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        long_side = max_side

    det = _detector()
    variants = [gray]
    if long_side < 320 or eager:
        variants.append(cv2.resize(gray, None, fx=1.5, fy=1.5,
                                   interpolation=cv2.INTER_LANCZOS4))
    if eager:
        h, w = gray.shape[:2]
        variants.append(gray[int(h * .05):int(h * .95), int(w * .05):int(w * .95)])
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(otsu)

    for img in variants:
        try:
            text, _pts, _ = det.detectAndDecode(img)
        except cv2.error:
            continue
        text = (text or "").strip()
        if plausible(text):
            return text
    return ""


def plausible(text: str) -> bool:
    """粗略过滤：太短、或带不可打印字符的，一律当没读到。

    半张码有时会解出一两个乱字节，这种要挡掉。注意不能靠「连续读两次结果一致」
    来判断——同一次输入解出来的结果本来就是确定的，读两遍必然一样，什么也筛不掉。
    """
    if len(text) < 2:
        return False
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


TILE_MAX_SIDE = 460   # 单块解码上限：再大就可能花掉上百毫秒，卡住界面


def tile_rects(shape):
    """把画面切成互相重叠的小块，先中间、后边上、再四角返回。

    为什么要切块：QRCodeDetector 在二维码只占画面一小块时，连定位都失败
    （detect() 直接返回 False），这是「框必须和码差不多大」的根因。切成小块后
    小码在块里依然占满，就能认了。

    块的大小和数量按画面尺寸自适应：
      - 每块不超过 TILE_MAX_SIDE，避免解码耗时失控（实测大图可能上百毫秒）
      - 块数和步长保证重叠至少一半，这样边长不超过 (块-步长) 的码一定落在某块里
    """
    h, w = shape[:2]
    long_side = max(h, w)
    side = min(0.55 * long_side, float(TILE_MAX_SIDE))
    tw, th = int(min(w, side)), int(min(h, side))
    if tw < 60 or th < 60:
        return []

    if long_side <= 780:
        grid = 3
    elif long_side <= 1200:
        grid = 4
    else:
        grid = 5

    step_x = max(1, (w - tw) // (grid - 1))
    step_y = max(1, (h - th) // (grid - 1))

    boxes = []
    for gy in range(grid):
        for gx in range(grid):
            x0 = min(gx * step_x, w - tw)
            y0 = min(gy * step_y, h - th)
            boxes.append((x0, y0, x0 + tw, y0 + th))

    boxes = list(dict.fromkeys(boxes))   # 贴边时会有重复块
    cx, cy = w / 2, h / 2
    boxes.sort(key=lambda b: ((b[0] + b[2]) / 2 - cx) ** 2 + ((b[1] + b[3]) / 2 - cy) ** 2)
    return boxes


def scan_once(rect, eager: bool, prev_sig, tile_cursor: int) -> dict | None:
    """抓一次屏并识别。纯函数，可以扔到工作线程里跑。

    放在这里而不是 ScanFrame 里，是因为它必须能在非界面线程执行：解码一次要
    15ms，翻块再多要 30ms 往上，跑在界面线程里动效就会一顿一顿的。
    """
    frame = grab_rect(*rect)
    if frame is None or not frame.size:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sig = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.int16)
    changed = prev_sig is None or float(np.mean(np.abs(sig - prev_sig))) >= 1.0

    out = {"sig": sig, "changed": changed, "text": "", "cursor": tile_cursor}
    if not changed and not eager:
        return out

    # 1) 整帧：码够大时这一步就够
    text = decode_gray(gray, eager=eager)
    cursor = tile_cursor

    # 2) 分块：码比框小很多时，只有切块才认得出来。
    #    每次只翻几块、按顺序轮转，保证单次开销有上限。
    if not text:
        tiles = tile_rects(gray.shape)
        if tiles:
            for _ in range(TILES_PER_TICK):
                x0, y0, x1, y1 = tiles[cursor % len(tiles)]
                cursor = (cursor + 1) % len(tiles)
                text = decode_gray(gray[y0:y1, x0:x1], max_side=TILE_MAX_SIDE)
                if text:
                    break

    out["text"] = text
    out["cursor"] = cursor
    return out


class _ScanBridge(QObject):
    """工作线程 → 界面线程的通道。信号跨线程是队列投递，安全。"""

    finished = Signal(object)


class _ScanJob(QRunnable):
    def __init__(self, rect, eager, prev_sig, cursor, token, emit):
        super().__init__()
        self._rect = rect
        self._eager = eager
        self._prev = prev_sig
        self._cursor = cursor
        self._token = token
        self._emit = emit

    def run(self):
        try:
            res = scan_once(self._rect, self._eager, self._prev, self._cursor)
        except Exception:
            res = None
        self._emit((self._token, res))


# --------------------------------------------------------------------------- #
# 结果条
# --------------------------------------------------------------------------- #

class ResultBar(QFrame):
    """贴着取景框的结果条。"""

    def __init__(self, owner: "ScanFrame"):
        super().__init__(None)
        self._owner = owner
        self.text = ""

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("bar")
        self.setStyleSheet(
            "QFrame#bar { background: #1a1a1e; border: 1px solid #34343c;"
            " border-radius: 10px; }"
            "QLabel { color: #e8e8ea; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 11, 14, 12)
        outer.setSpacing(7)

        self.tag = QLabel("链接", self)
        self.tag.setStyleSheet("color: #8b8b93; font-size: 11px;")
        outer.addWidget(self.tag)

        self.body = QLabel("", self)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setStyleSheet("font-size: 13px;")
        self.body.setMaximumWidth(340)
        outer.addWidget(self.body)

        row = QHBoxLayout()
        row.setSpacing(8)
        for name, slot in (("复制", self.copy), ("打开", self._open), ("关闭", self.hide)):
            b = QPushButton(name, self)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(
                "QPushButton { background: #2e2e36; color: #e8e8ea; border: 0;"
                " border-radius: 6px; padding: 6px 16px; font-size: 12px; }"
                "QPushButton:hover { background: #3c3c46; }"
                "QPushButton:pressed { background: #26262c; }"
            )
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        outer.addLayout(row)

    def show_text(self, text: str):
        self.text = text
        is_url = text.lower().startswith(("http://", "https://"))
        self.tag.setText("链接" if is_url else "文本")
        self.body.setText(text if len(text) <= 400 else text[:397] + "...")
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()

    def _place(self):
        """贴着取景框放：右边优先，右边没地方放左边，再不行放下面、上面。

        每次都按框当前位置重算，所以框拖到屏幕边缘时它会自动换边。
        """
        box = self._owner.frameGeometry()
        screen = QGuiApplication.screenAt(box.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        w, h = self.width(), self.height()
        gap = 10

        if box.right() + gap + w <= avail.right():
            x, y = box.right() + gap, box.top()          # 右
        elif box.left() - gap - w >= avail.left():
            x, y = box.left() - gap - w, box.top()       # 左
        elif box.bottom() + gap + h <= avail.bottom():
            x, y = box.left(), box.bottom() + gap        # 下
        else:
            x, y = box.left(), box.top() - gap - h       # 上

        x = max(avail.left() + 4, min(x, avail.right() - w - 4))
        y = max(avail.top() + 4, min(y, avail.bottom() - h - 4))
        self.move(int(x), int(y))

    def follow(self):
        """框动了，跟过去。"""
        if self.isVisible():
            self._place()

    def copy(self):
        QGuiApplication.clipboard().setText(self.text)
        self.tag.setText("已复制")

    def _open(self):
        if self.text.lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(self.text))


# --------------------------------------------------------------------------- #
# 取景框
# --------------------------------------------------------------------------- #

class ScanFrame(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(MIN_SCAN, MIN_SCAN + HEADER_H)
        self.setMouseTracking(True)
        self.resize(260, 260 + HEADER_H)

        self._found = ""
        self._sig: np.ndarray | None = None
        self._moved_at = time.time()
        self._last_geo = QRect()
        self._flash_until = 0.0
        self._tile_cursor = 0
        self._sweep_from = time.time()
        self._drag_mode = None           # None / "move" / "resize"（系统拖动失败时的兜底）
        self._drag_edges = Qt.Edges()
        self._drag_origin = QPoint()
        self._drag_geo = QRect()
        self.bar = ResultBar(self)

        # 扫描放到工作线程：解码 15ms、翻块更久，跑在界面线程里动效会一顿一顿的。
        # 抓屏用的内存 DC 是复用的，所以线程池只留一个线程，串行执行。
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._busy = False
        self._token = 0
        self._bridge = _ScanBridge(self)
        self._bridge.finished.connect(self._on_scanned)

        self.btn_min = self._make_button("\u2212", "最小化", self._minimize)
        self.btn_close = self._make_button("\u2715", "关闭", self._close)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(SCAN_ACTIVE_MS)

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._animate)
        self._anim.start(ANIM_MS)

        QShortcut(QKeySequence("Escape"), self, self._close)
        QShortcut(QKeySequence("Ctrl+C"), self, self.bar.copy)

        screen = QGuiApplication.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            self.move(g.center() - self.rect().center())

    # ---------------- 部件 ----------------

    def _make_button(self, glyph: str, tip: str, slot) -> QPushButton:
        b = QPushButton(glyph, self)
        b.setToolTip(tip)
        b.setCursor(Qt.ArrowCursor)
        b.setFixedSize(24, 20)
        b.setFocusPolicy(Qt.NoFocus)
        b.setStyleSheet(
            "QPushButton { background: transparent; color: #9a9aa4; border: 0;"
            " border-radius: 5px; font-size: 12px; padding-bottom: 2px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.14); color: #fff; }"
            "QPushButton:pressed { background: rgba(255,255,255,0.24); }"
        )
        b.clicked.connect(slot)
        return b

    def _scan_rect(self) -> QRect:
        return QRect(0, HEADER_H, self.width(), max(1, self.height() - HEADER_H))

    def resizeEvent(self, e):
        y = (HEADER_H - 20) // 2
        self.btn_close.move(self.width() - 24 - 6, y)
        self.btn_min.move(self.width() - 24 * 2 - 8, y)
        self.bar.follow()          # 框变了大小，结果条跟着换位置
        super().resizeEvent(e)

    def moveEvent(self, e):
        # 结果条是独立窗口（要能画到框外面去），所以得手动跟着框走
        self.bar.follow()
        super().moveEvent(e)

    # ---------------- 绘制 ----------------

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        sr = self._scan_rect()
        found = time.time() < self._flash_until

        # 顶栏
        head = QPainterPath()
        head.moveTo(0.5, HEADER_H)
        head.lineTo(0.5, 9)
        head.quadTo(0.5, 0.5, 9, 0.5)
        head.lineTo(w - 9, 0.5)
        head.quadTo(w - 0.5, 0.5, w - 0.5, 9)
        head.lineTo(w - 0.5, HEADER_H)
        head.closeSubpath()
        p.fillPath(head, QColor(22, 22, 26))
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))
        p.drawLine(0, HEADER_H, w, HEADER_H)

        # 左上的小二维码图标 + 文字
        ix, iy = 10, (HEADER_H - 12) // 2
        p.setPen(QPen(QColor(120, 190, 240), 1.4))
        p.drawRoundedRect(QRectF(ix, iy, 12, 12), 2, 2)
        p.fillRect(QRectF(ix + 2.5, iy + 2.5, 3, 3), QColor(120, 190, 240))
        p.fillRect(QRectF(ix + 6.5, iy + 6.5, 3, 3), QColor(120, 190, 240))
        p.setPen(QColor(168, 168, 178))
        p.setFont(QFont("Microsoft YaHei UI", 8))
        p.drawText(QRect(ix + 19, 0, 110, HEADER_H),
                   Qt.AlignVCenter | Qt.AlignLeft, "扫码")

        # 取景区底层：alpha=1 的白。
        # 这一层是关键：分层窗口在完全透明处会把鼠标事件透给下面的程序，
        # 不填这一层，框中间就点不动，只能抓住边框拖。
        p.fillRect(sr, QColor(255, 255, 255, 1))

        color = ACCENT_OK if found else ACCENT

        # 扫光：一道亮线带着上方渐隐的辉光，由上往下走。
        # 注意透明度必须压得很低——扫光画在码上面，而截屏会把这一层一起抓进去，
        # 带子太重就等于给二维码抹了一道亮条，识别率会掉。这里靠实测定的值。
        if not found:
            phase = ((time.time() - self._sweep_from) * 1000 % SWEEP_MS) / SWEEP_MS
            band = max(22.0, sr.height() * 0.13)
            y = sr.top() + phase * (sr.height() + band) - band * 0.5
            p.save()
            p.setClipRect(sr)
            grad = QLinearGradient(0, y - band, 0, y)
            grad.setColorAt(0.0, QColor(90, 200, 255, 0))
            grad.setColorAt(0.70, QColor(90, 200, 255, 16))
            grad.setColorAt(1.0, QColor(150, 232, 255, 44))
            p.fillRect(QRectF(sr.left(), y - band, sr.width(), band), grad)
            p.setPen(QPen(QColor(190, 244, 255, 130), 1.2))
            p.drawLine(QLineF(float(sr.left()), y, float(sr.right()), y))
            p.restore()

        # 外框
        r = QRectF(sr).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(0, 0, 0, 120), 5))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QPen(color, 2))
        p.drawRoundedRect(r, 4, 4)

        # 四角加粗
        arm = max(16.0, min(r.width(), r.height()) * 0.17)
        p.setPen(QPen(color, 4, Qt.SolidLine, Qt.RoundCap))
        for cx, cy, dx, dy in (
            (r.left(), r.top(), 1, 1),
            (r.right(), r.top(), -1, 1),
            (r.left(), r.bottom(), 1, -1),
            (r.right(), r.bottom(), -1, -1),
        ):
            p.drawLine(QPoint(int(cx), int(cy)), QPoint(int(cx + dx * arm), int(cy)))
            p.drawLine(QPoint(int(cx), int(cy)), QPoint(int(cx), int(cy + dy * arm)))

        p.end()

    def _animate(self):
        if not self.isVisible() or self.isMinimized():
            return
        # 只刷取景区：顶栏、按钮没变，全窗口重绘是白开销
        self.update(self._scan_rect())

    # ---------------- 拖动 / 缩放 ----------------

    def _edges_at(self, pos: QPoint) -> Qt.Edges:
        e = Qt.Edges()
        if pos.x() <= EDGE_GRAB:
            e |= Qt.LeftEdge
        elif pos.x() >= self.width() - EDGE_GRAB:
            e |= Qt.RightEdge
        if pos.y() <= EDGE_GRAB:
            e |= Qt.TopEdge
        elif pos.y() >= self.height() - EDGE_GRAB:
            e |= Qt.BottomEdge
        return e

    def _cursor_for(self, edges: Qt.Edges):
        l = bool(edges & Qt.LeftEdge)
        r = bool(edges & Qt.RightEdge)
        t = bool(edges & Qt.TopEdge)
        b = bool(edges & Qt.BottomEdge)
        if (l and t) or (r and b):
            return Qt.SizeFDiagCursor
        if (r and t) or (l and b):
            return Qt.SizeBDiagCursor
        if l or r:
            return Qt.SizeHorCursor
        if t or b:
            return Qt.SizeVerCursor
        return Qt.SizeAllCursor

    def mouseMoveEvent(self, e):
        if self._drag_mode:
            d = e.globalPosition().toPoint() - self._drag_origin
            g = QRect(self._drag_geo)
            if self._drag_mode == "move":
                self.move(g.topLeft() + d)
            else:
                eg = self._drag_edges
                if eg & Qt.LeftEdge:
                    g.setLeft(g.left() + d.x())
                if eg & Qt.RightEdge:
                    g.setRight(g.right() + d.x())
                if eg & Qt.TopEdge:
                    g.setTop(g.top() + d.y())
                if eg & Qt.BottomEdge:
                    g.setBottom(g.bottom() + d.y())
                min_h = MIN_SCAN + HEADER_H
                if g.width() < MIN_SCAN:
                    if eg & Qt.LeftEdge:
                        g.setLeft(g.right() - MIN_SCAN)
                    else:
                        g.setRight(g.left() + MIN_SCAN)
                if g.height() < min_h:
                    if eg & Qt.TopEdge:
                        g.setTop(g.bottom() - min_h)
                    else:
                        g.setBottom(g.top() + min_h)
                self.setGeometry(g)
            return
        self.setCursor(self._cursor_for(self._edges_at(e.position().toPoint())))

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        edges = self._edges_at(e.position().toPoint())
        wh = self.windowHandle()

        # 优先交给系统：原生拖动/缩放，跟普通窗口手感一致
        if wh is not None:
            if edges and wh.startSystemResize(edges):
                e.accept()
                return
            if not edges and wh.startSystemMove():
                e.accept()
                return

        # 系统调用不可用时自己算，保证一定拖得动
        self._drag_mode = "resize" if edges else "move"
        self._drag_edges = edges
        self._drag_origin = e.globalPosition().toPoint()
        self._drag_geo = self.geometry()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_mode:
            self._drag_mode = None
            self._sig = None
            self._tile_cursor = 0
            self._moved_at = time.time()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, _e):
        self._reset_and_scan()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #232327; color: #e8e8ea; border: 1px solid #3a3a40;"
            " padding: 4px; }"
            "QMenu::item { padding: 6px 22px; border-radius: 4px; }"
            "QMenu::item:selected { background: #3a3a44; }"
        )
        act_copy = menu.addAction("复制上次结果")
        act_rescan = menu.addAction("重新识别")
        menu.addSeparator()
        act_small = menu.addAction("缩小")
        act_big = menu.addAction("放大")
        menu.addSeparator()
        act_min = menu.addAction("最小化")
        act_quit = menu.addAction("退出")
        act_copy.setEnabled(bool(self.bar.text))

        chosen = menu.exec(e.globalPos())
        if chosen == act_copy:
            self.bar.copy()
        elif chosen == act_rescan:
            self._reset_and_scan()
        elif chosen == act_small:
            self._resize_by(-40)
        elif chosen == act_big:
            self._resize_by(40)
        elif chosen == act_min:
            self._minimize()
        elif chosen == act_quit:
            self._close()

    def _reset_and_scan(self):
        self._found = ""
        self._tile_cursor = 0
        self._sig = None
        self._moved_at = time.time()
        self._scan(force=True)

    def _resize_by(self, delta: int):
        side = max(MIN_SCAN, min(MAX_SCAN, self.width() + delta))
        center = self.frameGeometry().center()
        self.resize(side, side + HEADER_H)
        self.move(center - QPoint(side // 2, (side + HEADER_H) // 2))
        self._sig = None
        self._tile_cursor = 0

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._close()
        else:
            super().keyPressEvent(e)

    def _minimize(self):
        self.bar.hide()
        self.showMinimized()

    def _close(self):
        self.bar.close()
        self.close()
        QApplication.quit()

    # ---------------- 扫描 ----------------

    def _capture(self) -> np.ndarray | None:
        rect = scan_rect_physical(int(self.winId()), HEADER_H, self.width(), self.height())
        if rect is not None:
            frame = grab_rect(*rect)
            if frame is not None and frame.size:
                return frame

        # 非 Windows / GDI 失败时退回 Qt 抓屏
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        if screen is None:
            return None
        full = screen.grabWindow(0)
        if full.isNull():
            return None
        sg, geo = screen.geometry(), self.frameGeometry()
        dpr = full.width() / max(1, sg.width())
        x = max(0, int((geo.x() - sg.x()) * dpr))
        y = max(0, int((geo.y() - sg.y() + HEADER_H) * dpr))
        w = min(int(geo.width() * dpr), full.width() - x)
        h = min(int((geo.height() - HEADER_H) * dpr), full.height() - y)
        if w <= 0 or h <= 0:
            return None
        return pixmap_to_bgr(full.copy(x, y, w, h))

    def _tick(self):
        if not self.isVisible() or self.isMinimized():
            return

        geo = self.frameGeometry()
        if geo != self._last_geo:
            # 框动了 = 换了个地方看。之前识别到的内容作废，重新开始扫，
            # 否则同一个码再扫一次不会再弹。
            self._last_geo = geo
            self._moved_at = time.time()
            self._sig = None
            self._tile_cursor = 0
            self._found = ""
            self._token += 1          # 让还在跑的那次结果直接作废
            self._timer.setInterval(SCAN_ACTIVE_MS)

        idle_ms = (time.time() - self._moved_at) * 1000
        if idle_ms < IDLE_BEFORE_SCAN_MS:
            return
        if self._busy:
            return                     # 上一次还没扫完，别叠上去

        eager = idle_ms < EAGER_MS
        if self._found and not eager:
            target = SCAN_FOUND_MS
        elif eager:
            target = SCAN_ACTIVE_MS
        else:
            target = SCAN_IDLE_MS
        if self._timer.interval() != target:
            self._timer.setInterval(target)

        rect = scan_rect_physical(int(self.winId()), HEADER_H,
                                  self.width(), self.height())
        if rect is None:
            return
        self._busy = True
        self._pool.start(_ScanJob(rect, eager, self._sig, self._tile_cursor,
                                  self._token, self._bridge.finished.emit))

    def _on_scanned(self, payload):
        self._busy = False
        token, res = payload
        if token != self._token or res is None:
            return
        self._sig = res["sig"]
        self._tile_cursor = res["cursor"]
        text = res["text"]
        if not text:
            return
        if text == self._found:
            return
        self._found = text
        self._flash_until = time.time() + 1.4
        self.update()
        self.bar.show_text(text)

    def _scan(self, force: bool = False, eager: bool = False) -> str:
        """同步版，自检用。正常运行时走 _tick 的工作线程。"""
        if self.isMinimized() or not self.isVisible():
            return ""
        rect = scan_rect_physical(int(self.winId()), HEADER_H,
                                  self.width(), self.height())
        if rect is None:
            return ""
        res = scan_once(rect, eager or force, None if force else self._sig,
                        self._tile_cursor)
        if res is None:
            return ""
        self._sig = res["sig"]
        self._tile_cursor = res["cursor"]
        text = res["text"]
        if not text:
            return ""
        if text == self._found and not force:
            return text
        self._found = text
        self._flash_until = time.time() + 1.4
        self.update()
        self.bar.show_text(text)
        return text

    def closeEvent(self, e):
        self._token += 1                  # 让在跑的扫描结果作废
        self._pool.waitForDone(1500)      # 等线程收尾，避免退出时抓屏缓冲被拆掉
        self.bar.close()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# 图标 / 自检 / 入口
# --------------------------------------------------------------------------- #

def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(ACCENT, 5, Qt.SolidLine, Qt.RoundCap))
    p.drawRoundedRect(QRectF(9, 9, 46, 46), 10, 10)
    p.fillRect(QRectF(24, 24, 7, 7), ACCENT)
    p.fillRect(QRectF(35, 35, 7, 7), ACCENT)
    p.end()
    return QIcon(pm)


def _qr_pixmap(text: str, px: int) -> QPixmap:
    enc = cv2.QRCodeEncoder_create()
    qr = enc.encode(text)
    qr = cv2.resize(qr, (px, px), interpolation=cv2.INTER_NEAREST)
    if qr.ndim == 2:
        qr = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    qr = cv2.copyMakeBorder(qr, 20, 20, 20, 20, cv2.BORDER_CONSTANT,
                            value=(255, 255, 255))
    rgb = cv2.cvtColor(qr, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(img)


def selftest() -> int:
    """端到端自检：开假网页窗口放二维码，把取景框盖上去，真实抓屏解码。"""
    app = QApplication(sys.argv)

    target = "https://hlmdf.github.io/books/"
    page = QWidget()
    # 必须置顶：否则桌面上任何一个前台窗口都可能盖在假页面上，抓到的就是别人的画面
    page.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    page.setStyleSheet("background: #f7f7f5;")
    page.resize(520, 420)
    page.move(420, 280)
    lab = QLabel(page)
    lab.setPixmap(_qr_pixmap(target, 180))
    lab.move(170, 120)
    page.show()
    page.raise_()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)

    win = ScanFrame()
    win._timer.stop()
    win._anim.stop()
    win.bar.hide()
    win.resize(260, 260 + HEADER_H)
    g = page.frameGeometry()
    win.move(g.center().x() - 130, g.center().y() - 130 - HEADER_H // 2)
    win.show()
    win.raise_()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)

    frame = win._capture()
    print(f"抓取区域: {None if frame is None else frame.shape}")

    g_ms, d_ms = [], []
    for _ in range(15):
        t0 = time.perf_counter()
        f = win._capture()
        t1 = time.perf_counter()
        decode_gray(f)
        t2 = time.perf_counter()
        g_ms.append((t1 - t0) * 1000)
        d_ms.append((t2 - t1) * 1000)

    win._sig = None
    win._scan(force=True)
    scans = []
    for _ in range(10):
        t0 = time.perf_counter()
        win._scan()
        scans.append((time.perf_counter() - t0) * 1000)

    print(f"截图 {np.mean(g_ms):.1f} ms | 解码 {np.mean(d_ms):.1f} ms | "
          f"画面没变时 {np.mean(scans):.1f} ms")

    text = win.bar.text
    ok = text == target
    print(f"识别结果: {text!r}")
    print("自检", "通过 (OK)" if ok else "失败 (FAIL)")

    win.bar.close()
    win.close()
    page.close()
    return 0 if ok else 1


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("QR Reader")
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(os.path.join(tempfile.gettempdir(), "qr-reader.lock"))
    if not lock.tryLock(200):
        print("QR Reader 已经在运行了")
        return

    win = ScanFrame()
    win.show()

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("QR Reader —— 拖到二维码上")
    tm = QMenu()
    tm.addAction("显示取景框",
                 lambda: (win.showNormal(), win.raise_(), win.activateWindow()))
    tm.addSeparator()
    tm.addAction("退出", win._close)
    tray.setContextMenu(tm)

    def on_tray(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            win.showNormal()
            win.raise_()
            win.activateWindow()

    tray.activated.connect(on_tray)
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
