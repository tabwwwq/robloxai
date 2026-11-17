"""
Guide-бот для Roblox (метро-тайкун): сам берёт задание Guide, находит на карте
станции Start и Goal и протягивает маршрут по одной станции за раз, от текущей
к соседней. Если нужная станция спрятана под окном задания - сам сдвигает карту.

Запуск:   python guide_bot.py
Клавиши:  P - пауза / продолжить
          O - выход
          (срабатывают, когда активно окно игры или окно бота; F8 / F10 работают везде)
Город:    бот сам узнаёт город по надписи на кнопке «City · Menu» (Самара, Belgian Coast)
          и сразу начинает работать. --city samara / --city belgian - задать город вручную.
Опции:    --count N     выполнить N заданий и выйти (по умолчанию без ограничения)
          --once        то же, что --count 1
          --cooldown N  пауза после пройденного задания в секундах
                        (по умолчанию 10, на Belgian Coast 5)
          --debug       подробный лог и размеченные скриншоты в папку debug/
          --check       один снимок без мыши: что бот видит и какой маршрут построил бы

Требования: Windows, Python 3.10+, pip install opencv-python mss numpy
Игра должна быть открыта (окно бот развернёт сам), карта - с одной веткой метро
(Самара или Belgian Coast).
Пока бот работает, мышь занята: не двигай её и не переключай окно (или поставь паузу клавишей P).
"""
from __future__ import annotations

import argparse
import base64
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


def is_light(img):
    """Днём (по игровому времени) карта светлая, ночью тёмная - цвета почти все другие.
    Считаем только серые пиксели суши: море на Belgian Coast, реки и парки цветные
    и могут занимать пол-экрана, поэтому в подсчёт не попадают."""
    h, w = img.shape[:2]
    s = img[int(h * 0.2):int(h * 0.65):16, int(w * 0.05):int(w * 0.8):16].reshape(-1, 3).astype(np.int16)
    lo, hi = s.min(axis=1), s.max(axis=1)
    neutral = hi - lo <= 18
    return int((neutral & (lo >= 200)).sum()) > int((neutral & (hi <= 100)).sum())


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
    light = is_light(img)
    m = ring_mask(img, rgb)
    thick = cv2.dilate(cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)),
                       np.ones((7, 7), np.uint8))
    thin = cv2.bitwise_and(m, cv2.bitwise_not(thick))
    wide = cv2.dilate(thin, np.ones((3, 3), np.uint8))
    rmin, rmax = 15.0, w * 0.16
    rng = np.random.default_rng(1)
    best = None
    best_score = 0.0
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
        # внутри настоящего кольца - карта (тёмная ночью, светлая днём), а не цветная
        # заливка: это отсекает значки линий и кнопки
        ang = np.linspace(0, 2 * np.pi, 48, endpoint=False)
        plain = []
        for q in (r - 4.0, r * 0.7):
            px = np.clip(np.round(cx + q * np.cos(ang)).astype(int), 0, w - 1)
            py = np.clip(np.round(cy + q * np.sin(ang)).astype(int), 0, h - 1)
            inside = img[py, px]
            plain.append((inside.min(1) >= 170).mean() if light else (inside.max(1) < 140).mean())
        if min(plain) < 0.6:
            continue
        # У Start/Goal над кольцом есть толстая цветная метка. Она помогает
        # отличить станцию от круглого аватара Discord поверх карты. Если метка
        # закрыта интерфейсом, кольцо по-прежнему можно найти по одной окружности.
        x0, x1 = max(0, int(cx - 0.7 * r)), min(w, int(cx + 0.7 * r) + 1)
        y0, y1 = max(0, int(cy - 2.3 * r)), max(0, int(cy - 0.35 * r))
        pin = cv2.countNonZero(thick[y0:y1, x0:x1]) >= 0.15 * r * r
        score = cov * (1.4 if pin and cov >= 0.5 else 1.0)
        if cov >= 0.22 and score > best_score:
            best_score = score
            best = (float(cx), float(cy), float(r), float(cov))
    return best


def dot_mask(img):
    """Заливка кружка станции. Чуть светлее эталона тоже берём: на мелком масштабе
    кружок размыт сглаживанием, а чистого цвета в нём всего 2-4 пикселя."""
    return cv2.inRange(img, np.array((50, 54, 61), np.uint8), np.array((70, 72, 82), np.uint8))


def find_stations(img, refs=()):
    """Обычные серые и прокачанные золотые станции. -> список (x, y, r).
    refs - точки, где станция есть наверняка (см. find_light_stations)."""
    out = find_light_stations(img, refs=refs) if is_light(img) else find_dark_stations(img)
    for s in find_gold_stations(img):
        if all(math.hypot(s[0] - x, s[1] - y) > max(s[2], r) for x, y, r in out):
            out.append(s)
    return out


def find_light_stations(img, loose=False, refs=()):
    """Станции днём: белый кружок с серым ободком. Ищем белую середину, отделённую
    ободком от белого же фона - так кружок находится, даже когда на станции стоит поезд.

    loose - мягкие правила для поиска вдоль уже найденной линии (там ошибиться труднее).
    refs  - точки, где станция есть наверняка (центры колец Start/Goal): все станции на
            кадре одного размера, поэтому по ним отсеиваются буквы надписей и окна поездов.
    """
    h, w = img.shape[:2]
    lo = img.min(axis=2)
    hi = img.max(axis=2)
    n, _, stats, _ = cv2.connectedComponentsWithStats((lo >= 225).astype(np.uint8), connectivity=4)
    bx, by, bw, bh, area = (stats[:, k] for k in range(5))
    small = np.maximum(bw, bh) <= 6
    ok = ((bw >= 2) & (bh >= 2) & (bw <= 18) & (bh <= 18) & (np.abs(bw - bh) <= 2)
          & (area >= np.where(small, 0.45, 0.55) * bw * bh))
    cand = []
    ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    ang24 = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    for i in np.nonzero(ok)[0]:
        if i == 0:
            continue
        cx, cy = bx[i] + (bw[i] - 1) / 2.0, by[i] + (bh[i] - 1) / 2.0
        rr = (bw[i] + bh[i]) / 4.0 + 0.5
        closed = np.zeros(16, bool)     # вокруг середины нет разрывов
        grayish = np.zeros(16, bool)    # и это именно серый ободок, а не буква или значок
        dark = 0
        for q in (rr, rr + 0.8):
            xs = np.clip(np.round(cx + q * np.cos(ang)).astype(int), 0, w - 1)
            ys = np.clip(np.round(cy + q * np.sin(ang)).astype(int), 0, h - 1)
            l, u = lo[ys, xs].astype(int), hi[ys, xs].astype(int)
            closed |= l < 225
            grayish |= (u >= 115) & (l <= 212) & (u - l <= 30)
            if q == rr:
                dark = int((u < 115).sum())
        # белая середина уже отделена от фона (иначе слилась бы с ним), поэтому по окружности
        # достаточно большинства точек: у маленького кружка ободок ромбом и углы выпадают
        tiny = max(bw[i], bh[i]) <= 5
        if closed.sum() < (9 if loose else 10):
            continue
        # На самом мелком масштабе кружок поверх поезда - это 2-4 белых пикселя, а ободок
        # сливается с поездом. Вдоль уже найденной линии такому кружку верим и без ободка.
        if grayish.sum() < (6 if loose else 8) and not (loose and tiny):
            continue
        if dark > (8 if loose and tiny else 6):   # тёмная обводка кругом - буква или окно поезда
            continue
        on_line = True
        if not loose:
            # станция стоит на линии: рядом с ободком есть цветные (не серые) пиксели
            xs = np.clip(np.round(cx + (rr + 3.0) * np.cos(ang24)).astype(int), 0, w - 1)
            ys = np.clip(np.round(cy + (rr + 3.0) * np.sin(ang24)).astype(int), 0, h - 1)
            on_line = ((hi[ys, xs].astype(int) - lo[ys, xs]) >= 14).sum() >= 2
        if on_line:
            cand.append((float(cx), float(cy), float(rr + 0.5), (bw[i] + bh[i]) / 2.0))
    # эталонный размер: станция под кольцом Start/Goal, иначе самый частый размер на кадре
    size = None
    known = [c[3] for c in cand if any(math.hypot(c[0] - r[0], c[1] - r[1]) <= 7 for r in refs)]
    if known:
        size = sum(known) / len(known)
    elif len(cand) >= 3:
        vals, counts = np.unique(np.round([c[3] for c in cand]), return_counts=True)
        if counts.max() >= 3:
            size = float(vals[counts.argmax()])
    if size is not None:
        # крупнее эталона станция быть не может; мельче - может, если её частично закрыл поезд
        under = 2.6 if loose and size <= 5.5 else 1.6
        cand = [c for c in cand if -under <= c[3] - size <= 1.6]
    return [c[:3] for c in cand]


def find_dark_stations(img):
    """Станции ночью: тёмная заливка + светлый ободок."""
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


def find_gold_stations(img):
    """Прокачанные станции: золотая заливка с цифрой и контрастный круглый ободок.

    Жёлтые значки достопримечательностей полые, поэтому одной проверки цвета
    недостаточно: проверяем заполненность кружка и ободок (днём тёмный, ночью светлый).
    """
    h, w = img.shape[:2]
    light = is_light(img)
    red, green, blue = img.astype(np.int16).transpose(2, 0, 1)
    gold = ((red >= 175) & (green >= 100) & (green <= 210) & (blue <= 110)
            & (red - green >= 35) & (green - blue >= 45)).astype(np.uint8) * 255
    # Заполняем отверстия от цифр внутри каждого контура. Замыкание маски всего
    # кадра склеивало маленькие станции с близкими золотыми подписями над ними.
    contours, _ = cv2.findContours(gold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(gold, contours, -1, 255, cv2.FILLED)
    ang = np.linspace(0, 2 * np.pi, 32, endpoint=False)
    out = []
    for b in blobs(gold, 10):
        if not (5 <= b["w"] <= 32 and 5 <= b["h"] <= 32 and abs(b["w"] - b["h"]) <= 2):
            continue
        if b["area"] < 0.6 * b["w"] * b["h"]:
            continue
        cx, cy = b["x"] + (b["w"] - 1) / 2, b["y"] + (b["h"] - 1) / 2
        rr = (b["w"] + b["h"]) / 4 + 0.5
        rim = np.zeros(32, bool)
        for q in (rr, rr + 1.0):
            xs = np.clip(np.round(cx + q * np.cos(ang)).astype(int), 0, w - 1)
            ys = np.clip(np.round(cy + q * np.sin(ang)).astype(int), 0, h - 1)
            px = img[ys, xs].astype(int)
            neutral = px.max(1) - px.min(1) <= 65
            # На маленьком масштабе светлый ободок смешивается с тёмным фоном;
            # порог 175 терял станцию из-за одного пикселя рядом с поездом.
            rim |= neutral & ((px.max(1) < 100) if light else (px.min(1) >= 140))
        if rim.sum() >= 20:
            out.append((cx, cy, rr + 0.5))
    return out


def line_colors(img, stations):
    """Цвета линии метро: то, что примыкает снаружи к кружкам станций."""
    h, w = img.shape[:2]
    light = is_light(img)
    gray = 14 if light else 22      # днём линия бледная, почти серая
    ang = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    seen = {}
    for cx, cy, r in stations:
        xs = np.clip(np.round(cx + (r + 3.5) * np.cos(ang)).astype(int), 0, w - 1)
        ys = np.clip(np.round(cy + (r + 3.5) * np.sin(ang)).astype(int), 0, h - 1)
        px = img[ys, xs].astype(int)
        cnt = {}
        for p in px:
            if p.max() - p.min() < gray or (not light and np.abs(p - C_BG).max() <= 8):
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


def round_blob(mask, cx, cy):
    """Пятно маски возле (cx, cy) круглое, как заливка кружка станции, а не тонкая полоска.
    Тёмное нутро вагона (Belgian Coast) по цвету почти как кружок станции, но вытянуто
    вдоль линии - без этой проверки бот тянул маршрут на поезд."""
    x0, y0 = max(0, int(cx) - 8), max(0, int(cy) - 8)
    sub = mask[y0:int(cy) + 9, x0:int(cx) + 9]
    n, lab, _, cent = cv2.connectedComponentsWithStats(sub, connectivity=8)
    if n < 2:
        return False
    i = min(range(1, n), key=lambda k: math.hypot(cent[k][0] + x0 - cx, cent[k][1] + y0 - cy))
    ys, xs = np.nonzero(lab == i)
    if len(xs) < 3:
        return False
    lo, hi = np.linalg.eigvalsh(np.cov(np.vstack([xs, ys]).astype(float)))
    return lo > 1e-6 and hi <= 6.25 * lo      # вытянутость (отношение осей) не больше 2.5


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
        self.cols = []        # цвета линии, найденные на этом кадре
        self.error = None


PLAN_F = 3            # шаг сетки поиска пути, px
PLAN_PAD = 40         # клеток сетки за краем экрана: линия может уйти за край и вернуться
COST_LINE, COST_HIDDEN, COST_EMPTY = 1.0, 6.0, 25.0


def make_plan(img, src, goal, stations, known_cols=(), rects=()):
    """Маршрут вдоль линии метро от src до goal через найденные кружки станций.
    known_cols - цвета линии с прошлых кадров этого задания: когда большая часть маршрута
    уже пройдена, на кадре остаётся мало станций и определить цвет заново не всегда удаётся.
    rects - интерфейс поверх карты. Под ним и за краем экрана линию не видно, поэтому путь
    там дешевле, чем по пустой карте: пусть лучше пройдёт под панелью (бот сдвинет карту
    и посмотрит), чем срежет напрямую мимо спрятанных станций - такой перегон игра не примет."""
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
    cols = list(dict.fromkeys(list(line_colors(img, stations)) + list(known_cols)))
    if not cols:
        p.error = "не определил цвет линии"
        return p
    p.cols = cols
    lm = cv2.dilate(line_mask(img, cols), np.ones((3, 3), np.uint8))
    # Кружок станции рвёт линию (золотой - на 20 px и больше), и путь мог обогнуть его
    # по соседней надписи, пройдя мимо станции. Станции стоят на линии - считаем их её частью.
    for cx, cy, r in stations:
        cv2.circle(lm, (int(round(cx)), int(round(cy))), int(math.ceil(r)) + 2, 255, -1)
    # Ищем по всему кадру. Раньше поиск шёл только в рамке вокруг видимых станций, и когда
    # часть линии со станциями уходила под верхнюю панель (Belgian Coast, Nieuwpoort), путь
    # срезал по пустой карте прямо к дальней станции. Дейкстра останавливается на цели,
    # а дорогую пустую карту почти не обходит, так что весь кадр - это быстро.
    f, pad = PLAN_F, PLAN_PAD
    gw, gh = max(1, w // f), max(1, h // f)
    small = cv2.resize(lm, (gw, gh), interpolation=cv2.INTER_AREA) > 0
    hidden = np.zeros((gh, gw), bool)
    for rx0, ry0, rx1, ry1 in rects:
        hidden[max(0, int(ry0 // f)):max(0, int(ry1 // f) + 1), max(0, int(rx0 // f)):max(0, int(rx1 // f) + 1)] = True
    cost = np.full((gh + 2 * pad, gw + 2 * pad), COST_HIDDEN)
    cost[pad:pad + gh, pad:pad + gw] = np.where(small, COST_LINE, np.where(hidden, COST_HIDDEN, COST_EMPTY))
    sh, sw = cost.shape

    def to_s(pt):
        return (min(sw - 1, max(0, int(pt[0] // f) + pad)), min(sh - 1, max(0, int(pt[1] // f) + pad)))

    path = shortest_path(cost, to_s(a), to_s(b))
    if not path:
        p.error = "не нашёл путь по линии"
        return p
    pts = np.array([((x - pad + 0.5) * f, (y - pad + 0.5) * f) for x, y in path])
    p.path = pts
    # точки пути на видимой карте: под интерфейсом и за краем экрана станций не ищем
    vis = np.array([0 <= x - pad < gw and 0 <= y - pad < gh and not hidden[y - pad, x - pad]
                    for x, y in path])
    on = []
    lm_wide = cv2.dilate(lm, np.ones((5, 5), np.uint8))

    def joined(cx, cy, i):
        """От станции до точки пути i - сплошная линия, без пустой карты."""
        n = int(math.hypot(pts[i][0] - cx, pts[i][1] - cy)) + 2
        xs = np.clip(np.round(np.linspace(cx, pts[i][0], n)).astype(int), 0, w - 1)
        ys = np.clip(np.round(np.linspace(cy, pts[i][1], n)).astype(int), 0, h - 1)
        return bool(lm_wide[ys, xs].all())

    for cx, cy, r in stations:
        d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        i = int(d.argmin())
        # По толстой линии (уже пройденный участок, крупный масштаб) путь идёт краем
        # и на изгибе срезает угол - мимо центра станции. Станция всё равно на маршруте,
        # если от неё до пути - сама линия, а не пустая карта (надпись рядом не подойдёт).
        # Только сбоку от пути: за его концами (за Goal) на линии бывают окна поездов.
        if d[i] <= max(9, r + 3) or (d[i] <= 14 and 0 < i < len(pts) - 1 and vis[i]
                                     and joined(cx, cy, i)):
            on.append((i, cx, cy))
    # Страховка: станции должны идти строго по одной, пропускать нельзя. Поэтому ещё раз
    # проходим вдоль самой линии и ищем кружки, которые общий поиск мог не узнать.
    if is_light(img):
        for cx, cy, _ in find_light_stations(img, loose=True, refs=(a, b)):
            d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
            i = int(d.argmin())
            if d[i] <= 5 and vis[i] and all(math.hypot(cx - o[1], cy - o[2]) > 10 for o in on):
                on.append((i, cx, cy))
        dm = np.zeros(img.shape[:2], np.uint8)
    else:
        gold = find_gold_stations(img)
        # Если на найденном участке есть только золотые станции, серые детали
        # вагонов не должны добавлять новые остановки резервным поиском.
        def is_gold(cx, cy):
            return any(math.hypot(cx - x, cy - y) <= 2 for x, y, _ in gold)

        all_gold = len(on) >= 2 and all(is_gold(cx, cy) for _, cx, cy in on)
        dm = np.zeros(img.shape[:2], np.uint8) if all_gold else dot_mask(img)
    run = []
    for i, (px, py) in enumerate(pts):
        xi, yi = int(px), int(py)
        cnt = cv2.countNonZero(dm[max(0, yi - 3):yi + 4, max(0, xi - 3):xi + 4]) if vis[i] else 0
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
            if (yellow <= 6 and all(math.hypot(cx - o[1], cy - o[2]) > 10 for o in on)
                    and round_blob(dm, cx, cy)):
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


def ready_legs(plan, view, goal_ok):
    """Сколько перегонов плана можно протянуть прямо сейчас, не двигая карту.
    -> (число перегонов, точка, которую надо открыть сдвигом карты, или None)."""
    for i in range(len(plan.route) - 1):
        b = plan.route[i + 1]
        seg = plan.path[plan.idx[i]:plan.idx[i + 1] + 1]
        bad = [q for q in seg if view.blocked(q, 2)]
        if bad or view.blocked(b, 16) or (i == len(plan.route) - 2 and not goal_ok):
            return i, (tuple(bad[0]) if bad else b)
    return len(plan.route) - 1, None


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


def blue_pins(img, scale):
    """Синие метки Current на кадре -> [(x центра, y нижнего края)].
    Метки (как и булавки Start/Goal) всегда около 41x46 px, при любом размере окна."""
    out = []
    for b in blobs(cmask(img, C_BLUE, 14), 250):
        if 24 <= b["w"] <= 54 and 28 <= b["h"] <= 66:
            out.append((b["x"] + b["w"] / 2.0, float(b["y"] + b["h"])))
    return out


PIN_TIP_TO_STATION = 17    # от острия булавки Start/Goal до центра станции, px (булавка качается на +-4)


def find_pin(img, rgb):
    """Булавка Start / Goal -> примерный центр станции под ней (x, y) или None.

    На мелком масштабе кольцо вокруг станции почти не видно (Belgian Coast на минимальном
    масштабе), а булавка с табличкой «Start» / «Goal» всегда одного размера и висит
    ровно над станцией. Высота качается на несколько пикселей - точное место потом
    уточняется по кружку станции."""
    h, w = img.shape[:2]
    light = is_light(img)
    best = None
    for b in blobs(cmask(img, rgb, 16), 300):
        if not (30 <= b["w"] <= 54 and 34 <= b["h"] <= 60):
            continue
        cx = b["x"] + (b["w"] - 1) / 2.0
        # над булавкой - белая табличка с надписью (ночью её видно на тёмной карте;
        # днём карта сама белая, и проверка ничего не даёт)
        x0, x1 = int(max(0, cx - 45)), int(min(w, cx + 46))
        y0, y1 = int(max(0, b["y"] - 34)), int(max(0, b["y"] - 2))
        label = int((img[y0:y1, x0:x1].min(axis=2) >= 235).sum()) if y1 > y0 else 0
        if not light and label < 300:
            continue
        if best is None or label > best[0]:
            best = (label, (float(cx), float(b["y"] + b["h"] + PIN_TIP_TO_STATION)))
    return best and best[1]


def find_marker(img, rgb):
    """Станция Start / Goal -> ((x, y), точно) или (None, False).
    Точно - по кольцу вокруг станции; не точно - по булавке над ней."""
    r = find_ring_ex(img, rgb)
    if r and r[3] >= (0.5 if r[2] >= 15 else 0.62):
        return (r[0], r[1]), True
    p = find_pin(img, rgb)
    return (p, False) if p else (None, False)


def same_marker(a, b):
    """Метка не сдвинулась (камера стоит). Булавка качается вверх-вниз, кольцо - нет."""
    (pa, ea), (pb, eb) = a, b
    if (pa is None) != (pb is None):
        return False
    if pa is None:
        return True
    tol_y = 1.5 if ea and eb else 7.0
    return abs(pa[0] - pb[0]) < 1.5 and abs(pa[1] - pb[1]) < tol_y


def marker_moved(before, after):
    """Камера поехала: метка Goal заметно сдвинулась или пропала. Качание булавки - не сдвиг."""
    (pa, ea), (pb, eb) = before, after
    if pa is None:
        return False
    if pb is None:
        return True
    return abs(pa[0] - pb[0]) > 3 or abs(pa[1] - pb[1]) > (3 if ea and eb else 9)


def has_blue_pin(img, st, scale):
    """Синяя метка Current стоит ровно над станцией st? (метка слегка покачивается)"""
    x, y = st
    return any(abs(px - x) <= 12 * scale and 4 * scale <= y - py <= 36 * scale
               for px, py in blue_pins(img, scale))


def train_near(img, pt, scale):
    """Стоит ли на станции поезд. Пока он там, игра не принимает протяжку к этой станции."""
    h, w = img.shape[:2]
    r = int(round(11 * scale))
    x, y = int(round(pt[0])), int(round(pt[1]))
    x0, y0 = max(0, x - r), max(0, y - r)
    crop = img[y0:y + r + 1, x0:x + r + 1]
    if crop.size == 0:
        return False
    if is_light(img):       # днём поезд тёмный
        train = crop.max(axis=2) < 95
    else:                   # ночью - светлый
        train = crop.min(axis=2) >= 215
    ys, xs = np.ogrid[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]]
    # В углах квадратного окна уже лежат подписи станций, а не ближайшие вагоны.
    train &= (xs - pt[0]) ** 2 + (ys - pt[1]) ** 2 <= (r + 0.5) ** 2
    # Ободок и цифра прокачанной станции имеют тот же цвет, что и поезд.
    # Убираем только сам распознанный значок: вагоны рядом остаются видимыми.
    for cx, cy, radius in find_gold_stations(img):
        if math.hypot(cx - pt[0], cy - pt[1]) <= 3 * scale:
            train &= (xs - cx) ** 2 + (ys - cy) ** 2 > (radius + 1) ** 2
    n = int(train.sum())
    return n >= 10 * scale * scale


def light_dot_near(img, pt, rad=9):
    """Днём: белая середина кружка станции рядом с точкой pt, без проверки ободка.
    Нужна для станции под меткой Current: она стоит на стыке красного и бледного участков
    линии, и её ободок на обычный серый не похож."""
    h, w = img.shape[:2]
    m = int(rad + 12)
    x0, x1 = max(0, int(pt[0]) - m), min(w, int(pt[0]) + m + 1)
    y0, y1 = max(0, int(pt[1]) - m), min(h, int(pt[1]) + m + 1)
    crop = (img[y0:y1, x0:x1].min(axis=2) >= 225).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(crop, connectivity=4)
    best = None
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if bx == 0 or by == 0 or bx + bw >= crop.shape[1] or by + bh >= crop.shape[0]:
            continue        # касается края окна - это фон карты, а не кружок
        if not (2 <= bw <= 18 and 2 <= bh <= 18 and abs(bw - bh) <= 3 and area >= 0.45 * bw * bh):
            continue
        cx, cy = x0 + bx + (bw - 1) / 2.0, y0 + by + (bh - 1) / 2.0
        d = math.hypot(cx - pt[0], cy - pt[1])
        if d <= rad and (best is None or d < best[0]):
            best = (d, (cx, cy))
    return best and best[1]


def find_current(img, stations, rects, scale):
    """Станция под синей меткой Current (докуда уже построен маршрут).
    Если сам кружок не распознан (стоит поезд), берём точку под остриём метки."""
    h, w = img.shape[:2]
    best = None
    for px, py in blue_pins(img, scale):
        if is_blocked((px, py - 20 * scale), rects, w, h, 0):
            continue
        tx, ty = px, py + 18 * scale
        cand = (99.0, (tx, ty))
        for cx, cy, _ in stations:
            d = math.hypot(cx - tx, cy - ty)
            if abs(cx - tx) <= 10 * scale and d <= 22 * scale and d < cand[0]:
                cand = (d, (cx, cy))
        if cand[0] == 99.0 and is_light(img):
            dot = light_dot_near(img, (tx, ty), 9 * scale)
            if dot:
                cand = (50.0, dot)
        if best is None or cand[0] < best[0]:
            best = cand
    return best and best[1]


# --------------------------------------------------------------------------- #
#  Логика бота
# --------------------------------------------------------------------------- #
T0 = time.time()
ZOOM_STEP = 0.85      # один щелчок колеса мыши (замерено: 10 щелчков - в 5.3 раза)

# Города, на которых бот проверен. Город бот узнаёт сам по надписи на кнопке
# «City · Menu» внизу слева. label - эта надпись (PNG в base64, вырезана из кадра
# высотой 1009 px). line - цвет линии метро: бот находит его на каждом кадре сам,
# а этот нужен, когда на экране слишком мало станций, чтобы его определить.
# cooldown - пауза после пройденного задания, сек (если не задана опцией --cooldown).
CITIES = {
    "samara": {"name": "Самара", "line": [(213, 39, 45), (75, 39, 45)], "cooldown": 10.0, "label": (
        "iVBORw0KGgoAAAANSUhEUgAAAFwAAAAVCAAAAADu09YgAAACl0lEQVQ4y2O0Y6AdYAGTjDKSol9f3/xDC8PNYuVB"
        "1M+dy75R1XBmoLGupQIQi9SdXj2mpuFMDAwS6XCewF9qB4seGwPDk6WPJHU8OBachIjySnO+fAm2iI0dGFy/WFTZ"
        "boNCTEnwyav/DNjVMKuz3viJKgE0XBlIHTjK8PjUFtsNICHZQHNeIPVx7dbfDAxeSQwMM0+3iTF8bb8kVqXEwHCv"
        "5wlWNUfaZRhKbqFKAMNcR5OBQfL+KwaGr9dBZkeXKrODaA5DsRMMDBqGDAz3g+WAztM/0KAIFBY02fYfm5ogoBt3"
        "vUWVABrOZcPAwO3sKsv0HOxjJicg8Y8RSCg+eALWqC0MEufyAVMMPM8eYFMjygAyHFUCGCzHrwOdziDi5vZh3Uag"
        "8RdvsW268ZwtIhCo4zjYuP+bz5j4Ad3+evW7MDUGBgVsahgeH3kg9QFNAmj4/4YcW0haSVLpAVJNn4DE98V+zODY"
        "AIIN8xkuOAJDsu4pw/9aBgZhbGruVkOyCIoEKBN9717hai4JkrI7DEwuLLF6wnzAAGNg4IRoBEYHwxdeMP0Fknox"
        "1eyBZj8UCUj2fzxvnqR1IFC/1UkGpQ4OggkYpxpUCZDhzEzAZPN8zetiYIZiYCgBSn978JdBmwm34TjVoEqADM9S"
        "6nrOAPHgMwZeGQaGOyX/GDiX4DYcpxo0CaDhYa4MEw/e/KXoDeTeZuAGkr//MXDVsuJ2OE41aBIsDGoxwDTv7g7m"
        "Pd3P8PIrN4PmjDdaLHiCHKcaNAkmhluzfsHkPnd+Z/i/DMiQ0mPZi6dwx6kGTQJox5ajfubAsGL4tGcVKD1tYYrm"
        "YPi8ZLs1HrfjVIMqwQip5viEWJ9/gRfD0n+eEyyqcalBlmCkZR0KAIjBDdYSfSRhAAAAAElFTkSuQmCC")},
    "belgian": {"name": "Belgian Coast", "line": [(213, 39, 39), (75, 39, 39)], "cooldown": 5.0, "label": (
        "iVBORw0KGgoAAAANSUhEUgAAAHMAAAATCAAAAACG3G1gAAAD00lEQVRIx8VWbVBUVRh+7mX37l0XXFjWBdb4Wvlw"
        "JJUhR2o2JEsl22ymcRgZ0ilNwmxygsloynKacbQa0/Jrcsom0SwZkmnwAwbURJtGqdXFAI1dNg0hPgxlcYG7e7md"
        "e9ldbjDs8IOp8+Pc95z3Oec5H8973kstwn9eFJPExVLunink3KghX3dnXZe8/xPm2iF5ezfz00dBpnloUfyMu9Ya"
        "72Q5MyMkK/8Dm3xfbNvkF06tylUCSZlP7bgbFPfk69ja4DtbzkkZQxV5tiDwN6kHEzufzicHdS+aTlnzafDFhYDy"
        "32fHZrClbCixZucneR2lTj/khSdUl0+8h4oqFKusBzHzudlG7uYxO7BHZa1Zl3L7c7sI074Efn8tNJtCDwBpeSah"
        "9dtmyMCGF+eGdjWfbtmSChQNVVVInEwiZqlwBTBvpoFH5r9zY4RzQw6wJC0G5MKj2XAwO4mhWjCnuJ00M8zhSHm3"
        "kCOwOWpU1wIPPlRyWPwG2Un6vN0XZOD34wCjsaNFrwV0CBvZZ8xnpKo6CraQtu9SlxjWlkiUUUvhquBXBE6Gq068"
        "0Jm+alrWcXHMJYdFH5ncSMx44KroFzho1lOD5VipLqh3B8CGOFSXxmZV4NCjK3DM2S6LlYW/1SWHw/EwOgypCkmA"
        "Jho/lMM2ekeHo+KN3YNsnGg3fIyhV6AXzWnAoA+RHIbKMqhypyfbAuB+ryKbbfreg+szgGab7z7bSqDfmFrsnAnk"
        "iG066o74IXpuBYZG5fnaUkpyi5ULaPf13wISfPqLAcjOG3NhbAiA3QfXqbOzC7448683gXfB9WsqnUCUfu4PsaNP"
        "8pFHYMEvyAxwzl+Gq9bhvLCxaiQyeb6uF3g8opIMmWWFCeiWgat/XpKWoVhbJRBb6ecMsyByGfBXN8eYTjr15tMj"
        "R9U8oH4mVmMKzB0NfPN7lmZcBNw6v1i39+L9pIXwXOYYyx1YwLU8Ngp+9eapE9vmhdD8APCsx9MkceoKxdpmFw4X"
        "JOwSKNDl0lyuIwXUXFxL98/dAmzvi6TGh92XiQnTLaLIPL1lq3VvE+u7+6Ng8/LlmwY0sPKwexUZGfVNtG/Y0J/H"
        "dwio3H57mELr377Ok1tP1e48Kt2dWBxfexnllq7xnK6iIx3DEBrfOouyfWRwz55yGfjKV52Uhv+RxEb3/n4SUqDG"
        "5hVG1xtQTY6mxqV/2Ywih9+p7REmeGJUEfd84tUKfWPBrLaHH3llIvvd4zllxbCPhYuIoH6bMKW5LCR+Yp/AmxgV"
        "+LMHuKnNn1TQnK2Mie5v65vqnE39D/8J/wC/nU/W+KLIxwAAAABJRU5ErkJggg==")},
}
CITY_ALIASES = {"samara": "samara", "самара": "samara",
                "belgian": "belgian", "belgium": "belgian", "coast": "belgian",
                "belgian coast": "belgian", "бельгия": "belgian"}
UNKNOWN_CITY = {"name": "неизвестный город", "line": [], "cooldown": 10.0}
_labels = {}


def city_label(key):
    if key not in _labels:
        data = np.frombuffer(base64.b64decode(CITIES[key]["label"]), np.uint8)
        _labels[key] = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    return _labels[key]


def detect_city(img):
    """Город по надписи на кнопке «City · Menu» внизу слева -> ключ CITIES или None.
    Интерфейс игры меняет размер вместе с окном (1366x768 - 0.79, 2560x1369 - 1.08),
    поэтому надпись ищем в нескольких масштабах."""
    h, w = img.shape[:2]
    area = img[int(h * 0.6):int(h * 0.96), 0:int(min(w * 0.25, 480))]
    if area.shape[0] < 30 or area.shape[1] < 60:
        return None
    gray = cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    best, key = 0.0, None
    for k in CITIES:
        for f in np.arange(0.6, 1.62, 0.04):
            t = cv2.resize(city_label(k), None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1]:
                continue
            r = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
            score = max(float(r.max()), -float(r.min()))   # ночью надпись светлая, днём тёмная
            if score > best:
                best, key = score, k
    return key if best >= 0.75 else None


def app_dir():
    base = sys.executable if getattr(sys, "frozen", False) else __file__
    return os.path.dirname(os.path.abspath(base))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_png(path, img_bgr):
    """cv2.imwrite не умеет пути с русскими буквами (например, имя пользователя Windows)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ok, data = cv2.imencode(".png", img_bgr)
    if ok:
        data.tofile(path)


class View:
    """Всё, что бот видит на одном кадре."""

    def __init__(self, img, hdr):
        self.img = img
        self.h, self.w = img.shape[:2]
        self.scale = self.w / 1920.0
        self.hdr = hdr
        self.rects = ui_rects(self.w, self.h, hdr)
        top = (hdr["y"] if hdr else 0.65 * self.h) - 16
        self.safe = (0.03 * self.w, 0.25 * self.h, 0.82 * self.w, top)   # свободная часть карты
        # G / R - станции Start / Goal на свободной части карты; G_raw / R_raw - где они
        # есть вообще, даже под окном задания или панелью: туда можно сдвинуть карту
        self.G, g_exact, self.G_raw = self._marker(C_GREEN)
        self.R, r_exact, self.R_raw = self._marker(C_RED)
        refs = [p for p, ok in ((self.G, g_exact), (self.R, r_exact)) if p and ok]
        self.stations = [s for s in find_stations(img, refs) if not self.blocked(s, 2)]
        # станция по булавке известна примерно - уточняем по ближайшему кружку под ней
        self.G, self.g_exact = self._snap(self.G, g_exact)
        self.R, self.r_exact = self._snap(self.R, r_exact)
        self.cur = find_current(img, self.stations, self.rects, self.scale)

    def _marker(self, col):
        p, exact = find_marker(self.img, col)
        if p is None or self.blocked(p, 0):
            return None, False, p
        return p, exact, p

    def _snap(self, p, exact):
        if p is None or exact:
            return p, exact
        near = [s for s in self.stations if abs(s[0] - p[0]) <= 8 and abs(s[1] - p[1]) <= 9]
        if not near:
            return p, False
        s = min(near, key=lambda s: math.hypot(s[0] - p[0], s[1] - p[1]))
        return (s[0], s[1]), True

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
        self.city_key = args.city          # None - город узнаём сами по надписи в игре
        self.city_fixed = args.city is not None
        self.city_checked = 0.0
        self.city_warned = False
        self.dbg_dir = os.path.join(app_dir(), "debug")
        self.dbg_n = 0

    @property
    def city(self):
        return CITIES.get(self.city_key, UNKNOWN_CITY)

    def cooldown(self):
        """Пауза после задания: из --cooldown, иначе своя для города (Belgian Coast - 5 с)."""
        return self.args.cooldown if self.args.cooldown is not None else self.city["cooldown"]

    def update_city(self, img):
        """Раз в несколько секунд смотрим, какой город открыт в игре (его можно сменить на ходу)."""
        if self.city_fixed or time.time() - self.city_checked < 3:
            return
        self.city_checked = time.time()
        key = detect_city(img)
        if key and key != self.city_key:
            self.city_key = key
            self.city_warned = False
            log(f"Город: {CITIES[key]['name']}")
            ctypes.windll.kernel32.SetConsoleTitleW(f"Guide Bot  |  {CITIES[key]['name']}  |  P - пауза, O - выход")
        elif key is None and self.city_key is None and not self.city_warned:
            self.city_warned = True
            log("Город не узнал - работаю как на любой карте с одной линией")

    def dbg(self, name, img_bgr):
        if not self.args.debug:
            return
        self.dbg_n += 1
        save_png(os.path.join(self.dbg_dir, f"{time.strftime('%H%M%S')}_{self.dbg_n:03d}_{name}.png"), img_bgr)

    def dbg_rgb(self, name, img):
        if self.args.debug:
            self.dbg(name, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

    def vlog(self, msg):
        if self.args.debug:
            log("  " + msg)

    # ---- ожидание остановки камеры ----
    def wait_still(self, timeout=7.0, need_ring=True):
        """Ждём, пока карта перестанет двигаться (по станциям Start/Goal: кольцо или булавка)."""
        last, stable, t0 = None, 0, time.time()
        while time.time() - t0 < timeout:
            img = self.g.grab()
            st, hdr = ui_state(img)
            if st != "task":
                return None
            cur = (find_marker(img, C_GREEN), find_marker(img, C_RED))
            if last is not None and (cur[0][0] or cur[1][0] or not need_ring):
                same = all(same_marker(a, b) for a, b in zip(cur, last))
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
        if is_light(v.img):       # днём свободное место - светлое, ночью - тёмное
            busy = (v.img.min(axis=2) < 200).astype(np.uint8)
        else:
            busy = (v.img.max(axis=2) >= 100).astype(np.uint8)    # надписи, значки, кольца, поезда
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
        return self.zoom(((v.safe[0] + v.safe[2]) / 2, (v.safe[1] + v.safe[3]) / 2), -notches)

    def zoom(self, at, clicks):
        """Колесо мыши над точкой at: clicks > 0 - приблизить, < 0 - отдалить.
        -> (точка, вокруг которой менялся масштаб; во сколько раз изменились расстояния)."""
        g = self.g
        g.wait_ready()
        g.glide(int(at[0]), int(at[1]))
        time.sleep(0.05)
        g._jiggle()
        ax, ay = g.cursor()
        self.vlog(f"{'приближаю' if clicks > 0 else 'отдаляю'} карту на {abs(clicks)} щелчков")
        delta = 120 if clicks > 0 else -120
        for _ in range(abs(clicks)):
            inp = INPUT(type=0)
            inp.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, 0x0800, 0, 0)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            time.sleep(0.03)
        g.sleep(0.4)
        g.park()
        return (ax, ay), ZOOM_STEP ** -clicks

    @staticmethod
    def stations_too_small(v):
        """Карта отдалена так, что кружков станций не разобрать: кольца Start и Goal
        на экране (они почти не меняют размер), а станций под ними не видно.
        Так бывает на Belgian Coast: линия длинная, и на мелком масштабе станции - точки в 2 px."""
        ends = [(p, ok) for p, ok in ((v.G, v.g_exact), (v.R, v.r_exact)) if p is not None and v.clear(p)]

        def has_station(p, exact):
            # не точно = нашли только булавку, а кружка станции под ней нет
            return exact and min((math.hypot(s[0] - p[0], s[1] - p[1]) for s in v.stations),
                                 default=99.0) <= 8

        return bool(ends) and not any(has_station(p, ok) for p, ok in ends)

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
        self.dbg_rgb("task", v.img)
        self.vlog(f"Start {v.G} {'кольцо' if v.g_exact else 'булавка'}, Goal {v.R} "
                  f"{'кольцо' if v.r_exact else 'булавка'}, станций {len(v.stations)}")
        est = {}            # последние известные координаты: start, goal, cur
        had_undo = False
        cols_seen = list(self.city["line"])   # цвета линии: известный для карты + найденные
        fails = searches = train_waits = zoom_ins = 0
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
            if v.G or v.G_raw:
                est["start"] = v.G or v.G_raw
            if v.R or v.R_raw:
                est["goal"] = v.R or v.R_raw
            if v.cur:
                est["cur"] = v.cur
            if v.hdr and self.undo_visible(v.img, v.hdr):
                had_undo = True          # маршрут уже начат (например, в прошлой попытке)
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
                anchor, z = self.zoom_out(v, 4)
                est = {k: (anchor[0] + (p[0] - anchor[0]) * z, anchor[1] + (p[1] - anchor[1]) * z)
                       for k, p in est.items()}
                v = self.wait_still()
                continue

            if zoom_ins < 8 and self.stations_too_small(v):
                zoom_ins += 1
                sx0, sy0, sx1, sy1 = v.safe
                room = min((sx1 - sx0) / (abs(dst[0] - src[0]) + 1), (sy1 - sy0) / (abs(dst[1] - src[1]) + 1))
                mid = ((src[0] + dst[0]) / 2, (src[1] + dst[1]) / 2)
                # Start и Goal поместятся и после приближения - приближаем вокруг середины,
                # иначе вокруг текущей станции: дальше бот сам сдвинет карту вдоль маршрута.
                # Колесо крутим только над картой: над панелью оно листало бы панель.
                at = next((p for p in (mid if room >= 1.5 else None, src, v.G, v.R)
                           if p is not None and v.clear(p)), None)
                if at is not None:
                    if zoom_ins == 1:
                        log("Станции слишком мелкие - приближаю карту")
                    self.dbg_rgb("zoom_in", v.img)
                    # понемногу (2 щелчка - в 1.4 раза) и каждый раз заново смотрим на карту:
                    # у масштаба есть предел, и после него пересчёт координат уже неверен
                    anchor, z = self.zoom(at, 2)
                    est = {k: (anchor[0] + (p[0] - anchor[0]) * z, anchor[1] + (p[1] - anchor[1]) * z)
                           for k, p in est.items()}
                    v = self.wait_still(need_ring=False)
                    continue

            # сколько перегонов можно пройти прямо сейчас, не двигая карту
            plan, legs, trouble = None, 0, None
            src_ok = v.clear(src) and (v.cur is not None or v.G is not None)
            if src_ok:
                plan = make_plan(v.img, src, dst, v.stations, cols_seen, v.rects)
                if not plan.error:
                    cols_seen = plan.cols
                elif v.R is not None and v.clear(v.R):
                    # и старт, и цель видны, но разобрать карту не вышло (поезд, анимация) -
                    # просто смотрим ещё раз
                    self.vlog(f"план не построен: {plan.error}")
                    fails += 1
                    g.sleep(0.6)
                    v = None
                    continue
                else:
                    plan = None      # цель спрятана под интерфейсом - сначала сдвинем карту
            if plan:
                legs, trouble = ready_legs(plan, v, v.R is not None and v.clear(v.R))
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
                    # текущая станция, цель и место, где путь уходит под интерфейс,
                    # помещаются в свободную зону вместе? Тогда ставим их посередине:
                    # иначе линия, которая петляет под панелью, так и осталась бы под ней
                    need = [src, dst] + ([trouble] if trouble else [])
                    nx0, nx1 = min(p[0] for p in need), max(p[0] for p in need)
                    ny0, ny1 = min(p[1] for p in need), max(p[1] for p in need)
                    if nx1 - nx0 <= sx1 - sx0 - 2 * m and ny1 - ny0 <= sy1 - sy0 - 2 * m:
                        vec = (cx - (nx0 + nx1) / 2, cy - (ny0 + ny1) / 2)
                    else:      # не помещаются - показываем проблемное место
                        q = trouble or dst
                        tx = min(sx1 - m, max(sx0 + m, cx - (q[0] - src[0]) / 2))
                        ty = min(sy1 - m, max(sy0 + m, cy - (q[1] - src[1]) / 2))
                        vec = (tx - src[0], ty - src[1])
                moved = (0, 0)
                if math.hypot(*vec) >= 20:
                    moved = self.pan(v, vec)
                if math.hypot(*moved) < 10:
                    fails += 1
                    if zoom_ins == 0:
                        # сдвинуть не вышло - немного отдаляем, чтобы освободить место.
                        # Если карту уже приближали из-за мелких станций, обратно не отдаляем:
                        # иначе бот крутил бы колесо туда-сюда без конца
                        anchor, z = self.zoom_out(v, 3)
                        est = {k: (anchor[0] + (p[0] - anchor[0]) * z, anchor[1] + (p[1] - anchor[1]) * z)
                               for k, p in est.items()}
                else:
                    est = {k: (p[0] + moved[0], p[1] + moved[1]) for k, p in est.items()}
                v = self.wait_still(need_ring=False)
                continue

            # тянем перегоны
            route = plan.route
            ring0 = find_marker(v.img, C_RED)    # Goal до протяжки: сдвинется, если поедет камера
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
                    # метка Current летит к новой станции; на длинном перегоне (Belgian Coast
                    # на крупном масштабе) это дольше полсекунды - ждём её до 1.2 с
                    t_end = time.time() + 1.2
                    while True:
                        ring1 = find_marker(img, C_RED)
                        moved = ring1[0] is not None and marker_moved(ring0, ring1)
                        if not moved and has_blue_pin(img, b, v.scale):
                            break
                        if time.time() >= t_end:
                            ok = False
                            break
                        g.sleep(0.2)
                        img = g.grab()
                if not ok:
                    if train_waits < 40 and (train_near(img, a, v.scale) or train_near(img, b, v.scale)):
                        # это не ошибка: ждём, пока поезд уедет, и не тратим попытку
                        train_waits += 1
                        self.vlog(f"перегон {i + 1}: на станции стоит поезд - жду, пока уедет")
                        g.sleep(0.8)
                    else:
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
            if had_undo:
                # начатый маршрут: пока он не закончен, в окне есть кнопка Undo
                if not self.undo_visible(img, hdr):
                    return True
            else:
                # маршрут из одного перегона: кнопки Undo не бывает, смотрим, поехала ли камера
                if marker_moved(ring0, find_marker(img, C_RED)):
                    return True
            self.g.sleep(0.15)
        return False

    def check(self):
        """Один снимок без движения мыши: что бот видит и какой маршрут построил бы."""
        self.g.focus()
        img = self.g.grab()
        st, hdr = ui_state(img)
        v = View(img, hdr if st == "task" else None)

        def fmt(p):
            return f"({p[0]:.0f}, {p[1]:.0f})" if p else "нет"

        if not self.city_fixed:
            self.city_key = detect_city(img)
        log(f"Город: {self.city['name']}, экран: {st}, карта {'дневная' if is_light(img) else 'ночная'}, "
            f"станций: {len(v.stations)}, Start {fmt(v.G)}{'' if v.g_exact else ' (по булавке)'}, "
            f"Goal {fmt(v.R)}{'' if v.r_exact else ' (по булавке)'}, Current {fmt(v.cur)}")
        if st == "task" and self.stations_too_small(v):
            log("Станции слишком мелкие - в работе бот приблизил бы карту")
        plan = Plan()
        plan.stations = v.stations
        src = v.cur or v.G
        if st != "task":
            log("Окно задания Guide не открыто - возьми задание и запусти проверку ещё раз")
        elif src and v.R:
            plan = make_plan(img, src, v.R, v.stations, self.city["line"], v.rects)
            if plan.error:
                log(f"Маршрут не построен: {plan.error}")
            else:
                log(f"Маршрут: {len(plan.route) - 1} перегон(ов): " + " -> ".join(fmt(p) for p in plan.route))
        else:
            log("Start или Goal не видно - в работе бот сдвинул бы или отдалил карту")
        path = os.path.join(self.dbg_dir, f"check_{time.strftime('%H%M%S')}.png")
        save_png(path, draw_plan(img, plan, 0, v.rects))
        log(f"Разметка сохранена: {path}")

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
            self.update_city(img)

        if st == "quit":
            if self.quitting:      # сами решили бросить зависшее задание
                g.click(int(d["cx"]), int(d["cy"]))
                self.quitting = False
                self.tries = 0
                self.next_guide_at = time.time() + self.cooldown()
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
                self.next_guide_at = time.time() + self.cooldown()
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
    ap.add_argument("--cooldown", type=float, default=None,
                    help="пауза после пройденного задания, сек (по умолчанию 10, на Belgian Coast 5)")
    ap.add_argument("--no-maximize", action="store_true", help="не разворачивать окно игры")
    ap.add_argument("--city", help="город: samara или belgian (без опции бот узнаёт город сам)")
    ap.add_argument("--check", action="store_true",
                    help="один снимок без мыши: показать, что бот видит, и сохранить разметку в debug/")
    args = ap.parse_args()
    if args.once:
        args.count = 1
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    k32 = ctypes.windll.kernel32
    if not args.check:
        k32.CreateMutexW(None, False, "GuideBot_single_instance")
        if k32.GetLastError() == 183:      # уже запущен: два бота дрались бы за мышь
            print("Guide Bot уже запущен в другом окне.")
            return
    print(BANNER, flush=True)
    k32.SetConsoleTitleW("Guide Bot  |  P - пауза, O - выход")
    if args.city:
        key = CITY_ALIASES.get(args.city.strip().lower())
        if not key:
            print(f"Не знаю город «{args.city}». Есть: samara, belgian. Определю город сам.")
        args.city = key
    if args.city:
        log(f"Город: {CITIES[args.city]['name']} (задан в --city)")
    bot = None
    try:
        bot = Bot(args)
        if args.check:
            bot.check()
            return
        bot.run()
    except (Stop, KeyboardInterrupt):
        pass
    finally:
        _mouse(M_LUP)
        if bot and not args.check:
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
