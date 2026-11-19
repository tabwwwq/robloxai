"""Регрессии распознавания на исходных снимках, без мыши и запуска игры.

Запуск на Windows: python -m unittest discover -s tests -v
"""
from pathlib import Path
import unittest

import cv2
import numpy as np

import guide_bot as bot


FIXTURES = Path(__file__).parent / "fixtures"
GOLD_STATIONS = [
    (2087.5, 303.5), (1959.5, 431.5), (1831.5, 559.5),
    (999.5, 655.5), (1175.5, 655.5), (1351.5, 655.5),
    (1527.5, 655.5), (1703.5, 655.5), (855.5, 783.5),
    (727.5, 911.5), (567.5, 1071.5),
]
GOLD_ROUTE = [(1704, 656), (1528, 656), (1352, 656),
              (1176, 656), (1000, 656), (856, 784)]
LIGHT_GOLD_ROUTE = [(94, 575), (200, 481), (330, 481), (459, 481),
                    (589, 481), (718, 481), (812, 410), (907, 316), (1001, 222)]


def load_image(name):
    data = np.fromfile(FIXTURES / name, dtype=np.uint8)
    return cv2.cvtColor(cv2.imdecode(data, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


class VisionTests(unittest.TestCase):
    def test_gold_stations_exclude_landmarks_and_ui(self):
        for name in ("gold_stations.png", "gold_stations_trains.png"):
            with self.subTest(image=name):
                img = load_image(name)
                _, hdr = bot.ui_state(img)
                stations = bot.View(img, hdr).stations
                self.assertEqual(len(stations), len(GOLD_STATIONS))
                for expected in GOLD_STATIONS:
                    self.assertTrue(any(np.linalg.norm(np.array(s[:2]) - expected) < 1
                                        for s in stations), expected)

    def test_start_ring_ignores_discord_avatar(self):
        for name in ("gold_stations.png", "gold_stations_trains.png"):
            with self.subTest(image=name):
                img = load_image(name)
                state, hdr = bot.ui_state(img)
                self.assertEqual(state, "task")
                view = bot.View(img, hdr)
                np.testing.assert_allclose(view.G, (1703.5, 655.5), atol=1)
                np.testing.assert_allclose(view.R, (855.5, 783.5), atol=1)

    def test_gold_route_includes_every_intermediate_station(self):
        for name in ("gold_stations.png", "gold_stations_trains.png"):
            with self.subTest(image=name):
                img = load_image(name)
                _, hdr = bot.ui_state(img)
                view = bot.View(img, hdr)
                self.assertIsNotNone(view.G)
                self.assertIsNotNone(view.R)
                plan = bot.make_plan(img, view.G, view.R, view.stations)
                self.assertIsNone(plan.error)
                self.assertEqual(plan.route, GOLD_ROUTE)
                self.assertTrue(all(view.clear(p) for p in plan.route))

    def test_gray_stations_and_route_remain_supported(self):
        img = load_image("gray_stations.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        # Goal здесь на границе верхней панели: берём само кольцо, как при
        # оценке позиции станции до сдвига карты.
        start = bot.find_ring(img, bot.C_GREEN)
        goal = bot.find_ring(img, bot.C_RED)
        self.assertIsNotNone(start)
        self.assertIsNotNone(goal)
        plan = bot.make_plan(img, start[:2], goal[:2], view.stations)
        self.assertIsNone(plan.error)
        expected = [(510, 621), (606, 525), (684, 448), (770, 371),
                    (876, 371), (981, 371), (1087, 371), (1192, 371),
                    (1270, 314), (1346, 236)]
        np.testing.assert_allclose(plan.route, expected, atol=1)

    # ---- дневная (светлая) карта ----
    def _plan(self, name):
        img = load_image(name)
        state, hdr = bot.ui_state(img)
        self.assertEqual(state, "task")
        self.assertTrue(bot.is_light(img))
        view = bot.View(img, hdr)
        return img, view

    def test_light_theme_route(self):
        img, view = self._plan("light_stations.png")
        np.testing.assert_allclose(view.G, (653, 550), atol=1)
        np.testing.assert_allclose(view.R, (1267, 458), atol=1)
        plan = bot.make_plan(img, view.G, view.R, view.stations)
        self.assertIsNone(plan.error)
        # на Sovetskaya (1140, 458) стоит поезд - станция всё равно должна быть в маршруте
        np.testing.assert_allclose(plan.route, [(653, 550), (758, 458), (885, 458), (1012, 458),
                                                (1140, 458), (1267, 458)], atol=1)

    def test_light_theme_min_zoom(self):
        img, view = self._plan("light_min_zoom.png")
        plan = bot.make_plan(img, view.G, view.R, view.stations)
        self.assertIsNone(plan.error)
        np.testing.assert_allclose(plan.route, [(667, 512), (740, 446), (830, 446), (919, 446),
                                                (1009, 446), (1098, 446)], atol=1)

    def test_light_theme_tiny_station_under_train(self):
        img, view = self._plan("light_train_on_station.png")
        plan = bot.make_plan(img, view.G, view.R, view.stations)
        self.assertIsNone(plan.error)
        # Alabinskaya (542, 516) почти целиком закрыта поездом - пропускать её нельзя
        self.assertEqual(len(plan.route), 10)
        np.testing.assert_allclose(plan.route[1], (542, 516), atol=2)

    def test_light_theme_continues_from_current_station(self):
        img, view = self._plan("light_route_started.png")
        self.assertTrue(bot.Bot.undo_visible(img, view.hdr))
        np.testing.assert_allclose(view.cur, (648.5, 573), atol=1.5)
        plan = bot.make_plan(img, view.cur, view.R, view.stations)
        self.assertIsNone(plan.error)
        np.testing.assert_allclose(plan.route, [(648, 573), (490, 573)], atol=1.5)

    def test_dark_theme_is_not_light(self):
        self.assertFalse(bot.is_light(load_image("gray_stations.png")))
        self.assertFalse(bot.is_light(load_image("gold_stations.png")))

    def test_light_gold_stations_exclude_landmarks(self):
        # Исходный кадр обрезан до карты: проверяем распознавание без масок
        # интерфейса, рассчитанных на целое окно Roblox.
        img = load_image("light_gold_stations.png")
        self.assertTrue(bot.is_light(img))
        stations = bot.find_stations(img)
        self.assertEqual(len(stations), len(LIGHT_GOLD_ROUTE))
        actual = sorted(s[:2] for s in stations)
        np.testing.assert_allclose(actual, sorted(LIGHT_GOLD_ROUTE), atol=1)

    def test_light_gold_route_keeps_stations_below_landmark_labels(self):
        img = load_image("light_gold_stations.png")
        stations = bot.find_stations(img)
        for route in (LIGHT_GOLD_ROUTE, LIGHT_GOLD_ROUTE[::-1]):
            with self.subTest(start=route[0]):
                plan = bot.make_plan(img, route[0], route[-1], stations)
                self.assertIsNone(plan.error)
                # Gagarinskaya и Sovetskaya расположены вплотную к золотому
                # тексту достопримечательностей. Они не должны теряться.
                np.testing.assert_allclose(plan.route, route, atol=1)

    def test_gold_station_border_is_not_a_train(self):
        cases = [("light_gold_stations.png", 1.0,
                  [(200, 481), (459, 481), (589, 481), (718, 481), (812, 410)]),
                 ("gold_stations.png", 2560 / 1920,
                  [(1704, 656), (1528, 656), (1000, 656), (856, 784)])]
        for name, scale, points in cases:
            img = load_image(name)
            for point in points:
                with self.subTest(image=name, station=point):
                    self.assertFalse(bot.train_near(img, point, scale))

    def test_trains_near_gold_and_gray_stations_are_still_detected(self):
        cases = [("light_gold_stations.png", (330, 481), 1.0),
                 ("gold_stations_trains.png", (1176, 656), 2560 / 1920),
                 ("light_stations.png", (1140, 458), 1.0)]
        for name, point, scale in cases:
            with self.subTest(image=name):
                self.assertTrue(bot.train_near(load_image(name), point, scale))

    def test_small_gold_route_does_not_skip_moskovskaya_or_add_train_windows(self):
        expected = [(535, 533), (608, 467), (698, 467), (787, 467), (877, 467),
                    (967, 467), (1032, 419), (1097, 353), (1162, 288)]
        for name in ("gold_small_zoom_train.png", "gold_small_zoom_windows.png"):
            with self.subTest(image=name):
                img = load_image(name)
                _, hdr = bot.ui_state(img)
                view = bot.View(img, hdr)
                plan = bot.make_plan(img, view.cur, view.R, view.stations)
                self.assertIsNone(plan.error)
                np.testing.assert_allclose(plan.route, expected, atol=1)

    def test_route_does_not_bypass_big_gold_station(self):
        # Днём кружок Sovetskaya рвёт бледную линию на 20 px, и путь огибал его в 10 px
        # от центра: бот тянул Pobeda -> Sportivnaya, игра не принимала - 20 минут неудач.
        img = load_image("samara_gold_station_skipped.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        route = [(1140, 548), (982, 548), (823, 548), (665, 548)]
        for expected in (route, route[::-1]):
            with self.subTest(start=expected[0]):
                plan = bot.make_plan(img, expected[0], expected[-1], view.stations,
                                     bot.CITIES["samara"]["line"], view.rects)
                self.assertIsNone(plan.error)
                np.testing.assert_allclose(plan.route, expected, atol=2)


class PinAndCityTests(unittest.TestCase):
    def test_pin_points_at_the_station_under_the_ring(self):
        for name, col in (("light_stations.png", bot.C_GREEN), ("light_stations.png", bot.C_RED),
                          ("gray_stations.png", bot.C_RED)):
            with self.subTest(image=name, color=col):
                img = load_image(name)
                ring, pin = bot.find_ring(img, col), bot.find_pin(img, col)
                self.assertIsNotNone(pin)
                self.assertLess(abs(pin[0] - ring[0]), 2)
                self.assertLess(abs(pin[1] - ring[1]), 9)

    def test_goal_found_by_pin_when_ring_is_hidden(self):
        # кольцо Goal закрыто окном задания и значком «1», булавка видна
        img = load_image("goal_ring_under_window.png")
        self.assertIsNone(bot.find_ring(img, bot.C_RED))
        p, exact = bot.find_marker(img, bot.C_RED)
        self.assertFalse(exact)
        np.testing.assert_allclose(p, (605, 666), atol=2)

    def test_city_is_detected_from_the_label(self):
        cases = [("gray_stations.png", "samara"), ("light_stations.png", "samara"),
                 ("gold_stations.png", "samara"),               # 2560x1369
                 ("gold_tiny_zoom_task_start.png", "samara"),   # 1366x768
                 ("belgian_min_zoom_menu.png", "belgian")]
        for name, city in cases:
            with self.subTest(image=name):
                self.assertEqual(bot.detect_city(load_image(name)), city)

    def test_unknown_city_without_label(self):
        # фрагмент карты без кнопки города
        self.assertIsNone(bot.detect_city(load_image("light_gold_stations.png")))


class ZoomTests(unittest.TestCase):
    def test_zooms_in_when_stations_are_too_small(self):
        # начало задания на мелком масштабе: Start и Goal видны, кружков станций не разобрать
        img = load_image("gold_tiny_zoom_task_start.png")
        _, hdr = bot.ui_state(img)
        self.assertTrue(bot.Bot.stations_too_small(bot.View(img, hdr)))

    def test_does_not_zoom_in_on_normal_frames(self):
        for name in ("gold_stations.png", "gray_stations.png", "light_stations.png",
                     "light_min_zoom.png", "gold_small_zoom_train.png"):
            with self.subTest(image=name):
                img = load_image(name)
                _, hdr = bot.ui_state(img)
                self.assertFalse(bot.Bot.stations_too_small(bot.View(img, hdr)))


class BelgianCoastTests(unittest.TestCase):
    """Карта Belgian Coast ночью: длинная почти прямая линия K вдоль моря, на линии
    красные значки K, поезда-прямоугольники и жёлтые значки достопримечательностей.
    Снимки - фрагменты экрана без окна задания, поэтому концы маршрута заданы вручную."""

    ROUTES = {
        # поезда и значок достопримечательности возле Blankenberge Pier
        "belgian_blankenberge.png": [(1090, 33), (980, 132), (896, 210), (763, 344),
                                     (674, 432), (592, 516), (502, 604)],
        # Oosteroever -> Vismijn -> Station: линия делает крюк
        "belgian_oostende_bend.png": [(1040, 68), (946, 162), (968, 272), (802, 378),
                                      (718, 434), (652, 500), (591, 561), (530, 622),
                                      (469, 683), (386, 766), (324, 828)],
        # петля через Nieuwpoort Stad
        "belgian_nieuwpoort_loop.png": [(610, 48), (541, 114), (442, 230), (502, 302),
                                        (510, 441), (403, 422), (338, 365), (260, 358),
                                        (184, 442), (116, 510), (56, 568)],
        # мелкий масштаб: поезда - белые квадратики, часть станций находит только
        # резервный поиск вдоль линии
        "belgian_small_zoom.png": [(831, 94), (768, 166), (709, 223), (659, 274),
                                   (615, 318), (547, 385), (504, 429), (461, 474),
                                   (399, 534), (338, 594), (278, 656)],
        # конец линии: De Panne Esplanade -> Kerk -> Moeder Lambic -> Pretpark -> Station
        "belgian_de_panne.png": [(622, 183), (538, 267), (453, 352), (368, 437),
                                 (307, 502), (250, 578), (322, 639), (444, 625),
                                 (533, 695), (533, 803)],
    }

    def test_sea_does_not_make_night_map_light(self):
        for name in self.ROUTES:
            with self.subTest(image=name):
                self.assertFalse(bot.is_light(load_image(name)))

    def test_k_badges_are_not_stations(self):
        img = load_image("belgian_blankenberge.png")
        stations = bot.find_stations(img)
        for badge in ((937, 170), (547, 560)):
            self.assertFalse(any(np.hypot(s[0] - badge[0], s[1] - badge[1]) < 20 for s in stations), badge)

    def test_route_visits_every_station_both_ways(self):
        for name, route in self.ROUTES.items():
            img = load_image(name)
            stations = bot.find_stations(img)
            for expected in (route, route[::-1]):
                with self.subTest(image=name, start=expected[0]):
                    plan = bot.make_plan(img, expected[0], expected[-1], stations,
                                         bot.CITIES["belgian"]["line"])
                    self.assertIsNone(plan.error)
                    np.testing.assert_allclose(plan.route, expected, atol=2)

    def test_task_start_finds_start_and_goal_under_the_interface(self):
        # Начало задания: игра сильно приблизила карту, кольца ~230 px. Start под окном
        # задания, Goal под верхней панелью - бот должен знать, где они, и сдвинуть карту.
        img = load_image("belgian_task_start.png")
        state, hdr = bot.ui_state(img)
        self.assertEqual(state, "task")
        self.assertEqual(bot.detect_city(img), "belgian")
        view = bot.View(img, hdr)
        self.assertIsNone(view.G)
        self.assertIsNone(view.R)
        np.testing.assert_allclose(view.G_raw, (664, 801), atol=3)
        np.testing.assert_allclose(view.R_raw, (1256, 208), atol=3)
        self.assertFalse(bot.Bot.stations_too_small(view))

    def test_task_start_route_on_dim_line(self):
        img = load_image("belgian_task_start.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        # Casino -> De Greefplein -> Raversijde Provinciedomein; значок K между ними - не станция
        route = [(850, 614), (944, 520), (1162, 302)]
        for expected in (route, route[::-1]):
            plan = bot.make_plan(img, expected[0], expected[-1], view.stations,
                                 bot.CITIES["belgian"]["line"])
            self.assertIsNone(plan.error)
            np.testing.assert_allclose(plan.route, expected, atol=2)

    def test_mid_zoom_task_needs_zoom_in(self):
        # после отдаления станции - точки 2x2 px: бот должен приближать, а не отдалять
        img = load_image("belgian_task_mid_zoom.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        np.testing.assert_allclose(view.G, (715.5, 549.5), atol=3)
        np.testing.assert_allclose(view.R, (916, 348), atol=3)
        self.assertTrue(bot.Bot.stations_too_small(view))

    def test_line_under_top_panel_is_not_cut_short(self):
        # Снимок пользователя (Nieuwpoort), вставленный в кадр 1920x1009 на своё место.
        # Жёлтая линия протяжки стёрта: так карта выглядела, когда бот строил маршрут.
        # Victorlaan и Bad - под зоной верхних панелей, поэтому их нет среди станций.
        # Раньше путь искался только возле видимых станций, срезал по пустой карте,
        # и бот тянул Cardijnlaan -> Sint Bernardusplein - игра такой перегон не принимала.
        img = load_image("belgian_nieuwpoort_under_panel.png")
        view = bot.View(img, None)
        np.testing.assert_allclose(view.cur, (1146, 306), atol=2)
        for hidden in ((1061, 233), (956, 222)):         # Victorlaan, Bad
            self.assertFalse(any(np.hypot(s[0] - hidden[0], s[1] - hidden[1]) < 10 for s in view.stations))
        plan = bot.make_plan(img, view.cur, (768, 422), view.stations,
                             bot.CITIES["belgian"]["line"], view.rects)
        self.assertIsNone(plan.error)
        for hidden in ((1061, 233), (956, 222)):         # путь идёт по линии через них
            self.assertLess(np.hypot(plan.path[:, 0] - hidden[0], plan.path[:, 1] - hidden[1]).min(), 10)
        legs, trouble = bot.ready_legs(plan, view, True)
        self.assertEqual(legs, 0)                        # сначала сдвинуть карту, а не тянуть
        self.assertLess(trouble[1], 245)

    def test_leg_accepted_without_current_pin(self):
        # В игре пропали метки Start/Current над станциями. Перегон Heist Duinbergen ->
        # Heist Kerk игра приняла (закрашен красным, Your route: 1 stops), а бот ждал синюю
        # метку и считал перегон не засчитанным - и тянул его снова и снова.
        img = load_image("belgian_no_pins_leg_drawn.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        start, kerk, next_st = (1152, 298), (1086, 370), (1033, 423)
        self.assertEqual(bot.blue_pins(img, view.scale), [])
        np.testing.assert_allclose(view.G, start, atol=2)
        np.testing.assert_allclose(view.cur, kerk, atol=2)     # конец красного маршрута
        self.assertTrue(view.cur_by_route)

        def line(p, q):
            return [(p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t) for t in np.linspace(0, 1, 30)]

        self.assertTrue(bot.leg_drawn(0.0, bot.route_cover(img, line(start, kerk), start, kerk)))
        self.assertFalse(bot.leg_drawn(0.0, bot.route_cover(img, line(kerk, next_st), kerk, next_st)))

    def test_no_route_end_before_the_route_starts(self):
        img = load_image("belgian_task_start.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        self.assertEqual(bot.route_end(img, view.G_raw, view.stations), (None, None))

    def test_leg_with_k_badge_counts_as_drawn(self):
        # На перегоне Koksijde Lejeunelaan -> Schipgat стоит красный значок K: ещё до
        # протяжки участок был закрашен на 0.71, и прирост до 1.0 бот не засчитывал.
        img = load_image("belgian_leg_past_k_badge.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        a, b = (788, 477), (845, 418)
        seg = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for t in np.linspace(0, 1, 30)]
        self.assertTrue(bot.leg_drawn(0.71, bot.route_cover(img, seg, a, b)))
        self.assertFalse(bot.leg_drawn(0.71, 0.75))
        np.testing.assert_allclose(view.cur, b, atol=2)       # конец красного маршрута

    def test_route_end_off_screen_is_not_guessed(self):
        # Маршрут из 7 перегонов уходит за верхний край: конца не видно, и бот не должен
        # тянуть от Start или от середины маршрута - это обрезало бы маршрут.
        img = load_image("belgian_route_end_offscreen.png")
        _, hdr = bot.ui_state(img)
        view = bot.View(img, hdr)
        self.assertIsNotNone(view.route_far)
        self.assertLess(view.route_far[1], 200)               # дальняя точка - у верхнего края
        # гость там, где кончается красное, а это место под верхней панелью:
        # бот сначала сдвинет карту, а не потянет перегон
        self.assertEqual(view.cur, view.route_far)
        self.assertFalse(view.clear(view.cur))

    def test_tiny_station_on_bend_is_found(self):
        # De Panne Esplanade на изгибе линии: заливка 4x4, ободок ближе обычного.
        # Без неё бот тянул Kerk -> Centrum, и игра раз за разом не принимала перегон.
        img = load_image("belgian_tiny_station_on_bend.png")
        stations = bot.find_stations(img)
        for st in ((410, 416.5), (456.5, 356), (467, 466.5)):     # Esplanade, Centrum, Kerk
            self.assertTrue(any(np.hypot(s[0] - st[0], s[1] - st[1]) < 3 for s in stations), st)

    def test_goal_ring_found_when_camera_centred_by_button(self):
        # Кнопка Goal ставит станцию в центр экрана; внутри кольца тогда название
        # станции и синий значок - кольцо всё равно должно находиться.
        img = load_image("belgian_goal_centred_by_button.png")
        ring = bot.find_ring_ex(img, bot.C_RED)
        self.assertIsNotNone(ring)
        np.testing.assert_allclose(ring[:2], (960, 505), atol=3)

    def test_map_motion_is_measured_from_the_map(self):
        # Расчёт сдвига и масштаба расходится с картой (предел масштаба, неточная протяжка),
        # поэтому бот меряет движение карты по ней самой.
        img = load_image("belgian_route_end_offscreen.png")
        h, w = img.shape[:2]
        _, hdr = bot.ui_state(img)
        rects = bot.ui_rects(w, h, hdr)
        for M in (bot.M_shift((60, -45)), bot.M_zoom((900, 400), 1.38)):
            moved = cv2.warpAffine(img, M, (w, h), borderValue=bot.C_BG)
            meas, n = bot.map_motion(img, moved, rects)
            self.assertIsNotNone(meas)
            self.assertGreaterEqual(n, 40)
            for p in ((400, 300), (1000, 600)):
                np.testing.assert_allclose(bot.M_apply(meas, p), bot.M_apply(M, p), atol=3)
            self.assertTrue(bot.motion_agrees(meas, n, M, w, h))

    def test_route_tip_found_at_min_zoom(self):
        # Камера на Goal (кнопкой) и карта отдалена до предела: кольца почти не видно,
        # а конец красного маршрута - где сейчас гость - виден. Значки K не в счёт.
        img = load_image("belgian_min_zoom_route_tip.png")
        _, hdr = bot.ui_state(img)
        rects = bot.ui_rects(img.shape[1], img.shape[0], hdr)
        np.testing.assert_allclose(bot.route_tip(img, rects, (960, 505)), (1205, 260), atol=4)

    def test_no_start_or_goal_ring_without_task(self):
        for name in self.ROUTES:
            with self.subTest(image=name):
                img = load_image(name)
                self.assertIsNone(bot.find_ring(img, bot.C_GREEN))
                self.assertIsNone(bot.find_ring(img, bot.C_RED))


if __name__ == "__main__":
    unittest.main()
