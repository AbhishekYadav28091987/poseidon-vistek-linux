#!/usr/bin/env python3
"""
vistek_widget.py — desktop widget for the COUGAR Poseidon Vistek cooler.

A small always-on-top panel that mirrors the cooler's LCD: CPU temperature,
load, clock and power, plus BOTH fan speeds with animated fans that spin at a
rate proportional to their real RPM, and the pump speed.

Live data comes from the vistek daemon's status file (/run/vistek/status.json).
If that's missing it falls back to reading sensors directly (CPU power needs the
daemon/root, so it may show 0 in fallback mode).

Controls: drag to move · right-click for menu (always-on-top, mini mode, quit).
"""
import os, sys, json, math, time
import tkinter as tk
import tkinter.font as tkfont

STATUS_PATH = os.environ.get("VISTEK_STATUS", "/run/vistek/status.json")

# colours
BG     = "#0b0e14"
CARD   = "#121723"
EDGE   = "#1f2738"
ACCENT = "#38bdf8"
MUTED  = "#7c8aa5"
TEXT   = "#e6edf6"

# The two live fan channels and their roles, determined by a stress test:
#   fan1  -> pump (higher RPM, ramps hard with temp, reaches ~2600)
#   fan16 -> CPU/radiator fan (lower RPM ~1300-1600)
FAN_A = int(os.environ.get("VISTEK_FAN_A", "1"))     # left gauge
FAN_B = int(os.environ.get("VISTEK_FAN_B", "16"))    # right gauge
LABEL_A = os.environ.get("VISTEK_LABEL_A", "PUMP")
LABEL_B = os.environ.get("VISTEK_LABEL_B", "CPU FAN")


# ---------------------------------------------------------------- data source
def _import_vistek():
    for p in ("/usr/lib/vistek", "/usr/local/bin", "/usr/bin",
              os.path.dirname(os.path.abspath(__file__))):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import importlib, importlib.util
        # vistek.py installed without .py extension? try both
        for name in ("vistek", "vistek_driver"):
            if importlib.util.find_spec(name):
                return importlib.import_module(name)
    except Exception:
        pass
    return None

_VK = _import_vistek()

def read_status():
    """Return the current readings dict, from status file or direct sensors."""
    try:
        with open(STATUS_PATH) as f:
            d = json.load(f)
        if time.time() - d.get("ts", 0) < 10:   # fresh
            return d, "service"
    except (OSError, ValueError):
        pass
    if _VK:
        try:
            d = _VK.read_all()
            return d, "direct"
        except Exception:
            pass
    return None, "none"


def temp_color(t):
    if t is None:        return MUTED
    if t < 50:           return "#34d399"   # green
    if t < 70:           return "#fbbf24"   # amber
    if t < 85:           return "#fb923c"   # orange
    return "#f87171"                        # red


# --------------------------------------------------------------------- widget
class FanGauge:
    """A small animated fan drawn on the shared canvas."""
    def __init__(self, canvas, cx, cy, r, blades=7):
        self.c = canvas; self.cx = cx; self.cy = cy; self.r = r
        self.blades = blades
        self.angle = 0.0
        self.rpm = 0
        self.items = []

    def set_rpm(self, rpm):
        self.rpm = max(0, int(rpm))

    def step(self, dt):
        # visible spin speed proportional to RPM, capped so it stays smooth
        deg_per_sec = min(self.rpm * 0.30, 900)     # 1200rpm -> 1 rev/s
        self.angle = (self.angle + deg_per_sec * dt) % 360
        self.draw()

    def draw(self):
        c, cx, cy, r = self.c, self.cx, self.cy, self.r
        for it in self.items:
            c.delete(it)
        self.items = []
        # outer ring
        self.items.append(c.create_oval(cx-r, cy-r, cx+r, cy+r,
                                         outline=EDGE, width=2))
        on = self.rpm > 0
        blade_col = ACCENT if on else MUTED
        for i in range(self.blades):
            a = math.radians(self.angle + i * (360 / self.blades))
            # a curved blade approximated by a triangle fan from hub to rim
            pts = []
            for (rad, off) in ((0.18, 0), (0.95, 18), (0.55, 0), (0.95, -18)):
                ang = a + math.radians(off)
                pts += [cx + math.cos(ang) * r * rad, cy + math.sin(ang) * r * rad]
            self.items.append(c.create_polygon(pts, fill=blade_col, outline=""))
        # hub
        hr = r * 0.22
        self.items.append(c.create_oval(cx-hr, cy-hr, cx+hr, cy+hr,
                                         fill=CARD, outline=blade_col, width=2))


class Widget:
    def __init__(self, root):
        self.root = root
        self.mini = False
        root.title("Poseidon Vistek")
        root.configure(bg=BG)
        root.overrideredirect(True)          # frameless
        root.attributes("-topmost", True)
        try: root.attributes("-type", "utility")
        except tk.TclError: pass
        self.W, self.H = 360, 320
        self._place()
        self.canvas = tk.Canvas(root, width=self.W, height=self.H,
                                bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_static()
        self.fanA = FanGauge(self.canvas, 96, 232, 40)
        self.fanB = FanGauge(self.canvas, 264, 232, 40)

        # interactions
        for seq in ("<ButtonPress-1>", "<B1-Motion>"):
            root.bind(seq, self._drag)
        self._menu()
        root.bind("<Button-3>", lambda e: self.popup.tk_popup(e.x_root, e.y_root))
        root.bind("<Escape>", lambda e: root.destroy())

        self._data = (None, "none")
        self._last = time.monotonic()
        self._poll()          # data poll (0.5s)
        self._animate()       # fan animation (~30 fps)

    # ---- layout helpers
    def _place(self):
        sw = self.root.winfo_screenwidth()
        x = sw - self.W - 24; y = 48
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2,
               x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _build_static(self):
        c = self.canvas
        self.f_brand = tkfont.Font(family="DejaVu Sans", size=9, weight="bold")
        self.f_temp  = tkfont.Font(family="DejaVu Sans", size=64, weight="bold")
        self.f_unit  = tkfont.Font(family="DejaVu Sans", size=18, weight="bold")
        self.f_lbl   = tkfont.Font(family="DejaVu Sans", size=8)
        self.f_val   = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")
        self.f_rpm   = tkfont.Font(family="DejaVu Sans", size=12, weight="bold")

        self._round_rect(6, 6, self.W-6, self.H-6, 22, fill=CARD, outline=EDGE)
        c.create_text(self.W/2, 24, text="POSEIDON  VISTEK", fill=ACCENT,
                      font=self.f_brand)
        # big temperature
        self.t_temp = c.create_text(self.W/2-10, 92, text="--", fill=TEXT,
                                    font=self.f_temp)
        self.t_unit = c.create_text(self.W/2+86, 70, text="°C", fill=MUTED,
                                    font=self.f_unit)
        c.create_text(self.W/2, 138, text="CPU TEMPERATURE", fill=MUTED,
                      font=self.f_lbl)
        # stat row: load / clock / power
        self.stat = {}
        cols = [("LOAD", 70), ("CLOCK", 180), ("POWER", 290)]
        for name, x in cols:
            c.create_text(x, 166, text=name, fill=MUTED, font=self.f_lbl)
            self.stat[name] = c.create_text(x, 184, text="--", fill=TEXT,
                                            font=self.f_val)
        c.create_line(24, 200, self.W-24, 200, fill=EDGE)
        # fan labels / values (left = pump, right = cpu fan)
        c.create_text(96, 284, text=LABEL_A, fill=MUTED, font=self.f_lbl)
        c.create_text(264, 284, text=LABEL_B, fill=MUTED, font=self.f_lbl)
        self.t_fanA = c.create_text(96, 300, text="-- rpm", fill=TEXT, font=self.f_rpm)
        self.t_fanB = c.create_text(264, 300, text="-- rpm", fill=TEXT, font=self.f_rpm)
        self.t_src = c.create_text(self.W-16, self.H-14, text="", fill=MUTED,
                                   font=self.f_lbl, anchor="e")

    def _menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT,
                    activebackground=ACCENT, activeforeground=BG)
        self._ontop = tk.BooleanVar(value=True)
        m.add_checkbutton(label="Always on top", variable=self._ontop,
                          command=lambda: self.root.attributes("-topmost", self._ontop.get()))
        m.add_command(label="Reset position", command=self._place)
        m.add_separator()
        m.add_command(label="Quit", command=self.root.destroy)
        self.popup = m

    def _drag(self, e):
        if e.type == tk.EventType.ButtonPress:
            self._dx, self._dy = e.x, e.y
        else:
            self.root.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")

    # ---- update loops
    def _poll(self):
        self._data = read_status()
        d, src = self._data
        c = self.canvas
        if d:
            t = d.get("cpu_temp")
            c.itemconfig(self.t_temp, text=f"{t:.0f}" if t is not None else "--",
                         fill=temp_color(t))
            c.itemconfig(self.stat["LOAD"],  text=f"{d.get('cpu_load',0):.0f}%")
            clk = d.get("cpu_clock_mhz", 0) or 0
            c.itemconfig(self.stat["CLOCK"], text=f"{clk/1000:.2f}GHz")
            c.itemconfig(self.stat["POWER"], text=f"{d.get('cpu_watt',0):.0f}W")
            fans = d.get("fans", {})
            ra = int(fans.get(str(FAN_A), 0)); rb = int(fans.get(str(FAN_B), 0))
            c.itemconfig(self.t_fanA, text=f"{ra} rpm")
            c.itemconfig(self.t_fanB, text=f"{rb} rpm")
            self.fanA.set_rpm(ra); self.fanB.set_rpm(rb)
            c.itemconfig(self.t_src, text="● live" if src == "service" else "○ direct")
            c.itemconfig(self.t_src, fill="#34d399" if src == "service" else MUTED)
        else:
            c.itemconfig(self.t_temp, text="--", fill=MUTED)
            c.itemconfig(self.t_src, text="✕ no data — is vistek-display running?",
                         fill="#f87171")
        self.root.after(500, self._poll)

    def _animate(self):
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        self.fanA.step(dt)
        self.fanB.step(dt)
        self.root.after(33, self._animate)


def main():
    root = tk.Tk()
    Widget(root)
    root.mainloop()


if __name__ == "__main__":
    main()
