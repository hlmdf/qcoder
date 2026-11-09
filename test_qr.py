"""离线验证识别链路：生成不同尺寸的二维码，贴到模拟屏幕上，按主程序的方式解码。

直接调用 main.decode_gray，测的就是实际会跑的那段代码。
    .venv/Scripts/python.exe test_qr.py

三种结果：
    直接命中   第一遍原图就认出来了（屏幕静止时走的就是这条路）
    需要重试   原图不行，靠 1.5x / 居中裁切 / 二值化 才认出来（刚放下小框时走这条路）
    失败       全都认不出
"""
import cv2
import numpy as np

from main import TILE_MAX_SIDE, decode_gray, tile_rects

TARGETS = [
    "https://hlmdf.github.io/books/",
    "https://example.com/a/very/long/path?with=query&and=more",
    "微信支付: https://pay.example.cn/x?c=1",
    "纯文本内容测试 中文也要能出来",
]


def make_qr(text: str, px: int) -> np.ndarray:
    enc = cv2.QRCodeEncoder_create()
    qr = enc.encode(text)
    if qr is None or qr.size == 0:
        raise RuntimeError("encode failed")
    qr = cv2.resize(qr, (px, px), interpolation=cv2.INTER_NEAREST)
    if qr.ndim == 2:
        qr = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    elif qr.shape[2] == 1:
        qr = cv2.cvtColor(qr[:, :, 0], cv2.COLOR_GRAY2BGR)
    return cv2.copyMakeBorder(qr, 8, 8, 8, 8, cv2.BORDER_CONSTANT,
                              value=(255, 255, 255))


def fake_screen() -> np.ndarray:
    """造一张 2200x1400 的浅灰"网页"，带点文字噪声。"""
    canvas = np.full((1400, 2200, 3), 246, np.uint8)
    rng = np.random.default_rng(7)
    for y in range(60, 1360, 26):
        w = int(rng.integers(200, 900))
        canvas[y:y + 9, 120:120 + w] = 90
    return canvas


def crop_around(canvas: np.ndarray, qr: np.ndarray, side: int) -> np.ndarray:
    """把二维码居中贴到画布上，再取一块 side×side 的取景框。"""
    cy, cx = 700, 1100
    qh, qw = qr.shape[:2]
    y0, x0 = cy - qh // 2, cx - qw // 2
    canvas[y0:y0 + qh, x0:x0 + qw] = qr
    half = side // 2
    return canvas[cy - half:cy - half + side, cx - half:cx - half + side]


def check(text: str, crop: np.ndarray) -> str:
    # 主程序在 _scan 里先转灰度再调 decode_gray，这里必须一致
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    if decode_gray(gray) == text:
        return "直接命中"
    if decode_gray(gray, eager=True) == text:
        return "需要重试"
    return "失败"


def main() -> int:
    rows = []
    stats = {"直接命中": 0, "需要重试": 0, "失败": 0}

    # 常规：逻辑 240~356 的取景框
    for text in TARGETS:
        for px in (110, 150, 200, 300):
            side = max(240, px + 56)
            crop = crop_around(fake_screen(), make_qr(text, px), side)
            r = check(text, crop)
            stats[r] += 1
            rows.append((f"{px}px", f"框{side}", text, r))

    # 高 DPI：250% 缩放下逻辑 240 的框物理上是 600，解码前会被压到 420
    for text in TARGETS:
        for side in (600, 750):
            crop = crop_around(fake_screen(), make_qr(text, int(side * 0.62)), side)
            r = check(text, crop)
            stats[r] += 1
            rows.append(("高DPI", f"框{side}", text, r))

    for px, side, text, r in rows:
        mark = {"直接命中": "✓", "需要重试": "~", "失败": "✗"}[r]
        print(f"{mark} {px:>6} {side:<8} {r:<5} {text[:30]}")

    # 尺寸容差：码比框小很多时，整帧识别不出来，必须靠分块
    print("\n尺寸容差（码在框里的占比）")
    print(f"{'框':>5} {'码':>5} {'占比':>6} {'整帧':>5} {'分块':>5}")
    tolerance = []
    for side in (650, 1100):
        for qpx in (260, 180, 140, 110, 90):
            if qpx >= side:
                continue
            crop = crop_around(fake_screen(), make_qr(TARGETS[0], qpx), side)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            whole = decode_gray(gray) == TARGETS[0]
            tiled = any(
                decode_gray(gray[y0:y1, x0:x1], max_side=TILE_MAX_SIDE) == TARGETS[0]
                for x0, y0, x1, y1 in tile_rects(gray.shape)
            )
            tolerance.append((side, qpx, whole, tiled))
            print(f"{side:>5} {qpx:>5} {qpx/side:>6.0%} "
                  f"{'✓' if whole else '·':>5} {'✓' if tiled else '·':>5}")

    # 半张码：必须识别失败，不能吐乱码
    qr = make_qr(TARGETS[0], 200)
    canvas = fake_screen()
    canvas[600:600 + qr.shape[0], 1000:1000 + qr.shape[1]] = qr
    half = cv2.cvtColor(canvas[600:720, 1000:1200], cv2.COLOR_BGR2GRAY)
    half = decode_gray(half, eager=True)

    total = len(rows)
    tiled_ok = sum(1 for _, _, _, t in tolerance if t)
    print(f"\n共 {total} 例：直接命中 {stats['直接命中']}，"
          f"需要重试 {stats['需要重试']}，失败 {stats['失败']}")
    print(f"尺寸容差 {tiled_ok}/{len(tolerance)} 能认（其中整帧只认 "
          f"{sum(1 for _, _, w, _ in tolerance if w)} 例）")
    print(f"半张码 -> {half!r}  {'✓ 正确拒绝' if not half else '✗ 吐了东西'}")

    ok = stats["失败"] == 0 and not half and tiled_ok == len(tolerance)
    print("结果", "全部通过 ✓" if ok else "有问题 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
