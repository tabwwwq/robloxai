"""
Guide-бот для Roblox (метро-тайкун): сам берёт задание Guide, находит на карте
станции Start и Goal и протягивает маршрут по одной станции за раз, от текущей
к соседней. Если нужная станция спрятана под окном задания - сам сдвигает карту.

Запуск:   python guide_bot.py
Клавиши:  P - пауза / продолжить
          O - выход
          (срабатывают, когда активно окно игры или окно бота; F8 / F10 работают везде)
Опции:    --count N     выполнить N заданий и выйти (по умолчанию без ограничения)
          --once        то же, что --count 1
          --cooldown N  пауза после пройденного задания в секундах (по умолчанию 10)
          --debug       подробный лог и размеченные скриншоты в папку debug/

Требования: Windows, Python 3.10+, pip install opencv-python mss numpy
Игра должна быть открыта (окно бот развернёт сам), карта - с одной веткой метро.
Пока бот работает, мышь занята: не двигай её и не переключай окно (или поставь паузу клавишей P).
"""
from __future__ import annotations

import argparse
import ctypes
import heapq
import math
import os
import sys
import time
from ctypes import wintypes

import cv2
import mss
import numpy as np

# --------------------------------------------------------------------------- #
#  Win32: окно, мышь, клавиши
# --------------------------------------------------------------------------- #
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
user32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


M_MOVE, M_ABS, M_VDESK, M_LDOWN, M_LUP = 0x0001, 0x8000, 0x4000, 0x0002, 0x0004
VK_F8, VK_F10, VK_MENU = 0x77, 0x79, 0x12
VK_P, VK_O = 0x50, 0x4F          # пауза и выход
CONSOLE = ctypes.windll.kernel32.GetConsoleWindow()


def hotkey(game_hwnd, vk, vk_anywhere):
    """Нажата ли клавиша управления. P и O считаются только в окне игры или бота,
    чтобы бот не останавливался, когда ты печатаешь в другом приложении."""
    # 0x8000 - клавиша зажата сейчас, 1 - её нажали с прошлой проверки (ловит короткие нажатия)
    if user32.GetAsyncKeyState(vk_anywhere) & 0x8001:
        return True
    if user32.GetAsyncKeyState(vk) & 0x8001:
        fg = user32.GetForegroundWindow()
        return bool(fg) and fg in (game_hwnd, CONSOLE)
    return False


class Stop(Exception):
    """Пользователь попросил выйти."""


def _mouse(flags, x=0, y=0):
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _key(vk, up=False):
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def find_roblox():
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            user32.GetClassNameW(hwnd, cls, 256)
            if title.value == "Roblox" and "WINDOWSCLIENT" in cls.value.upper():
                found.append(hwnd)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


class Game:
    """Окно игры: захват кадра и ввод в клиентских координатах."""

    def __init__(self):
        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.paused = False
        self._f8 = False
        self.hwnd = None
        self.find_window()

    def find_window(self):
        """Ждём окно Roblox (при запуске и если игра перезапустилась)."""
        told = False
        while True:
            self.hwnd = find_roblox()
            if self.hwnd:
                if told:
                    log("Окно Roblox найдено")
                return
            if not told:
                log("Жду окно Roblox - запусти игру (O - выход)")
                told = True
            if hotkey(None, VK_O, VK_F10):
                raise Stop
            time.sleep(0.5)

    def alive(self):
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    # ---- окно ----
    def rect(self):
        r = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        p = wintypes.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(p))
        return p.x, p.y, r.right, r.bottom

    def focused(self):
        return user32.GetForegroundWindow() == self.hwnd

    def focus(self):
        if self.focused():
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
        _key(VK_MENU), _key(VK_MENU, True)  # позволяет забрать фокус из фонового процесса
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(0.5)

    def grab(self):
        while True:
            if not self.alive():
                self.find_window()
            x, y, w, h = self.rect()
            if w >= 400 and h >= 300:
                shot = self.sct.grab({"left": x, "top": y, "width": w, "height": h})
                return np.ascontiguousarray(np.array(shot)[:, :, 2::-1])  # RGB
            self.check()          # окно свёрнуто - ждём
            time.sleep(0.3)

    # ---- управление ----
    def check(self):
        """Обработка клавиш паузы / выхода. Вызывается во всех ожиданиях."""
        if hotkey(self.hwnd, VK_O, VK_F10):
            raise Stop
        down = hotkey(self.hwnd, VK_P, VK_F8)
        if down and not self._f8:
            self.paused = not self.paused
            log("ПАУЗА (P - продолжить)" if self.paused else "Продолжаю")
        self._f8 = down
        return not self.paused

    def sleep(self, sec):
        end = time.time() + sec
        while True:
            self.check()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.03, left))

    def wait_ready(self):
        """Ждём, пока бот не на паузе и окно игры активно."""
        warned = False
        while True:
            ok = self.check()
            if not self.alive():
                self.find_window()
                self.focus()
            if ok and self.focused():
                return
            if ok and not warned:
                log("Окно Roblox не активно - жду (кликни по игре; O в окне бота - выход)")
                warned = True
            time.sleep(0.1)

    # ---- мышь ----
    def cursor(self):
        p = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        ox, oy, _, _ = self.rect()
        return p.x - ox, p.y - oy

    def move(self, x, y):
        ox, oy, _, _ = self.rect()
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        ax = int((ox + x - vx) * 65535 / max(1, vw - 1))
        ay = int((oy + y - vy) * 65535 / max(1, vh - 1))
        _mouse(M_MOVE | M_ABS | M_VDESK, ax, ay)

    def glide(self, x, y, step=40, delay=0.004):
        cx, cy = self.cursor()
        n = max(1, int(max(abs(x - cx), abs(y - cy)) / step))
        for i in range(1, n + 1):
            self.check()      # клавиши паузы / выхода ловим и во время движения мыши
            self.move(round(cx + (x - cx) * i / n), round(cy + (y - cy) * i / n))
            time.sleep(delay)

    def _jiggle(self):
        # Roblox замечает курсор только после относительного движения мыши
        for d in (2, -2, 1, -1):
            _mouse(M_MOVE, d, d)
            time.sleep(0.02)

    def click(self, x, y):
        self.wait_ready()
        self.glide(x, y)
        time.sleep(0.06)
        self._jiggle()
        time.sleep(0.06)
        _mouse(M_LDOWN)
        time.sleep(0.07)
        _mouse(M_LUP)

    def drag(self, a, b):
        self.wait_ready()
        self.glide(*a)
        time.sleep(0.06)
        self._jiggle()
        time.sleep(0.08)
        _mouse(M_LDOWN)
        try:
            time.sleep(0.12)
            self.glide(b[0], b[1], step=14, delay=0.008)
            time.sleep(0.05)
            self._jiggle()
            time.sleep(0.12)
        finally:
            _mouse(M_LUP)

    def park(self):
        """Убрать курсор с карты, чтобы не мешал распознаванию."""
        _, _, w, h = self.rect()
        self.glide(int(w * 0.5), int(h * 0.13))


# --------------------------------------------------------------------------- #
#  Зрение
# --------------------------------------------------------------------------- #
C_BG = (44, 47, 53)
C_GREEN = (34, 197, 115)      # кольцо/метка Start
C_RED = (255, 94, 87)         # кольцо/метка Goal
C_BLUE = (46, 132, 255)       # метка Current, кнопка Undo
C_ORANGE = (255, 160, 40)     # кнопка Guide, кнопка Start, шапка задания
C_PURPLE = (122, 90, 222)     # кнопка Tasks
C_DOT_IN = (54, 58, 66)       # заливка кружка станции


def cmask(img, rgb, tol):
    lo = np.clip(np.array(rgb) - tol, 0, 255).astype(np.uint8)
    hi = np.clip(np.array(rgb) + tol, 0, 255).astype(np.uint8)
    return cv2.inRange(img, lo, hi)


def blobs(mask, min_area=20):
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a >= min_area:
            out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "area": int(a),
                        "cx": float(cent[i][0]), "cy": float(cent[i][1])})
    return out


def _fit_circle(xs, ys):
    A = np.column_stack([xs, ys, np.ones_like(xs)])
    b = xs ** 2 + ys ** 2
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    r = math.sqrt(max(1e-6, sol[2] + cx * cx + cy * cy))
    return cx, cy, r


def _coverage(mask, cx, cy, r, n=180):
    h, w = mask.shape
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    xs = np.round(cx + r * np.cos(ang)).astype(int)
    ys = np.round(cy + r * np.sin(ang)).astype(int)
    ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    hit = np.zeros(n, bool)
    hit[ok] = mask[ys[ok], xs[ok]] > 0
    return hit.mean()


def find_ring(img, rgb):
    """Тонкое цветное кольцо вокруг станции Start/Goal -> (cx, cy, r) или None."""
    best = find_ring_ex(img, rgb)
    return best and best[3] >= 0.5 and (best[0], best[1], best[2]) or None


def ring_mask(img, rgb):
    """Пиксели цвета кольца, включая сглаженные края (кольцо бывает толщиной 1 px)."""
    r = img[:, :, 0].astype(np.int16)
    g = img[:, :, 1].astype(np.int16)
    b = img[:, :, 2].astype(np.int16)
    if rgb == C_GREEN:
        m = (g >= 105) & (g - r >= 55) & (g - b >= 22) & ((g - b) * 4 <= (g - r) * 3)
    else:  # красное кольцо Goal: розоватое, в отличие от чисто-красной линии метро
        m = (r >= 135) & (g >= 60) & (b >= 58) & (r - g >= 65) & (np.abs(g - b) <= 22)
    return m.astype(np.uint8) * 255


def find_ring_ex(img, rgb):
    """Кольцо вокруг станции Start/Goal -> (cx, cy, r, доля видимой окружности) или None.
    Работает на любом масштабе и когда часть кольца закрыта интерфейсом."""
    h, w = img.shape[:2]
    m = ring_mask(img, rgb)
    thick = cv2.dilate(cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)),
                       np.ones((7, 7), np.uint8))
    thin = cv2.bitwise_and(m, cv2.bitwise_not(thick))
    wide = cv2.dilate(thin, np.ones((3, 3), np.uint8))
    rmin, rmax = 15.0, w * 0.07
    rng = np.random.default_rng(1)
    best = None
    # склеиваем обрывки кольца в группы и в каждой ищем окружность (RANSAC)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(thin, np.ones((7, 7), np.uint8)), connectivity=8)
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if max(bw, bh) < 24 or min(bw, bh) < 8:
            continue
        sub = (lab[y:y + bh, x:x + bw] == i) & (thin[y:y + bh, x:x + bw] > 0)
        ys, xs = np.nonzero(sub)
        if len(xs) < 20 or len(xs) > 6000:
            continue
        xs, ys = xs.astype(float) + x, ys.astype(float) + y
        cand = None
        for _ in range(220):
            k = rng.choice(len(xs), 3, replace=False)
            (x1, y1), (x2, y2), (x3, y3) = zip(xs[k], ys[k])
            d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
            if abs(d) < 1e-6:
                continue
            s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
            cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
            cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
            r = math.hypot(x1 - cx, y1 - cy)
            if not (rmin <= r <= rmax):
                continue
            inl = np.abs(np.hypot(xs - cx, ys - cy) - r) < 1.6
            c = int(inl.sum())
            if cand is None or c > cand[0]:
                cand = (c, inl)
        if cand is None or cand[0] < 18:
            continue
        cx, cy, r = _fit_circle(xs[cand[1]], ys[cand[1]])
        for _ in range(2):
            inl = np.abs(np.hypot(xs - cx, ys - cy) - r) < 1.6
            if inl.sum() < 18:
                break
            cx, cy, r = _fit_circle(xs[inl], ys[inl])
        if not (rmin <= r <= rmax):
            continue
        cov = _coverage(wide, cx, cy, r)
        # внутри настоящего кольца - тёмная карта, а не заливка (отсекает значки линий и кнопки)
        ang = np.linspace(0, 2 * np.pi, 48, endpoint=False)
        dark = []
        for q in (r - 4.0, r * 0.7):
            px = np.clip(np.round(cx + q * np.cos(ang)).astype(int), 0, w - 1)
            py = np.clip(np.round(cy + q * np.sin(ang)).astype(int), 0, h - 1)
            dark.append((img[py, px].max(1) < 140).mean())
        if min(dark) < 0.6:
            continue
        if cov >= 0.22 and (best is None or cov > best[3]):
            best = (float(cx), float(cy), float(r), float(cov))
    return best


def dot_mask(img):
    """Заливка кружка станции. Чуть светлее эталона тоже берём: на мелком масштабе
    кружок размыт сглаживанием, а чистого цвета в нём всего 2-4 пикселя."""
    return cv2.inRange(img, np.array((50, 54, 61), np.uint8), np.array((70, 72, 82), np.uint8))


def find_stations(img):
    """Кружки станций: тёмная заливка + серый ободок. -> список (x, y, r)."""
    m = dot_mask(img)
    h, w = m.shape
    out = []
    ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    for b in blobs(m, 6):     # на минимальном масштабе кружок всего 3-4 px
        if not (3 <= b["w"] <= 18 and 3 <= b["h"] <= 18 and abs(b["w"] - b["h"]) <= 2):
            continue
        # маленький кружок после сглаживания похож на ромб, большой - на круг
        if b["area"] < (0.45 if max(b["w"], b["h"]) <= 6 else 0.55) * b["w"] * b["h"]:
            continue
        cx, cy = b["x"] + (b["w"] - 1) / 2, b["y"] + (b["h"] - 1) / 2
        rr = (b["w"] + b["h"]) / 4 + 0.5
        # ободок полупрозрачный: над фоном серый, над поездом светлее, над линией с её оттенком,
        # но всегда заметно светлее тёмной заливки. Смотрим два соседних радиуса.
        bright = np.zeros(16, int)
        yellow = np.zeros(16, bool)
        for q in (rr, rr + 1.0):
            xs = np.clip(np.round(cx + q * np.cos(ang)).astype(int), 0, w - 1)
            ys = np.clip(np.round(cy + q * np.sin(ang)).astype(int), 0, h - 1)
            px = img[ys, xs].astype(int)
            bright = np.maximum(bright, px.sum(1))
            yellow |= (px[:, 0] > 200) & (px[:, 1] > 140) & (px[:, 2] < 110)  # значки достопримечательностей
        if (bright >= 215).sum() >= 10 and yellow.sum() <= 3:
            out.append((cx, cy, rr + 0.5))
    return out


def line_colors(img, stations):
    """Цвета линии метро: то, что примыкает снаружи к кружкам станций."""
    h, w = img.shape[:2]
    ang = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    seen = {}
    for cx, cy, r in stations:
        xs = np.clip(np.round(cx + (r + 3.5) * np.cos(ang)).astype(int), 0, w - 1)
        ys = np.clip(np.round(cy + (r + 3.5) * np.sin(ang)).astype(int), 0, h - 1)
        px = img[ys, xs].astype(int)
        cnt = {}
        for p in px:
            if np.abs(p - C_BG).max() <= 8 or p.max() - p.min() < 22:
                continue  # фон и серое/белое (текст, поезда)
            k = tuple(int(v) // 6 for v in p)
            cnt[k] = cnt.get(k, 0) + 1
        for k, c in cnt.items():
            if c >= 2:
                seen.setdefault(k, []).append(c)
    need = max(2, 0.15 * len(stations))
    cols = [tuple(v * 6 + 3 for v in k) for k, lst in seen.items() if len(lst) >= need]
    if not cols and seen:      # запасной вариант: самый частый цвет, если он есть хотя бы у двух станций
        k, lst = max(seen.items(), key=lambda kv: len(kv[1]))
        if len(lst) >= 2:
            cols = [tuple(v * 6 + 3 for v in k)]
    return cols


def line_mask(img, colors):
    m = np.zeros(img.shape[:2], np.uint8)
    for c in colors:
        m |= cmask(img, c, 9)
    return m


def shortest_path(cost, src, dst):
    """Дейкстра по сетке стоимостей (8 соседей). src/dst = (x, y)."""
    h, w = cost.shape
    INF = float("inf")
    dist = np.full(h * w, INF)
    prev = np.full(h * w, -1, np.int64)
    c = cost.ravel().tolist()
    s, t = src[1] * w + src[0], dst[1] * w + dst[0]
    dist[s] = 0.0
    dl = dist.tolist()
    pl = prev.tolist()
    pq = [(0.0, s)]
    nb = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
          (-1, -1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (1, 1, 1.414)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dl[u]:
            continue
        uy, ux = divmod(u, w)
        for dx, dy, k in nb:
            vx, vy = ux + dx, uy + dy
            if 0 <= vx < w and 0 <= vy < h:
                v = vy * w + vx
                nd = d + c[v] * k
                if nd < dl[v]:
                    dl[v] = nd
                    pl[v] = u
                    heapq.heappush(pq, (nd, v))
    if dl[t] == INF:
        return None
    path, u = [], t
    while u != -1:
        path.append((u % w, u // w))
        u = pl[u]
    return path[::-1]


class Plan:
    def __init__(self):
        self.route = []       # [(x, y), ...] станции по порядку: откуда тянем ... Goal
        self.idx = []         # индекс точки пути для каждой станции маршрута
        self.path = []        # точки пути вдоль линии
        self.stations = []
        self.error = None


def make_plan(img, src, goal, stations):
    """Маршрут вдоль линии метро от src до goal через найденные кружки станций."""
    p = Plan()
    p.stations = stations
    h, w = img.shape[:2]
    if len(stations) < 1:
        p.error = "не вижу станций"
        return p

    def snap(pt):  # центр кружка станции точнее центра кольца
        dm, s = min((math.hypot(s[0] - pt[0], s[1] - pt[1]), s) for s in stations)
        return (s[0], s[1]) if dm <= 9 else pt

    a, b = snap(src), snap(goal)
    cols = line_colors(img, stations)
    if not cols:
        p.error = "не определил цвет линии"
        return p
    lm = cv2.dilate(line_mask(img, cols), np.ones((3, 3), np.uint8))
    f = 3
    xs = [s[0] for s in stations] + [a[0], b[0]]
    ys = [s[1] for s in stations] + [a[1], b[1]]
    x0, x1 = int(min(w - 2, max(0, min(xs) - 60))), int(max(2, min(w, max(xs) + 60)))
    y0, y1 = int(min(h - 2, max(0, min(ys) - 60))), int(max(2, min(h, max(ys) + 60)))
    sub = lm[y0:y1, x0:x1]
    small = cv2.resize(sub, (max(1, sub.shape[1] // f), max(1, sub.shape[0] // f)),
                       interpolation=cv2.INTER_AREA) > 0
    cost = np.where(small, 1.0, 25.0)
    sh, sw = cost.shape

    def to_s(pt):
        return (min(sw - 1, max(0, int((pt[0] - x0) / f))), min(sh - 1, max(0, int((pt[1] - y0) / f))))

    path = shortest_path(cost, to_s(a), to_s(b))
    if not path:
        p.error = "не нашёл путь по линии"
        return p
    pts = np.array([(x0 + (x + 0.5) * f, y0 + (y + 0.5) * f) for x, y in path])
    p.path = pts
    on = []
    for cx, cy, _ in stations:
        d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        i = int(d.argmin())
        if d[i] <= 9:
            on.append((i, cx, cy))
    # Страховка: станции должны идти строго по одной, пропускать нельзя. Поэтому ещё раз
    # проходим вдоль самой линии и ищем кружки, которые общий поиск мог не узнать.
    dm = dot_mask(img)
    run = []
    for i, (px, py) in enumerate(pts):
        xi, yi = int(px), int(py)
        cnt = cv2.countNonZero(dm[max(0, yi - 3):yi + 4, max(0, xi - 3):xi + 4])
        if cnt >= 4:
            run.append((cnt, i))
        if (cnt < 4 or i == len(pts) - 1) and run:
            _, bi = max(run)
            run = []
            xi, yi = int(pts[bi][0]), int(pts[bi][1])
            ys_, xs_ = np.nonzero(dm[max(0, yi - 6):yi + 7, max(0, xi - 6):xi + 7])
            cx, cy = xs_.mean() + max(0, xi - 6), ys_.mean() + max(0, yi - 6)
            near = img[max(0, yi - 9):yi + 10, max(0, xi - 9):xi + 10].astype(int)
            yellow = ((near[:, :, 0] > 200) & (near[:, :, 1] > 140) & (near[:, :, 2] < 110)).sum()
            if yellow <= 6 and all(math.hypot(cx - o[1], cy - o[2]) > 10 for o in on):
                on.append((bi, float(cx), float(cy)))
    on.sort()
    route, idx = [tuple(a)], [0]
    for i, cx, cy in on:
        if math.hypot(cx - a[0], cy - a[1]) < 14 or math.hypot(cx - b[0], cy - b[1]) < 14:
            continue
        if math.hypot(cx - route[-1][0], cy - route[-1][1]) < 14:
            continue
        route.append((cx, cy))
        idx.append(i)
    route.append(tuple(b))
    idx.append(len(pts) - 1)
    p.route = [(int(round(x)), int(round(y))) for x, y in route]
    p.idx = idx
    return p


def draw_plan(img, p, legs=0, rects=()):
    out = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    for x0, y0, x1, y1 in rects:
        cv2.rectangle(out, (int(x0), int(y0)), (int(x1), int(y1)), (90, 90, 90), 1)
    for x, y in (p.path if len(p.path) else []):
        cv2.circle(out, (int(x), int(y)), 1, (0, 255, 255), -1)
    for cx, cy, r in p.stations:
        cv2.circle(out, (int(cx), int(cy)), 10, (255, 255, 0), 1)
    for i, (x, y) in enumerate(p.route):
        col = (255, 0, 255) if i <= legs else (120, 120, 255)
        cv2.circle(out, (x, y), 14, col, 2)
        cv2.putText(out, str(i), (x + 14, y - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2)
    return out


# ---- интерфейс ---- #
def ui_state(img):
    """-> (state, data). state: quit | task | intro | bar | closed | unknown"""
    h, w = img.shape[:2]
    rm = cmask(img, C_RED, 14) | cmask(img, (255, 118, 112), 12)
    for b in blobs(rm, 300):  # большая красная кнопка Quit в окне "Quit this task?"
        if 0.08 * w <= b["w"] <= 0.24 * w and 0.04 * h <= b["h"] <= 0.13 * h and b["cy"] < 0.85 * h:
            return "quit", b
    om = cmask(img, C_ORANGE, 14) | cmask(img, (255, 174, 72), 12)
    ob = blobs(om, 300)
    for b in ob:  # шапка открытого задания
        if b["w"] >= 0.25 * w and 0.02 * h <= b["h"] <= 0.09 * h:
            return "task", b
    for b in ob:  # большая кнопка Start в окне Guide
        if 0.08 * w <= b["w"] <= 0.24 * w and 0.04 * h <= b["h"] <= 0.13 * h and b["cy"] < 0.85 * h:
            return "intro", b
    pm = cmask(img, C_PURPLE, 14)[int(h * 0.82):, int(w * 0.75):]
    if cv2.countNonZero(pm) > 0.0008 * w * h:
        ys, xs = np.nonzero(pm)
        return "closed", {"cx": float(xs.mean()) + int(w * 0.75), "cy": float(ys.mean()) + int(h * 0.82)}
    cand = [b for b in ob if b["cy"] > 0.82 * h and 0.2 * w < b["cx"] < 0.8 * w
            and 0.02 * w <= b["w"] <= 0.07 * w and b["h"] >= 0.03 * h]
    if cand:
        return "bar", max(cand, key=lambda b: b["area"])
    return "unknown", None


def ui_rects(w, h, hdr):
    """Прямоугольники интерфейса, закрывающие карту (доли подобраны под развёрнутое окно)."""
    r = [(0.30 * w, 0, 0.72 * w, 0.235 * h),      # верхние панели и подсказка "Drag from..."
         (0, 0, 0.115 * w, 0.07 * h),             # кнопки Roblox
         (0.832 * w, 0, w, 0.48 * h),             # статистика и рейтинг
         (0.955 * w, 0.545 * h, w, h),            # зум
         (0.85 * w, 0.84 * h, w, h),              # Menu / Drive
         (0, 0.76 * h, 0.185 * w, h),             # город, деньги
         (0.38 * w, 0.86 * h, 0.62 * w, h)]       # нижняя панель
    if hdr:
        r.append((hdr["x"] - 10, hdr["y"] - 10, hdr["x"] + hdr["w"] + 10, h))  # окно задания
    return r


def is_blocked(pt, rects, w, h, m=16):
    x, y = pt
    if x < m or y < m or x > w - m or y > h - m:
        return True
    return any(x0 - m <= x <= x1 + m and y0 - m <= y <= y1 + m for x0, y0, x1, y1 in rects)


def has_blue_pin(img, st, scale):
    """Синяя метка Current над станцией st?"""
    h, w = img.shape[:2]
    x, y = st
    x0, x1 = int(max(0, x - 34 * scale)), int(min(w, x + 34 * scale))
    y0, y1 = int(max(0, y - 100 * scale)), int(max(0, y - 8 * scale))
    if y1 <= y0 or x1 <= x0:
        return False
    return cv2.countNonZero(cmask(img[y0:y1, x0:x1], C_BLUE, 14)) >= 60 * scale * scale


def find_current(img, stations, rects, scale):
    """Станция под синей меткой Current (докуда уже построен маршрут)."""
    h, w = img.shape[:2]
    best = None
    for b in blobs(cmask(img, C_BLUE, 14), int(250 * scale * scale)):
        if not (24 * scale <= b["w"] <= 54 * scale and 28 * scale <= b["h"] <= 66 * scale):
            continue
        if is_blocked((b["cx"], b["cy"]), rects, w, h, 0):
            continue
        tx, ty = b["x"] + b["w"] / 2.0, b["y"] + b["h"] + 18 * scale
        for cx, cy, _ in stations:
            d = math.hypot(cx - tx, cy - ty)
            if abs(cx - tx) <= 10 * scale and d <= 22 * scale and (best is None or d < best[0]):
                best = (d, (cx, cy))
    return best and best[1]


# --------------------------------------------------------------------------- #
#  Логика бота
# --------------------------------------------------------------------------- #
T0 = time.time()
ZOOM_STEP = 0.98      # один щелчок колеса мыши


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class View:
    """Всё, что бот видит на одном кадре."""

    def __init__(self, img, hdr):
        self.img = img
        self.h, self.w = img.shape[:2]
        self.scale = self.w / 1920.0
        self.rects = ui_rects(self.w, self.h, hdr)
        top = (hdr["y"] if hdr else 0.65 * self.h) - 16
        self.safe = (0.03 * self.w, 0.25 * self.h, 0.82 * self.w, top)   # свободная часть карты
        self.G = self._ring(C_GREEN)
        self.R = self._ring(C_RED)
        self.stations = [s for s in find_stations(img) if not self.blocked(s, 6)]
        self.cur = find_current(img, self.stations, self.rects, self.scale)

    def _ring(self, col):
        r = find_ring_ex(self.img, col)
        if not r or r[3] < (0.5 if r[2] >= 15 else 0.62):
            return None
        return None if self.blocked(r, 0) else (r[0], r[1])

    def blocked(self, pt, m=16):
        return is_blocked((pt[0], pt[1]), self.rects, self.w, self.h, m)

    def clear(self, pt):
        return pt is not None and not self.blocked(pt, 16)


class Bot:
    def __init__(self, args):
        self.g = Game()
        self.args = args
        self.done = 0
        self.failed = 0
        self.next_guide_at = 0.0
        base = sys.executable if getattr(sys, "frozen", False) else __file__
        self.dbg_dir = os.path.join(os.path.dirname(os.path.abspath(base)), "debug")
        self.dbg_n = 0

    def dbg(self, name, img_bgr):
        if not self.args.debug:
            return
        os.makedirs(self.dbg_dir, exist_ok=True)
        self.dbg_n += 1
        cv2.imwrite(os.path.join(self.dbg_dir, f"{time.strftime('%H%M%S')}_{self.dbg_n:03d}_{name}.png"), img_bgr)

    def dbg_rgb(self, name, img):
        if self.args.debug:
            self.dbg(name, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

    def vlog(self, msg):
        if self.args.debug:
            log("  " + msg)

    # ---- ожидание остановки камеры ----
    def wait_still(self, timeout=7.0, need_ring=True):
        """Ждём, пока карта перестанет двигаться (по кольцам Start/Goal)."""
        last, stable, t0 = None, 0, time.time()
        while time.time() - t0 < timeout:
            img = self.g.grab()
            st, hdr = ui_state(img)
            if st != "task":
                return None
            g, r = find_ring_ex(img, C_GREEN), find_ring_ex(img, C_RED)
            cur = tuple((p[0], p[1]) if p and p[3] >= 0.5 else None for p in (g, r))
            if last is not None and (cur[0] or cur[1] or not need_ring):
                same = all((a is None) == (b is None) and (a is None or math.hypot(a[0] - b[0], a[1] - b[1]) < 1.5)
                           for a, b in zip(cur, last))
                stable = stable + 1 if same else 0
                if stable >= 2:
                    return View(img, hdr)
            last = cur
            self.g.sleep(0.15)
        return None

    # ---- движение карты ----
    def pan(self, v, vec):
        """Сдвинуть карту на vec протяжкой по пустому месту. -> фактический сдвиг."""
        g = self.g
        w, h = v.w, v.h
        busy = (v.img.max(axis=2) >= 100).astype(np.uint8)        # надписи, значки, кольца, поезда
        cols = line_colors(v.img, v.stations) if v.stations else []
        if cols:
            busy |= (line_mask(v.img, cols) > 0).astype(np.uint8)
        k = int(30 * v.scale) | 1
        busy = cv2.dilate(busy, np.ones((k, k), np.uint8))
        sx0, sy0, sx1, sy1 = v.safe
        vx, vy = vec
        for shrink in (1.0, 0.6, 0.35):
            dx, dy = vx * shrink, vy * shrink
            best = None
            for y in range(int(sy0 + 20), int(sy1 - 10), 30):
                for x in range(int(sx0 + 20), int(sx1 - 10), 30):
                    ex, ey = x + dx, y + dy
                    if busy[y, x] or v.blocked((x, y), 24):
                        continue
                    if not (25 < ex < w - 25 and 25 < ey < h - 25):
                        continue
                    # лучше всего, когда вся протяжка идёт по середине свободной зоны
                    mx, my = x + dx / 2, y + dy / 2
                    score = abs(mx - (sx0 + sx1) / 2) + abs(my - (sy0 + sy1) / 2)
                    if best is None or score < best[0]:
                        best = (score, x, y)
            if best:
                _, x, y = best
                self.vlog(f"сдвиг карты на ({dx:.0f}, {dy:.0f})")
                g.drag((x, y), (int(round(x + dx)), int(round(y + dy))))
                g.park()
                return (round(x + dx) - x, round(y + dy) - y)
        return (0, 0)

    def zoom_out(self, v, notches):
        g = self.g
        ax, ay = (v.safe[0] + v.safe[2]) / 2, (v.safe[1] + v.safe[3]) / 2
        g.wait_ready()
        g.glide(int(ax), int(ay))
        time.sleep(0.05)
        g._jiggle()
        ax, ay = g.cursor()
        self.vlog(f"отдаляю карту на {notches} щелчков")
        for _ in range(notches):
            inp = INPUT(type=0)
            inp.u.mi = MOUSEINPUT(0, 0, (-120) & 0xFFFFFFFF, 0x0800, 0, 0)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            time.sleep(0.03)
        g.sleep(0.4)
        g.park()
        return (ax, ay), ZOOM_STEP ** notches

    def click_dialog(self, hdr, which):
        """Кнопки Start / Goal в окне задания - камера наводится на эту станцию."""
        fx = 0.559 if which == "start" else 0.735
        self.g.click(int(hdr["x"] + fx * hdr["w"]), int(hdr["y"] + 3.47 * hdr["h"]))
        self.g.park()

    # ---- одно задание ----
    def solve(self):
        g = self.g
        g.park()
        v = self.wait_still()
        if v is None:
            st, hdr = ui_state(g.grab())
            if st != "task":
                return True
            self.vlog("не вижу колец - навожу камеру кнопкой Start")
            self.click_dialog(hdr, "start")
            v = self.wait_still()
            if v is None:
                log("Не вижу станции Start и Goal")
                self.dbg_rgb("no_rings", g.grab())
                return False
            sym = False
        else:
            sym = True      # игра ставит Start и Goal симметрично относительно центра экрана
        est = {}            # последние известные координаты: start, goal, cur
        had_undo = False
        fails = searches = 0
        announced = False
        for _ in range(40):
            if fails >= 6:
                break
            if v is None:
                img = g.grab()
                st, hdr = ui_state(img)
                if st != "task":
                    return True
                v = View(img, hdr)
            if v.G:
                est["start"] = v.G
            if v.R:
                est["goal"] = v.R
            if v.cur:
                est["cur"] = v.cur
            if sym:
                sym = False
                if "start" in est and "goal" not in est:
                    est["goal"] = (v.w - est["start"][0], v.h - est["start"][1])
                elif "goal" in est and "start" not in est:
                    est["start"] = (v.w - est["goal"][0], v.h - est["goal"][1])
            src = est.get("cur") or est.get("start")
            dst = est.get("goal")
            if src is None or dst is None:     # одна из станций где-то за экраном - отдаляем карту
                searches += 1
                if searches > 8:
                    break
                anchor, z = self.zoom_out(v, 10)
                est = {k: (anchor[0] + (p[0] - anchor[0]) * z, anchor[1] + (p[1] - anchor[1]) * z)
                       for k, p in est.items()}
                v = self.wait_still()
                continue

            # сколько перегонов можно пройти прямо сейчас, не двигая карту
            plan, legs, trouble = None, 0, None
            src_ok = v.clear(src) and (v.cur is not None or v.G is not None)
            if src_ok:
                plan = make_plan(v.img, src, dst, v.stations)
                if plan.error:
                    # карта видна, но разобрать её не вышло (поезд, анимация) - просто смотрим ещё раз
                    self.vlog(f"план не построен: {plan.error}")
                    fails += 1
                    g.sleep(0.6)
                    v = None
                    continue
            if plan:
                goal_ok = v.R is not None and v.clear(v.R)
                for i in range(len(plan.route) - 1):
                    b = plan.route[i + 1]
                    seg = plan.path[plan.idx[i]:plan.idx[i + 1] + 1]
                    bad = [q for q in seg if v.blocked(q, 2)]
                    if bad or v.blocked(b, 16) or (i == len(plan.route) - 2 and not goal_ok):
                        trouble = tuple(bad[0]) if bad else b
                        break
                    legs += 1
                self.dbg("plan", draw_plan(v.img, plan, legs, v.rects))
                if not announced:
                    log(f"Маршрут: {len(plan.route) - 1} перегон(ов)")
                    announced = True

            if legs == 0:
                # нужная станция под интерфейсом или за экраном - сдвигаем карту
                sx0, sy0, sx1, sy1 = v.safe
                cx, cy, m = (sx0 + sx1) / 2, (sy0 + sy1) / 2, 45 * v.scale
                if not src_ok:
                    # ставим текущую станцию так, чтобы и цель по возможности попала в свободную зону
                    tx = min(sx1 - m, max(sx0 + m, cx - (dst[0] - src[0]) / 2))
                    ty = min(sy1 - m, max(sy0 + m, cy - (dst[1] - src[1]) / 2))
                    vec = (tx - src[0], ty - src[1])
                else:
                    q = trouble or dst
                    # оба конца в свободной зоне? иначе показываем проблемное место
                    if abs(dst[0] - src[0]) <= sx1 - sx0 - 2 * m and abs(dst[1] - src[1]) <= sy1 - sy0 - 2 * m:
                        q = dst
                    tx = min(sx1 - m, max(sx0 + m, cx - (q[0] - src[0]) / 2))
                    ty = min(sy1 - m, max(sy0 + m, cy - (q[1] - src[1]) / 2))
                    vec = (tx - src[0], ty - src[1])
                moved = (0, 0)
                if math.hypot(*vec) >= 20:
                    moved = self.pan(v, vec)
                if math.hypot(*moved) < 10:
                    fails += 1
                    anchor, z = self.zoom_out(v, 12)
                    est = {k: (anchor[0] + (p[0] - anchor[0]) * z, anchor[1] + (p[1] - anchor[1]) * z)
                           for k, p in est.items()}
                else:
                    est = {k: (p[0] + moved[0], p[1] + moved[1]) for k, p in est.items()}
                v = self.wait_still(need_ring=False)
                continue

            # тянем перегоны
            route = plan.route
            ring0 = v.R
            ok = True
            for i in range(legs):
                a, b = route[i], route[i + 1]
                last = i == len(route) - 2
                g.drag(a, b)
                g.glide(b[0], int(min(v.safe[3], b[1] + 70 * v.scale)), step=30)  # курсор прочь со станции
                g.sleep(0.25)
                img = g.grab()
                st, hdr = ui_state(img)
                if st != "task":
                    return True
                if last:
                    if self.wait_finish(ring0, had_undo):
                        return True
                    ok = False
                else:
                    ring1 = find_ring(img, C_RED)
                    moved = ring0 and ring1 and math.hypot(ring1[0] - ring0[0], ring1[1] - ring0[1]) > 3
                    if moved or not has_blue_pin(img, b, v.scale):
                        g.sleep(0.35)             # метка Current могла ещё не доехать
                        img = g.grab()
                        if moved or not has_blue_pin(img, b, v.scale):
                            ok = False
                if not ok:
                    self.vlog(f"перегон {i + 1} не засчитан - смотрю заново")
                    self.dbg_rgb("seg_fail", img)
                    fails += 1
                    break
                had_undo = True
                est["cur"] = b
            g.park()
            g.sleep(0.2)
            v = None     # заново осматриваемся: где метка Current и что дальше
        return False

    @staticmethod
    def undo_visible(img, hdr):
        """Синяя кнопка Undo в окне задания (есть, пока маршрут начат, но не закончен)."""
        h, w = img.shape[:2]
        y0, y1 = int(hdr["y"] + hdr["h"]), int(min(h, hdr["y"] + hdr["h"] * 5.5))
        x0, x1 = int(hdr["x"] + hdr["w"] * 0.45), int(hdr["x"] + hdr["w"])
        undo = cv2.countNonZero(cmask(img[y0:y1, x0:x1], C_BLUE, 14))
        return undo >= 0.004 * (y1 - y0) * (x1 - x0)

    def wait_finish(self, ring0, had_undo, timeout=1.8):
        """После последнего перегона: маршрут принят, если окно задания закрылось,
        пропала кнопка Undo или камера поехала за гостем."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            img = self.g.grab()
            st, hdr = ui_state(img)
            if st != "task":
                return True
            if had_undo and not self.undo_visible(img, hdr):
                return True
            ring = find_ring(img, C_RED)
            if ring0 and (ring is None or math.hypot(ring[0] - ring0[0], ring[1] - ring0[1]) > 3):
                return True
            self.g.sleep(0.15)
        return False

    # ---- главный цикл ----
    def wait_task_end(self, timeout=90):
        """Ждём, пока окно задания закроется. -> новое состояние экрана или None (всё ещё открыто)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, _ = ui_state(self.g.grab())
            if st != "task":
                return st
            self.g.sleep(0.4)
        return None

    def prepare_window(self):
        g = self.g
        g.focus()
        if not self.args.no_maximize and not user32.IsZoomed(g.hwnd):
            user32.ShowWindow(g.hwnd, 3)      # разворачиваем окно: под него подобрана разметка
            time.sleep(0.8)

    def run(self):
        self.prepare_window()
        log("Бот запущен. P - пауза / продолжить, O - выход.")
        self.unknown = 0           # сколько проверок подряд экран не узнан
        self.unknown_at = None     # с какого времени
        self.tries = 0             # попыток на текущее задание
        self.quitting = False
        self.prev = None
        hwnd, errors = self.g.hwnd, 0
        while True:
            try:
                if self.g.hwnd != hwnd:        # игра перезапустилась - окно новое
                    hwnd = self.g.hwnd
                    self.prepare_window()
                if self.tick():
                    return
                errors = 0
            except (Stop, KeyboardInterrupt):
                raise
            except Exception as e:             # что бы ни случилось - не падаем, а продолжаем
                errors += 1
                _mouse(M_LUP)
                log(f"Сбой: {type(e).__name__}: {e} - продолжаю")
                time.sleep(min(10, errors))

    def tick(self):
        """Один шаг: посмотреть на экран и сделать нужное действие. True = работа закончена."""
        g = self.g
        g.wait_ready()
        img = g.grab()
        st, d = ui_state(img)
        if self.args.debug and st != self.prev:
            log(f"  экран: {st}")
        self.prev = st
        if st != "unknown":
            if self.unknown_at and time.time() - self.unknown_at > 20:
                log("Игра снова на экране - продолжаю")
                self.next_guide_at = 0.0
            self.unknown, self.unknown_at = 0, None

        if st == "quit":
            if self.quitting:      # сами решили бросить зависшее задание
                g.click(int(d["cx"]), int(d["cy"]))
                self.quitting = False
                self.tries = 0
                self.next_guide_at = time.time() + self.args.cooldown
            else:                  # окно выхода открылось случайно - "Keep going"
                g.click(int(d["cx"] - 1.076 * d["w"]), int(d["cy"]))
            g.park()
            g.sleep(1.0)

        elif st == "task":
            t0 = time.time()
            self.tries += 1
            ok = self.solve()
            end = self.wait_task_end(90 if ok else 3)
            if end in ("bar", "closed", "intro"):
                self.done += 1
                self.tries = 0
                self.next_guide_at = time.time() + self.args.cooldown
                log(f"Готово за {time.time() - t0:.1f} c. Всего выполнено: {self.done}")
                if self.args.count and self.done >= self.args.count:
                    return True
            elif end is not None:
                self.tries = 0     # экран сменился (перезагрузка сервера, всплывающее окно)
                log("Задание прервано - жду игру")
            elif self.tries >= 4:
                self.failed += 1
                log("Задание не решается с 4 попыток - бросаю его и беру новое")
                self.dbg_rgb("task_fail", g.grab())
                st, d = ui_state(g.grab())
                if st == "task":   # крестик в шапке, дальше подтвердим Quit
                    self.quitting = True
                    g.click(int(d["x"] + d["w"] - d["h"] * 0.62), int(d["y"] + d["h"] * 0.5))
                    g.park()
                    g.sleep(1.0)
            else:
                log(f"Попытка {self.tries} не удалась - пробую ещё раз")
                self.dbg_rgb("retry", g.grab())
                g.sleep(0.5)

        elif st == "intro":
            g.click(int(d["cx"]), int(d["cy"]))
            g.park()
            g.sleep(1.0)

        elif st == "bar":
            self.tries = 0
            left = self.next_guide_at - time.time()
            if left > 0:
                g.sleep(min(left, 0.5))
                return False
            g.click(int(d["cx"]), int(d["cy"]))
            g.park()
            g.sleep(0.9)
            if ui_state(g.grab())[0] == "bar":      # окно не открылось - ещё кулдаун
                g.sleep(1.2)

        elif st == "closed":       # обычное меню (например, после перезагрузки сервера) - открываем Tasks
            log("Открываю меню Tasks")
            g.click(int(d["cx"]), int(d["cy"]))
            g.park()
            g.sleep(1.0)

        else:
            self.unknown += 1
            if self.unknown_at is None:
                self.unknown_at = time.time()
            if self.unknown >= 3:
                # всплывающее окно (миссии, награда...) - закрываем серой кнопкой Close
                h, w = img.shape[:2]
                gm = cv2.inRange(img, np.array((80, 82, 90), np.uint8), np.array((90, 92, 102), np.uint8))
                btn = [b for b in blobs(gm, 500) if b["w"] >= 0.1 * w and 0.012 * h <= b["h"] <= 0.06 * h]
                if btn:
                    b = max(btn, key=lambda b: b["area"])
                    log("Закрываю всплывающее окно")
                    g.click(int(b["cx"]), int(b["cy"] + b["h"] * 0.6))
                    g.park()
                    self.unknown = 0
                    g.sleep(0.8)
                    return False
            if self.unknown == 8:
                log("Не вижу меню игры (загрузка / перезагрузка сервера?). Жду...")
                self.dbg_rgb("unknown", img)
            elif self.unknown > 8 and self.unknown % 150 == 0:
                log(f"Всё ещё жду игру ({(time.time() - self.unknown_at) / 60:.0f} мин)")
            g.sleep(0.6 if self.unknown < 30 else 1.0)
        return False


BANNER = r"""
  ================================================
     GUIDE BOT  -  авто-маршруты для задания Guide
  ================================================
   P - пауза / продолжить
   O - выход
   (клавиши работают, когда активно окно игры)
   Не двигай мышь, пока бот работает (или ставь паузу).
"""


def main():
    ap = argparse.ArgumentParser(description="Guide-бот для Roblox")
    ap.add_argument("--once", action="store_true", help="выполнить одно задание и выйти")
    ap.add_argument("--count", type=int, default=0, help="сколько заданий выполнить (0 = без ограничения)")
    ap.add_argument("--debug", action="store_true", help="подробный лог и размеченные скриншоты в debug/")
    ap.add_argument("--cooldown", type=float, default=10.0, help="пауза после пройденного задания, сек")
    ap.add_argument("--no-maximize", action="store_true", help="не разворачивать окно игры")
    args = ap.parse_args()
    if args.once:
        args.count = 1
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    k32 = ctypes.windll.kernel32
    k32.SetConsoleTitleW("Guide Bot  |  P - пауза, O - выход")
    k32.CreateMutexW(None, False, "GuideBot_single_instance")
    if k32.GetLastError() == 183:      # уже запущен: два бота дрались бы за мышь
        print("Guide Bot уже запущен в другом окне.")
        return
    print(BANNER, flush=True)
    bot = None
    try:
        bot = Bot(args)
        bot.run()
    except (Stop, KeyboardInterrupt):
        pass
    finally:
        _mouse(M_LUP)
        if bot:
            log(f"Остановлен. Выполнено: {bot.done}, брошено: {bot.failed}, "
                f"время: {(time.time() - T0) / 60:.1f} мин")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    if getattr(sys, "frozen", False):   # запущен как .exe двойным кликом - не закрываем окно сразу
        try:
            input("\nНажми Enter, чтобы закрыть окно...")
        except Exception:
            pass
