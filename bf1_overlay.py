# -*- coding: utf-8 -*-
"""
战地 1 狙击枪弹道标尺（屏幕悬浮窗）
- 透明背景、置顶、鼠标穿透，可叠加在无边框窗口模式的游戏画面上
- 根据所选枪械的初速 / 阻力 / 重力 / 子弹存活时间计算弹道下坠
- 根据游戏内 FOV、屏幕分辨率、瞄准镜倍率自动换算刻度像素位置
- 支持归零距离（75/150/300m）自动平移刻度原点

数据来源于 sym.gg 解包的战地 1 武器数据（bf1_weapons.json）。

全局热键（游戏中也可切换）：
  Ctrl+Alt+Left / Right  切换上一把 / 下一把枪
  Ctrl+Alt+Up            循环切换归零距离
  Ctrl+Alt+Down          循环切换瞄准镜倍率
  Alt+Shift+X           显示 / 隐藏标尺
"""
import json
import math
import os
import sys
import tkinter as tk
from tkinter import ttk

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEAPONS_FILE = os.path.join(BASE_DIR, "bf1_weapons.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

KEY_COLOR = "#FF00FF"          # 透明色（不要用于任何可见元素）
DEFAULTS = {
    "weapon": "Gewehr 98 Sniper",
    "zero": 75,                  # 归零距离 m
    "zoom": 8,                   # 瞄准镜倍率
    "fov": 90,                   # 游戏内基础 FOV（步兵腰射, 垂直基准）
    "fov_vehicle": 55,           # 载具/固定炮位 FOV (GstRender.FieldOfViewVerticalVehicle)
    "fov_axis": "vertical",      # FOV 基准: BF1 的 FOV 设置即垂直FOV
    "calib": 1.0,                # 像素校准系数
    "color": "#39FF14",          # 标尺颜色（亮绿）
    "max_dist": 500,             # 标尺最大距离 m
    "visible": True,
    "tick_step": 50,             # 主刻度间隔 m
}


def read_game_config():
    """读取战地1配置文件中的 FOV / 载具FOV / 分辨率"""
    path = os.path.join(os.path.expanduser("~"),
                        "Documents", "Battlefield 1", "settings", "PROFSAVE_profile")
    vals = {}
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0].startswith("GstRender."):
                    vals[parts[0][10:]] = parts[1]
    except OSError:
        return None
    try:
        return {
            "fov": float(vals.get("FieldOfViewVertical", 90)),
            "fov_vehicle": float(vals.get("FieldOfViewVerticalVehicle", 55)),
            "res_w": int(float(vals.get("ResolutionWidth", 0))),
            "res_h": int(float(vals.get("ResolutionHeight", 0))),
            "fullscreen_mode": int(float(vals.get("FullscreenMode", 0))),
        }
    except ValueError:
        return None


# ---------------------------------------------------------------- 弹道计算
def time_of_flight(x, v0, k):
    """BF1 阻力模型 dv/dt = -k*v^2 下飞行 x 米所需时间(秒); k=0 表示匀速(火箭)"""
    if k <= 0:
        return x / v0
    return (math.exp(k * x) - 1.0) / (k * v0)


def drop_at(x, v0, k, g):
    """飞行 x 米后的纯重力下坠量(米)"""
    t = time_of_flight(x, v0, k)
    return 0.5 * g * t * t


def _landing_x(theta, v0, k, g, h):
    """以仰角 theta(可为负)、炮口高出目标平面 h 米发射时的落点水平距离"""
    s, c = math.sin(theta), math.cos(theta)
    disc = (v0 * s) ** 2 + 2 * g * h
    if disc < 0:
        return 0.0
    t = (v0 * s + math.sqrt(disc)) / g
    if k <= 0:
        return v0 * c * t
    return math.log(1.0 + k * v0 * c * t) / k


def _flight_time(theta, v0, g, h):
    """以仰角 theta 发射到落地的飞行时间(秒)"""
    s = math.sin(theta)
    return (v0 * s + math.sqrt((v0 * s) ** 2 + 2 * g * h)) / g


def elev_for_range(dist, v0, k, g, h=0.0, ttl=None):
    """精确求解命中 dist 所需的最小仰角(弧度, 负值=需俯瞄)。
    落点距离随仰角先增后减(大阻力下最远在 ~10-12°), 所以粗扫找第一个
    穿过 dist 的区间再二分; ttl 给出时要求飞行时间不超过子弹存活时间;
    无解返回 None。比 drop/dist 近似更准, 对高抛弹道是数量级的修正。"""
    lo, hi = -0.6, 1.45

    def ok(theta, x):
        if x < dist:
            return False
        if ttl is not None and _flight_time(theta, v0, g, h) > ttl:
            return False
        return True

    prev_t = lo
    prev_ok = ok(lo, _landing_x(lo, v0, k, g, h))
    t = lo + 0.02
    while t <= hi:
        x = _landing_x(t, v0, k, g, h)
        cur_ok = ok(t, x)
        if not prev_ok and cur_ok:
            a, b = prev_t, t
            for _ in range(60):
                mid = (a + b) / 2.0
                mid_ok = ok(mid, _landing_x(mid, v0, k, g, h))
                if mid_ok:
                    b = mid
                else:
                    a = mid
            return (a + b) / 2.0
        prev_t, prev_ok = t, cur_ok
        t += 0.02
    return None


def max_range(v0, k, ttl):
    """子弹存活时间限制下的最大射程"""
    if k <= 0:
        return v0 * ttl
    return math.log(1.0 + k * v0 * ttl) / k


def holdover_angle(dist, zero, v0, k, g, h=0.0, ttl=None):
    """
    归零距离为 zero 时，命中 dist 所需的瞄准角偏移（弧度，相对屏幕中心）。
    用精确仰角解算: elev(dist) - elev(zero); 超出射程返回 None。
    """
    a = elev_for_range(dist, v0, k, g, h, ttl)
    if a is None:
        return None
    if zero > 0:
        z = elev_for_range(zero, v0, k, g, h, ttl)
        if z is not None:
            a -= z
    return a


def angle_to_pixels(angle_rad, fov_deg, screen_h, calib=1.0):
    """把角偏移换算成屏幕像素（正值向下）"""
    return math.tan(angle_rad) * calib / math.tan(math.radians(fov_deg) / 2.0) * (screen_h / 2.0)


def effective_vfov(fov, axis, screen_w, screen_h):
    """由基础 FOV 求垂直视场角；开镜后 FOV ≈ 基础 FOV / 倍率"""
    if axis == "vertical":
        return fov
    return math.degrees(2 * math.atan(math.tan(math.radians(fov) / 2.0) * screen_h / screen_w))


# ---------------------------------------------------------------- PNG 导出
# 纯标准库实现: 不依赖 pillow。5x7 点阵数字 + 手写 PNG 编码器。
_FONT_5X7 = {
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x0E, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
}


class _Raster:
    """最小光栅画布: 只画水平/垂直线和 5x7 点阵文字, 输出透明 PNG"""

    def __init__(self, w, h):
        self.w, self.h = int(w), int(h)
        self.buf = bytearray(self.w * self.h * 4)

    def _px(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.buf[i:i + 4] = bytes(color)

    def line(self, x0, y0, x1, y1, color, thick=1):
        x0, y0, x1, y1 = (int(round(v)) for v in (x0, y0, x1, y1))
        if y0 == y1:                       # 水平线, 厚度向下
            for t in range(thick):
                for x in range(min(x0, x1), max(x0, x1) + 1):
                    self._px(x, y0 + t, color)
        elif x0 == x1:                     # 垂直线, 厚度向右
            for t in range(thick):
                for y in range(min(y0, y1), max(y0, y1) + 1):
                    self._px(x0 + t, y, color)

    def text(self, x, y, s, color, scale=1):
        cx = int(x)
        for ch in str(s):
            pat = _FONT_5X7.get(ch)
            if pat is None:
                cx += 4 * scale
                continue
            for r, bits in enumerate(pat):
                for c in range(5):
                    if bits >> (4 - c) & 1:
                        for dy in range(scale):
                            for dx in range(scale):
                                self._px(cx + c * scale + dx,
                                         int(y) + r * scale + dy, color)
            cx += 6 * scale

    @staticmethod
    def text_width(s, scale=1):
        return len(str(s)) * 6 * scale - scale

    def save(self, path):
        import struct
        import zlib

        def chunk(tag, data):
            c = tag + data
            return (struct.pack(">I", len(data)) + c
                    + struct.pack(">I", zlib.crc32(c) & 0xffffffff))

        raw = b"".join(b"\x00" + bytes(self.buf[y * self.w * 4:(y + 1) * self.w * 4])
                       for y in range(self.h))
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 9))
               + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(png)


def render_ruler_png(settings, weapons, weapon_name, sw, sh, out_path):
    """把指定武器的标尺按悬浮窗完全相同的像素几何渲染到透明 PNG。
    图片几何中心 = 屏幕中心，导入 Crosshair X 等工具后刻度位置一致。
    返回输出路径；当前设置下没有可画刻度时返回 None。"""
    w = weapons[weapon_name]
    v0, k, g, ttl = w["v0"], w["drag"], w["g"], w["ttl"]
    h = w.get("h", 0.0)
    zero = 0 if w.get("vehicle") else settings["zero"]
    if w.get("vehicle"):
        vfov = effective_vfov(settings["fov_vehicle"], settings["fov_axis"], sw, sh)
    else:
        vfov = effective_vfov(settings["fov"], settings["fov_axis"], sw, sh) / settings["zoom"]
    step = settings["tick_step"]
    d0 = max(int(w.get("min_dist", step)), step)
    x_max = min(settings["max_dist"], max_range(v0, k, ttl))
    marks = []
    for d in range(d0, int(x_max) + 1, step):
        ang = holdover_angle(d, zero, v0, k, g, h, ttl)
        if ang is None:
            continue
        marks.append((d, angle_to_pixels(ang, vfov, sh, settings["calib"])))
    if not marks:
        return None

    # 与 Overlay.redraw 完全一致的刻度尺寸公式; 文字用 5x7 点阵按 fs 缩放
    fs = max(10, sh // 130)
    zfs = max(9, sh // 160)
    scale = max(1, int(round(fs / 9.0)))
    zscale = max(1, int(round(zfs / 9.0)))
    tl_major = max(5, sh // 220)
    tl_minor = max(3, sh // 380)

    def rgba(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (255,)

    col, yel = rgba(settings["color"]), (245, 210, 60, 255)
    y_end = max(y for _, y in marks)
    y_min = min(y for _, y in marks)
    label_w = max(_Raster.text_width(d, scale) for d, _ in marks if d % 100 == 0)
    cx = tl_major + 5 + label_w + 12      # 主干 x，左侧留边(左右对称)
    # 垂直方向以零位点为图片几何中心: 上下各留 20px 边距,
    # Crosshair X 等工具按图片几何中心对齐屏幕, 这样 Z 点正好在屏幕正中
    CY = max(y_end, -y_min) + 20
    img = _Raster(cx * 2, CY * 2)
    img.line(cx, CY + 10, cx, CY + y_end, col, 1)
    last_label_y = -9999
    for d, y in marks:
        yp = CY + y
        major = (d % 100 == 0)
        tl = tl_major if major else tl_minor
        img.line(cx - tl, yp, cx + tl, yp, col, 2 if major else 1)
        if major and yp - last_label_y >= fs * 1.2:
            img.text(cx + tl + 5, yp - 7 * scale // 2, d, col, scale)
            last_label_y = yp
    img.text(cx + 6, CY + 2, f"Z{zero}", yel, zscale)
    img.save(out_path)
    return out_path


# ---------------------------------------------------------------- 悬浮标尺
class Overlay:
    def __init__(self, root, settings, weapons):
        self.settings = settings
        self.weapons = weapons
        self.root = root

        top = tk.Toplevel(root)
        top.title("BF1 弹道标尺")
        self.top = top
        top.attributes("-topmost", True)
        top.attributes("-transparentcolor", KEY_COLOR)
        top.configure(bg=KEY_COLOR)
        top.attributes("-fullscreen", True)
        top.overrideredirect(True)

        if sys.platform == "win32":
            # 鼠标穿透：WS_EX_TRANSPARENT
            top.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(top.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x20
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)

        self.sw = top.winfo_screenwidth()
        self.sh = top.winfo_screenheight()
        self.cx, self.cy = self.sw // 2, self.sh // 2

        self.canvas = tk.Canvas(top, width=self.sw, height=self.sh,
                                bg=KEY_COLOR, highlightthickness=0)
        self.canvas.pack()

    def redraw(self):
        s = self.settings
        self.canvas.delete("all")
        if not s["visible"]:
            return
        w = self.weapons[s["weapon"]]
        v0, k, g, ttl = w["v0"], w["drag"], w["g"], w["ttl"]
        h = w.get("h", 0.0)
        color = s["color"]

        if w.get("vehicle"):
            # 载具/炮位: 直接使用载具FOV (GstRender.FieldOfViewVerticalVehicle)
            vfov = effective_vfov(s["fov_vehicle"], s["fov_axis"], self.sw, self.sh)
        else:
            vfov = effective_vfov(s["fov"], s["fov_axis"], self.sw, self.sh) / s["zoom"]

        # 下坠标尺（A 版设计：无中心准星，细线 + 大小刻度，数字防重叠）
        c = self.cx, self.cy
        x_max = min(s["max_dist"], max_range(v0, k, ttl))
        step = s["tick_step"]
        # 武器可指定起始距离(如驱逐舰200m内视为无下坠, 不画刻度)
        d0 = max(int(w.get("min_dist", step)), step)
        dists = [d for d in range(d0, int(x_max) + 1, step)]
        if not dists:
            return
        ys = []
        for d in dists:
            ang = holdover_angle(d, s["zero"], v0, k, g, h, ttl)
            if ang is None:
                continue
            ys.append((d, angle_to_pixels(ang, vfov, self.sh, s["calib"])))
        if not ys:
            return
        y_end = self.cy + ys[-1][1]
        y_top = min(self.cy + 10, self.cy + ys[0][1])
        self.canvas.create_line(c[0], y_top, c[0], y_end, fill=color, width=1)

        fs = max(10, self.sh // 130)
        label_font = ("Consolas", fs, "bold")
        last_label_y = -9999
        for d, y in ys:
            yp = self.cy + y
            major = (d % 100 == 0)
            tl = max(5, self.sh // 220) if major else max(3, self.sh // 380)
            self.canvas.create_line(c[0]-tl, yp, c[0]+tl, yp, fill=color,
                                    width=2 if major else 1)
            if major and yp - last_label_y >= fs * 1.2:
                self.canvas.create_text(c[0]+tl+5, yp, text=str(d), fill=color,
                                        font=label_font, anchor="w")
                last_label_y = yp
        # 归零提示：线顶端右侧小黄字
        self.canvas.create_text(c[0]+6, self.cy+2, text=f"Z{s['zero']}",
                                fill="#F5D23C", font=("Consolas", max(9, self.sh // 160)), anchor="w")

        # 信息块（左下角，小字低扰动）：枪名 / 倍率 / 最大有效射程 / 署名
        info = f"{s['weapon']}  x{s['zoom']}  max={int(x_max)}m  |  phantomolithship制作"
        self.canvas.create_text(10, self.sh-10, text=info, fill=color,
                                font=("Consolas", max(9, self.sh // 150)), anchor="sw")


# ---------------------------------------------------------------- 设置面板
class Panel:
    def __init__(self, root, settings, weapons, overlay, on_change):
        self.s, self.weapons, self.overlay, self.on_change = settings, weapons, overlay, on_change
        win = tk.Toplevel(root)
        win.title("BF1 标尺设置 - phantomolithship制作")
        win.attributes("-topmost", True)
        win.configure(bg="#202225")
        win.resizable(False, False)
        self.win = win
        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        frm = tk.Frame(win, bg="#202225", padx=10, pady=10)
        frm.pack()

        def lbl(row, text):
            tk.Label(frm, text=text, bg="#202225", fg="#DDDDDD",
                     font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", pady=2)

        self.weapon_names = sorted(weapons.keys())
        lbl(0, "枪械")
        self.w_var = tk.StringVar(value=self.s["weapon"])
        self.w_box = ttk.Combobox(frm, textvariable=self.w_var, values=self.weapon_names,
                                  state="readonly", width=30)
        self.w_box.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)
        self.w_box.bind("<<ComboboxSelected>>", self._upd_weapon)

        lbl(1, "瞄准镜倍率")
        self.z_var = tk.StringVar(value=str(self.s["zoom"]))
        self.z_box = ttk.Combobox(frm, textvariable=self.z_var, state="readonly", width=6)
        self.z_box.grid(row=1, column=1, sticky="w", pady=2)
        self.z_box.bind("<<ComboboxSelected>>", lambda e: self._apply())

        lbl(2, "归零距离 (m)")
        self.zero_var = tk.StringVar(value=str(self.s["zero"]))
        self.zero_box = ttk.Combobox(frm, textvariable=self.zero_var,
                                     values=["75", "150", "300"], state="readonly", width=6)
        self.zero_box.grid(row=2, column=1, sticky="w", pady=2)
        self.zero_box.bind("<<ComboboxSelected>>", lambda e: self._apply())

        lbl(3, "游戏 FOV (°)")
        self.fov_var = tk.StringVar(value=str(self.s["fov"]))
        tk.Entry(frm, textvariable=self.fov_var, width=8,
                 bg="#2F3136", fg="white", insertbackground="white").grid(
            row=3, column=1, sticky="w", pady=2)
        self.axis_var = tk.StringVar(value=self.s["fov_axis"])
        ttk.Combobox(frm, textvariable=self.axis_var,
                     values=["horizontal", "vertical"], state="readonly", width=12
                     ).grid(row=3, column=2, sticky="w", pady=2)
        tk.Label(frm, text="载具FOV", bg="#202225", fg="#DDDDDD",
                 font=("Microsoft YaHei UI", 9)).grid(row=3, column=3, sticky="w", padx=(8, 2))
        self.fovv_var = tk.StringVar(value=str(self.s.get("fov_vehicle", 55)))
        tk.Entry(frm, textvariable=self.fovv_var, width=6,
                 bg="#2F3136", fg="white", insertbackground="white").grid(
            row=3, column=3, sticky="e", pady=2)

        lbl(4, "像素校准 (%)")
        self.calib_var = tk.StringVar(value=str(int(self.s["calib"] * 100)))
        tk.Entry(frm, textvariable=self.calib_var, width=8,
                 bg="#2F3136", fg="white", insertbackground="white").grid(
            row=4, column=1, sticky="w", pady=2)
        tk.Label(frm, text="先保持100, 打靶偏差大再微调", bg="#202225", fg="#888888",
                 font=("Microsoft YaHei UI", 8)).grid(row=4, column=2, columnspan=2, sticky="w")

        lbl(5, "最大距离 (m)")
        self.md_var = tk.StringVar(value=str(self.s["max_dist"]))
        tk.Entry(frm, textvariable=self.md_var, width=8,
                 bg="#2F3136", fg="white", insertbackground="white").grid(
            row=5, column=1, sticky="w", pady=2)

        lbl(6, "刻度间隔 (m)")
        self.ts_var = tk.StringVar(value=str(self.s["tick_step"]))
        ttk.Combobox(frm, textvariable=self.ts_var,
                     values=["25", "50", "100"], state="readonly", width=6).grid(
            row=6, column=1, sticky="w", pady=2)

        lbl(7, "颜色")
        self.c_var = tk.StringVar(value=self.s["color"])
        ttk.Combobox(frm, textvariable=self.c_var, state="readonly", width=10,
                     values=["#39FF14 亮绿", "#00E5FF 青色", "#FF6D00 橙色",
                             "#FF1744 红色", "#FFFFFF 白色"]).grid(
            row=7, column=1, sticky="w", pady=2)

        self.v_var = tk.BooleanVar(value=self.s["visible"])
        tk.Checkbutton(frm, text="显示标尺", variable=self.v_var, bg="#202225",
                       fg="#DDDDDD", selectcolor="#2F3136",
                       activebackground="#202225", activeforeground="#DDDDDD",
                       command=self._apply).grid(row=8, column=0, columnspan=2, sticky="w", pady=4)

        tk.Button(frm, text="应用 (热键 Alt+Shift+X 隐藏)", command=self._apply,
                  bg="#5865F2", fg="white", font=("Microsoft YaHei UI", 9),
                  relief="flat").grid(row=9, column=0, columnspan=4, sticky="ew", pady=6)

        self.note_lbl = tk.Label(frm, text="", bg="#202225", fg="#F5C542",
                                 font=("Microsoft YaHei UI", 8), wraplength=380,
                                 justify="left")
        self.note_lbl.grid(row=10, column=0, columnspan=4, sticky="w", pady=(4, 0))

        cfg_frm = tk.Frame(frm, bg="#202225")
        cfg_frm.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        tk.Button(cfg_frm, text="从战地1配置读取FOV", command=self._read_cfg,
                  bg="#43B581", fg="white", font=("Microsoft YaHei UI", 9),
                  relief="flat").pack(side="left")
        self.cfg_status = tk.Label(cfg_frm, text="", bg="#202225", fg="#888888",
                                   font=("Microsoft YaHei UI", 8))
        self.cfg_status.pack(side="left", padx=8)
        # 常驻显示: 屏幕分辨率 + 配置文件里的游戏分辨率
        self.res_label = tk.Label(cfg_frm, text="", bg="#202225", fg="#888888",
                                  font=("Consolas", 8))
        self.res_label.pack(side="left", padx=4)
        self._refresh_res_label(None)
        tk.Button(cfg_frm, text="导出标尺PNG", command=self._export_png,
                  bg="#F5A623", fg="white", font=("Microsoft YaHei UI", 9),
                  relief="flat").pack(side="left", padx=8)

    def _export_png(self):
        """把当前武器的标尺按屏幕等大小渲染成透明PNG, 存到程序目录并自动打开"""
        sw, sh = self.overlay.sw, self.overlay.sh
        safe = "".join("_" if c in '\\/:*?"<>|' else c for c in self.s["weapon"])
        out = os.path.join(BASE_DIR, f"标尺_{safe}_{sw}x{sh}.png")
        try:
            path = render_ruler_png(self.s, self.weapons, self.s["weapon"], sw, sh, out)
        except Exception as e:
            self.cfg_status.config(text=f"导出失败: {e}", fg="#FF6D6D")
            return
        if path:
            self.cfg_status.config(text=f"已导出: {path}", fg="#43B581")
            try:
                os.startfile(path)   # 自动打开图片, 方便确认
            except OSError:
                pass
        else:
            self.cfg_status.config(text="导出失败: 当前设置下没有可画刻度", fg="#FF6D6D")

    def _refresh_res_label(self, game_res):
        """更新分辨率显示; game_res=(w,h) 或 None(配置未读到)"""
        cur = f"屏幕 {self.overlay.sw}x{self.overlay.sh}"
        if game_res:
            cur += f" | 游戏 {game_res[0]}x{game_res[1]}"
            if game_res != (self.overlay.sw, self.overlay.sh):
                cur += " ⚠不一致"
        self.res_label.config(text=cur)

        self._sync_zoom_values()

    def _sync_zoom_values(self):
        w = self.weapons[self.w_var.get()]
        self.z_box["values"] = [str(z) for z in w["zoom"]]
        if str(self.s["zoom"]) not in self.z_box["values"]:
            self.z_var.set(self.z_box["values"][len(self.z_box["values"]) // 2])
        self.s["zoom"] = float(self.z_var.get())
        self.note_lbl.config(text=w.get("note", ""))
        if w.get("vehicle") or w.get("no_zero"):
            # 载具/炮位/无归零武器: 锁定为 0 并禁用选项
            self.zero_var.set("0")
            self.zero_box.config(state="disabled")
        else:
            self.zero_box.config(state="readonly")
            if self.zero_var.get() not in ("75", "150", "300"):
                self.zero_var.set("75")

    def _upd_weapon(self, _event=None):
        self.s["weapon"] = self.w_var.get()
        self._sync_zoom_values()
        self._apply()

    def _apply(self):
        s = self.s
        s["weapon"] = self.w_var.get()
        s["zoom"] = float(self.z_var.get())
        s["zero"] = int(self.zero_var.get())
        try:
            s["fov"] = float(self.fov_var.get())
            s["calib"] = max(0.5, min(1.5, float(self.calib_var.get()) / 100.0))
            s["max_dist"] = int(float(self.md_var.get()))
        except ValueError:
            pass
        try:
            s["fov_vehicle"] = float(self.fovv_var.get())
        except ValueError:
            pass
        s["fov_axis"] = self.axis_var.get()
        s["tick_step"] = int(self.ts_var.get())
        s["color"] = self.c_var.get().split()[0]
        s["visible"] = bool(self.v_var.get())
        self.on_change()

    def _read_cfg(self):
        cfg = read_game_config()
        if cfg is None:
            self.cfg_status.config(text="未找到 Documents\\Battlefield 1\\settings\\PROFSAVE_profile",
                                   fg="#FF6D6D")
            self._refresh_res_label(None)
            return
        self.fov_var.set(str(int(cfg["fov"])))
        self.fovv_var.set(str(int(cfg["fov_vehicle"])))
        mode = {0: "独占全屏(无法叠加!)", 1: "窗口", 2: "无边框"}.get(cfg["fullscreen_mode"], "?")
        self._refresh_res_label((cfg["res_w"], cfg["res_h"]))
        hint = ""
        if cfg["res_w"]:
            cur = f"{self.overlay.sw}x{self.overlay.sh}"
            game = f"{cfg['res_w']}x{cfg['res_h']}"
            if game != cur:
                hint = f"  ⚠游戏分辨率{game}与当前屏幕{cur}不同"
        self.cfg_status.config(
            text=f"已读取: 步兵FOV {cfg['fov']:.0f} / 载具FOV {cfg['fov_vehicle']:.0f} / {mode}{hint}",
            fg="#43B581")
        self._apply()

    # 供全局热键调用
    def cycle_weapon(self, step):
        i = self.weapon_names.index(self.w_var.get())
        self.w_var.set(self.weapon_names[(i + step) % len(self.weapon_names)])
        self._upd_weapon()

    def cycle_zero(self):
        if self.weapons[self.w_var.get()].get("vehicle") or self.weapons[self.w_var.get()].get("no_zero"):
            return  # 无归零武器
        opts = [75, 150, 300]
        i = (opts.index(self.s["zero"]) + 1) % len(opts) if self.s["zero"] in opts else 0
        self.zero_var.set(str(opts[i]))
        self._apply()

    def cycle_zoom(self):
        vals = [float(v) for v in self.z_box["values"]]
        if not vals:
            return
        i = (vals.index(self.s["zoom"]) + 1) % len(vals) if self.s["zoom"] in vals else 0
        self.z_var.set(str(vals[i]))
        self._apply()

    def toggle_visible(self):
        self.v_var.set(not self.v_var.get())
        self._apply()


# ---------------------------------------------------------------- 全局热键
class Hotkeys:
    """WH_KEYBOARD_LL 全局键盘钩子, 不依赖消息轮询, 游戏全屏下也稳定.
    回调在 tkinter 主线程执行(钩子随主线程消息泵派发), 可直接操作界面."""

    WH_KEYBOARD_LL = 13
    LLKHF_UP = 0x00000080

    # (名字, 触发键vk, 需要按住的修饰键集合, 需要放开的修饰键集合)
    HOTKEYS = {
        0x25: ("weapon_prev", frozenset((0x11, 0x12)), frozenset()),     # Ctrl+Alt+Left
        0x27: ("weapon_next", frozenset((0x11, 0x12)), frozenset()),     # Ctrl+Alt+Right
        0x26: ("cycle_zero", frozenset((0x11, 0x12)), frozenset()),      # Ctrl+Alt+Up
        0x28: ("cycle_zoom", frozenset((0x11, 0x12)), frozenset()),      # Ctrl+Alt+Down
        0x58: ("toggle", frozenset((0x12, 0x10)), frozenset((0x11,))),   # Alt+Shift+X
    }
    NAME_TO_ID = {name: i for i, name in enumerate(
        ("weapon_prev", "weapon_next", "cycle_zero", "cycle_zoom", "toggle"), start=1)}

    def __init__(self, root, on_key):
        self.root = root
        self.on_key = on_key
        self.ok = False
        self._down = set()
        self._last_fire = 0.0
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        user32.CallNextHookEx.restype = ctypes.c_long
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self._cb = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)(self._proc)
        self._hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._cb, None, 0)
        self._dbg(f"init hook={bool(self._hook)} err={ctypes.get_last_error()}")
        if not self._hook:
            print("警告: 键盘钩子安装失败")
            return
        self.ok = True
        root.bind("<Destroy>", self._on_destroy)

    @staticmethod
    def _dbg(msg):
        try:
            import time as _time
            with open(os.path.join(BASE_DIR, "hotkey_debug.log"), "a", encoding="utf-8") as f:
                f.write(_time.strftime("%H:%M:%S") + " " + msg + "\n")
        except OSError:
            pass

    def _on_destroy(self, _event=None):
        if getattr(self, "_hook", None):
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _proc(self, ncode, wparam, lparam):
        try:
            if ncode == 0 and lparam:
                vk = ctypes.cast(lparam, ctypes.POINTER(wintypes.DWORD))[0]
                flags = ctypes.cast(
                    ctypes.cast(lparam, ctypes.c_void_p).value + 8,
                    ctypes.POINTER(wintypes.DWORD))[0]
                # 左右侧修饰键统一成通用 vk: L/R Shift->0x10, Ctrl->0x11, Alt->0x12
                if vk in (0xA0, 0xA1):
                    vk = 0x10
                elif vk in (0xA2, 0xA3):
                    vk = 0x11
                elif vk in (0xA4, 0xA5):
                    vk = 0x12
                if flags & self.LLKHF_UP:
                    self._down.discard(vk)
                else:
                    if vk not in self._down:
                        self._down.add(vk)
                        self._dbg(f"vk={vk:#04x} down_set={sorted(self._down)}")
                        self._check(vk)
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

    def _check(self, vk):
        spec = self.HOTKEYS.get(vk)
        if not spec:
            return
        name, need_down, need_up = spec
        if need_down.issubset(self._down) and not (need_up & self._down):
            import time as _time
            now = _time.time()
            if now - getattr(self, "_last_fire", 0) < 0.35:  # 消抖: 按住/抖动只触发一次
                self._dbg(f"debounce skip {name}")
                return
            self._last_fire = now
            self._dbg(f"FIRE {name}")
            self.on_key(self.NAME_TO_ID[name])


# ---------------------------------------------------------------- 主程序
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def selftest(weapons):
    """命令行验证弹道计算，不弹窗"""
    print("== 自检：弹道计算 ==")
    for name in ("Gewehr 98 Sniper", "SMLE MKIII Marksman", "Martini-Henry Infantry"):
        w = weapons[name]
        for d in (100, 200, 300, 400):
            ang = holdover_angle(d, 75, w["v0"], w["drag"], w["g"])
            px = angle_to_pixels(ang, 90 / 8, 1080)
            print(f"{name:26s} {d:3d}m  偏角={ang*1000:7.2f} mrad  "
                  f"@1080p 8x = {px:7.1f} px")
        print(f"  最大射程(ttl) = {max_range(w['v0'], w['drag'], w['ttl']):.0f} m")

    # 实测数据复现: 用户测试时程序版本不同, 刻度仰角按当时版本计算:
    #   驱逐舰(10-20上午): 旧近似公式 drop/dist + 占位参数(300/0.0008/12)
    #   野战炮(10-20下午): 精确解算 + 贴吧旧参数(121.3/0.0012/7)
    CASES = (
        ("驱逐舰 QF4英寸速射炮 (实测校准)", (300.0, 0.0008, 12.0), "approx",
         [(0, 170), (200, 247), (400, 350), (500, 406), (700, 502), (1000, 605)]),
        ("野战炮 FK96 (实测校准)", (121.3, 0.0012, 7.0), "exact",
         [(150, 131), (200, 179), (225, 199), (250, 206), (250, 222),
          (275, 239), (350, 287)]),
    )
    for wname, (ov0, ok, og), mode, table in CASES:
        if wname not in weapons:
            continue
        w = weapons[wname]
        h = w.get("h", 0.0)
        print(f"\n== {wname} 实测复现 (h={h}m, 测试时角度按{mode}计算) ==")
        for mark, actual in table:
            if mark == 0:
                old_ang = 0.0
            elif mode == "approx":
                old_ang = drop_at(mark, ov0, ok, og) / mark
            else:
                old_ang = elev_for_range(mark, ov0, ok, og, 0.0)
            land = _landing_x(old_ang, w["v0"], w["drag"], w["g"], h)
            print(f"  旧刻度{mark:>4}m -> 复现落点 {land:6.1f}m (实测 {actual}m, "
                  f"{100*(land-actual)/actual:+.1f}%)")
        # 新标尺自洽性: 各距离仰角解算后落点应回到原距离
        for d in range(100, 700, 100):
            ang = holdover_angle(d, 0, w["v0"], w["drag"], w["g"], h, w["ttl"])
            if ang is None:
                print(f"  新刻度{d:>4}m -> 超出射程")
                continue
            land = _landing_x(ang, w["v0"], w["drag"], w["g"], h)
            print(f"  新刻度{d:>4}m -> 落点 {land:6.1f}m ({100*(land-d)/d:+.2f}%)")


def main():
    weapons = load_json(WEAPONS_FILE)
    if not weapons:
        print(f"未找到武器数据 {WEAPONS_FILE}")
        sys.exit(1)

    if "--selftest" in sys.argv:
        selftest(weapons)
        return

    if sys.platform == "win32":
        # 高 DPI 下保证 1 画布单位 = 1 物理像素
        ctypes.windll.user32.SetProcessDPIAware()

    settings = dict(DEFAULTS)
    settings.update(load_json(SETTINGS_FILE, {}))
    if settings["weapon"] not in weapons:
        settings["weapon"] = sorted(weapons.keys())[0]

    root = tk.Tk()
    root.withdraw()

    overlay = Overlay(root, settings, weapons)
    holder = {}

    def refresh():
        overlay.redraw()
        save_settings(settings)

    panel = Panel(root, settings, weapons, overlay, refresh)
    holder["panel"] = panel

    def on_hotkey(hid):
        if hid == 1:
            panel.cycle_weapon(-1)
        elif hid == 2:
            panel.cycle_weapon(+1)
        elif hid == 3:
            panel.cycle_zero()
        elif hid == 4:
            panel.cycle_zoom()
        elif hid == 5:
            panel.toggle_visible()

    Hotkeys(root, on_hotkey)
    refresh()
    panel.win.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
