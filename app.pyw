"""Govee lights - desktop window for the Bluetooth bulbs. No server: while the window is open it keeps
the bulbs connected itself, so every change is instant; closing the window lets go of them.

Start: double-click "Govee lights.cmd" (or run: govee app).
"""
from __future__ import annotations

import ctypes
import math
import os
import queue
import sys
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import colorchooser, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lights import Lights, LightsError  # noqa: E402

SWATCHES = ["#ff0000", "#ff7f00", "#ffd000", "#00ff00", "#00ffff", "#0000ff", "#8000ff", "#ff00ff", "#ff1493"]
LIGHT = dict(bg="#f3f2f0", card="#ffffff", text="#1d1b19", muted="#6f6963", line="#dcd8d3", accent="#c2410c",
             accent_text="#ffffff", bad="#b91c1c", track="#d9d5d0", hover="#ece9e5", ok="#15803d")
DARK = dict(bg="#151413", card="#211f1d", text="#f3f1ee", muted="#a39d96", line="#3a3633", accent="#f97316",
            accent_text="#1d1b19", bad="#f87171", track="#46413d", hover="#2c2926", ok="#22c55e")
NEW_GROUP, NO_GROUP = "new group…", "no group"
APP_ID = "Govee.Lights"  # Windows AppUserModelID, also written into the shortcuts by make_shortcut.ps1
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "govee.ico")


def log_error(text: str) -> str:
    """pythonw has no console, so errors go to app_error.log next to this file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_error.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n\n")
    except OSError:
        pass
    return path


def windows_dark_mode() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def kelvin_color(k: float) -> str:
    """Approximate colour of a white temperature (for the dots)."""
    t = k / 100
    if t <= 66:
        r, g = 255, 99.47 * math.log(t) - 161.12
        b = 0 if t <= 19 else 138.52 * math.log(t - 10) - 305.04
    else:
        r, g, b = 329.7 * (t - 60) ** -0.1332, 288.12 * (t - 60) ** -0.0755, 255
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in (r, g, b))


def aggregate(bulbs: list[dict]) -> dict:
    """What a group card shows: average brightness and the look of the first bulb that is on."""
    on = [b for b in bulbs if b.get("online") and b.get("on")]
    first = on[0] if on else {}
    return {"online": any(b.get("online") for b in bulbs), "on": bool(on), "n_on": len(on), "n": len(bulbs),
            "n_online": sum(bool(b.get("online")) for b in bulbs),
            "n_offline": sum(bool(b.get("offline")) for b in bulbs),
            "brightness": round(sum(b["brightness"] for b in on) / len(on)) if on else None,
            "mode": first.get("mode"), "kelvin": first.get("kelvin"), "color": first.get("color"),
            "effect": first.get("effect")}


class Switch(tk.Canvas):
    """A small on/off switch (green = on)."""

    def __init__(self, parent, app: "App", command):
        px, C = app.px, app.C
        self.w, self.h, self.pad = px(42), px(24), px(3)
        super().__init__(parent, width=self.w, height=self.h, bg=C["card"], highlightthickness=0, cursor="hand2")
        self.app, self.command, self.value = app, command, False
        h, w = self.h, self.w
        self.parts = [self.create_oval(0, 0, h, h, outline="", fill=C["track"]),
                      self.create_oval(w - h, 0, w, h, outline="", fill=C["track"]),
                      self.create_rectangle(h // 2, 0, w - h // 2, h, outline="", fill=C["track"])]
        self.knob = self.create_oval(0, 0, 0, 0, outline="", fill="#ffffff")
        self.bind("<Button-1>", lambda e: self.toggle())
        self.set(False)

    def toggle(self) -> None:
        self.set(not self.value)  # show it at once; the bulb's answer confirms it
        self.command(self.value)

    def set(self, on: bool) -> None:
        self.value = bool(on)
        for p in self.parts:
            self.itemconfigure(p, fill=self.app.C["ok"] if on else self.app.C["track"])
        x = self.w - self.h if on else 0
        self.coords(self.knob, x + self.pad, self.pad, x + self.h - self.pad, self.h - self.pad)


class MiniSpinner(tk.Canvas):
    """A small spinning ring, shown on a group card while some of its bulbs are still connecting."""

    def __init__(self, parent, app: "App"):
        size, w = app.px(18), app.px(3)
        super().__init__(parent, width=size, height=size, bg=app.C["card"], highlightthickness=0)
        self.app, self.angle, self.running = app, 0, False
        self.create_oval(w, w, size - w, size - w, outline=app.C["track"], width=w)
        self.arc = self.create_arc(w, w, size - w, size - w, start=0, extent=100, style="arc",
                                   outline=app.C["accent"], width=w)

    def start(self) -> None:
        if not self.running:
            self.running = True
            self._spin()

    def stop(self) -> None:
        self.running = False

    def _spin(self) -> None:
        if not self.running:
            return
        self.angle = (self.angle - 15) % 360
        try:
            self.itemconfigure(self.arc, start=self.angle)
        except tk.TclError:
            return
        self.app.root.after(40, self._spin)


class Loader:
    """Covers a bulb card's controls (not its name) with a spinner while the bulb is not connected."""

    def __init__(self, card: "Card", app: "App"):
        C, px = app.C, app.px
        self.card, self.app, self.angle, self.timer, self.visible = card, app, 0, None, False
        self.frame = tk.Frame(card.frame, bg=C["card"])
        size = px(44)
        self.canvas = tk.Canvas(self.frame, width=size, height=size, bg=C["card"], highlightthickness=0)
        w = px(5)
        self.canvas.create_oval(w, w, size - w, size - w, outline=C["track"], width=w)
        self.arc = self.canvas.create_arc(w, w, size - w, size - w, start=0, extent=100, style="arc",
                                          outline=C["accent"], width=w)
        self.label = tk.Label(self.frame, bg=C["card"], fg=C["muted"], font=("Segoe UI", 10))
        self.title = tk.Label(self.frame, bg=C["card"], fg=C["text"], font=("Segoe UI Semibold", 11))
        self.title.place(x=px(14), y=px(14))
        self.canvas.place(relx=0.5, rely=0.36, anchor="center")
        self.label.place(relx=0.5, rely=0.36, y=size // 2 + px(18), anchor="center")
        self.hint = tk.Label(self.frame, bg=C["card"], fg=C["muted"], font=("Segoe UI", 9))
        self.button = ttk.Button(self.frame, text="Try now", width=10)
        self.spinning = True

    def show(self, text: str, bad: bool, title: str = "", spin: bool = True, hint: str = "", action=None) -> None:
        """spin=False: a still, dimmed ring (the bulb seems switched off), with an optional "Try now"."""
        C, px = self.app.C, self.app.px
        self.title.configure(text=title)
        self.label.configure(text=text, fg=C["bad"] if bad else C["muted"])
        self.canvas.itemconfigure(self.arc, outline=(C["bad"] if bad else C["accent"]) if spin else C["track"])
        if hint:
            self.hint.configure(text=hint)
            self.hint.place(relx=0.5, rely=0.36, y=px(22) + px(40), anchor="center")
        else:
            self.hint.place_forget()
        if action:
            self.button.configure(command=action)
            self.button.place(relx=0.5, rely=0.36, y=px(22) + px(78), anchor="center")
        else:
            self.button.place_forget()
        self._place()
        self.spinning = spin
        if not self.visible:
            self.visible = True
            self._spin()

    def _place(self) -> None:  # the whole card; the title label repeats the name
        self.frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.frame.lift()

    def hide(self) -> None:
        self.visible = False
        self.frame.place_forget()
        if self.timer:
            self.app.root.after_cancel(self.timer)
            self.timer = None

    def _spin(self) -> None:
        if not self.visible:
            return
        if self.spinning:
            self.angle = (self.angle - 12) % 360
        try:
            self.canvas.itemconfigure(self.arc, start=self.angle)
        except tk.TclError:  # card was rebuilt
            return
        self.timer = self.app.root.after(40, self._spin)


class Card:
    """One group or one bulb: power, brightness, white temperature and colour."""

    def __init__(self, app: "App", parent, target: str, title: str, bulb: bool):
        self.app, self.target, self.bulb = app, target, bulb
        C, px = app.C, app.px
        self.setting = False  # True while the program (not the user) moves the controls
        self.hold_until = 0.0  # the user just moved a slider here: keep showing their value
        self.effect_hold_until = 0.0  # ... or picked an effect, which takes a few seconds to confirm
        self.release_timer = None
        self.frame = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["line"],
                              padx=px(14), pady=px(12))
        top = tk.Frame(self.frame, bg=C["card"])
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=px(18), height=px(18), bg=C["card"], highlightthickness=0)
        self.dot_item = self.dot.create_oval(px(1), px(1), px(17), px(17), fill=C["track"], outline=C["line"])
        self.dot.pack(side="left", padx=(0, px(8)))
        if bulb:
            self.name = ttk.Entry(top, style="Name.TEntry", font=("Segoe UI Semibold", 11), width=14)
            self.name.insert(0, title)
            self.name.pack(side="left", fill="x", expand=True)
            self.name.bind("<Return>", lambda e: self.app.root.focus_set())
            self.name.bind("<Escape>", lambda e: self._reset_name())
            self.name.bind("<FocusOut>", lambda e: self._renamed())
            self.switch = Switch(top, app, lambda on: self.app.command(self.target, {"on": on}))
            self.switch.pack(side="right")
        else:
            box = self.title_box = tk.Frame(top, bg=C["card"])
            self.title_text = title
            box.pack(side="left", fill="x", expand=True)
            ttk.Label(box, text=title, style="Title.Card.TLabel").pack(anchor="w")
            self.count = ttk.Label(box, style="Muted.Card.TLabel")
            self.count.pack(anchor="w")
            ttk.Button(top, text="Off", width=5, command=lambda: self.app.command(self.target, {"on": False})).pack(side="right")
            ttk.Button(top, text="On", width=5, command=lambda: self.app.command(self.target, {"on": True})).pack(side="right", padx=(0, px(6)))
            self.mini = MiniSpinner(top, app)
            self.mini_shown = False
        self.status = ttk.Label(self.frame, style="Bad.Card.TLabel")
        self.top = top
        self.loader = Loader(self, app)
        lo, hi = app.kelvin
        self.bright, self.bright_val = self._slider("Brightness", 1, 100, "brightness")
        self.white, self.white_val = self._slider("White", lo, hi, "kelvin")
        row = tk.Frame(self.frame, bg=C["card"])
        row.pack(fill="x", pady=(px(8), 0))
        ttk.Label(row, text="Colour", style="Muted.Card.TLabel", width=10).pack(side="left")
        step = px(24)
        sw = self.swatches = tk.Canvas(row, height=px(24), width=(len(SWATCHES) + 1) * step, bg=C["card"],
                                       highlightthickness=0, cursor="hand2")
        for i, col in enumerate(SWATCHES):
            x = px(2) + i * step
            item = sw.create_oval(x, px(2), x + px(20), px(22), fill=col, outline=C["line"])
            sw.tag_bind(item, "<Button-1>", lambda e, c=col: self.app.command(self.target, {"color": c}))
        x = px(2) + len(SWATCHES) * step  # last one: "+" opens the colour picker
        more = [sw.create_oval(x, px(2), x + px(20), px(22), fill=C["card"], outline=C["muted"]),
                sw.create_text(x + px(10), px(12), text="+", fill=C["text"], font=("Segoe UI Semibold", 11))]
        for item in more:
            sw.tag_bind(item, "<Button-1>", lambda e: self._pick_color())
        sw.pack(side="left")
        row = tk.Frame(self.frame, bg=C["card"])
        row.pack(fill="x", pady=(px(8), 0))
        ttk.Label(row, text="Effect", style="Muted.Card.TLabel", width=10).pack(side="left")
        self.effect = ttk.Combobox(row, state="readonly", values=app.effect_values, height=24, style="Card.TCombobox")
        self.effect.pack(side="left", fill="x", expand=True)
        self.effect.bind("<<ComboboxSelected>>", lambda e: self._effect_picked())
        if bulb:
            foot = tk.Frame(self.frame, bg=C["card"])
            foot.pack(fill="x", pady=(px(10), 0))
            self.group = ttk.Combobox(foot, state="readonly", width=11, style="Card.TCombobox")
            self.group.bind("<<ComboboxSelected>>", lambda e: self._group_picked())
            self.group.pack(side="left")
            ttk.Button(foot, text="Identify", command=lambda: self.app.identify(self.target)).pack(side="left", padx=px(6))
            self.addr = ttk.Label(foot, style="Muted.Card.TLabel")
            self.addr.pack(side="right")

    def _slider(self, label: str, lo: int, hi: int, kind: str):
        C, px = self.app.C, self.app.px
        row = tk.Frame(self.frame, bg=C["card"])
        row.pack(fill="x", pady=(px(8), 0))
        ttk.Label(row, text=label, style="Muted.Card.TLabel", width=10).pack(side="left")
        val = ttk.Label(row, style="Card.TLabel", width=7, anchor="e")
        val.pack(side="right")
        scale = ttk.Scale(row, from_=lo, to=hi, orient="horizontal", style="Card.Horizontal.TScale",
                          command=lambda v: self._moved(kind, v))
        scale.pack(side="left", fill="x", expand=True, padx=(px(4), px(6)))
        scale.bind("<ButtonRelease-1>", lambda e: self._released(kind))
        return scale, val

    @staticmethod
    def _value(kind: str, v) -> int:
        v = float(v)
        return int(round(v / 50) * 50) if kind == "kelvin" else int(round(v))

    def _moved(self, kind: str, v) -> None:
        if self.setting:
            return
        value = self._value(kind, v)
        (self.bright_val if kind == "brightness" else self.white_val).configure(
            text=f"{value}%" if kind == "brightness" else f"{value} K")
        self.hold_until = time.monotonic() + 3.0
        self.app.live(self.target, {kind: value})

    def _released(self, kind: str) -> None:
        scale = self.bright if kind == "brightness" else self.white
        self.hold_until = time.monotonic() + 3.0
        if self.release_timer:  # debounce: one confirmed command once the clicking has stopped
            self.app.root.after_cancel(self.release_timer)

        def send():
            self.release_timer = None
            self.hold_until = time.monotonic() + 3.0  # the confirmation may still carry an older value
            self.app.command(self.target, {kind: self._value(kind, scale.get())})

        self.release_timer = self.app.root.after(250, send)

    def _pick_color(self) -> None:
        v = self.view or {}
        start = v.get("color") if v.get("mode") == "color" and v.get("color") else "#ff7f00"
        rgb, hexa = colorchooser.askcolor(color=start, title="Pick a colour", parent=self.app.root)
        if hexa:
            self.app.command(self.target, {"color": hexa})

    def _effect_picked(self) -> None:
        name = self.effect.get().split(" · ", 1)[-1]
        self.effect_hold_until = time.monotonic() + 8.0
        self.app.root.focus_set()  # leave the list, so the window keeps refreshing
        if name:
            self.app.command(self.target, {"effect": name})

    def _reset_name(self) -> None:
        self.name.delete(0, "end")
        self.name.insert(0, self.view["name"] if self.view else "")
        self.app.root.focus_set()

    def _renamed(self) -> None:
        new = self.name.get().strip()
        if self.view and new and new != self.view["name"]:
            self.app.rename(self.target, new)
        elif self.view:
            self._reset_name()

    def _group_picked(self) -> None:
        g = self.group.get()
        if g == NEW_GROUP:
            g = simpledialog.askstring("New group", "Name of the new group:", parent=self.app.root) or ""
            if not g.strip():
                self.show(self.view)
                return
        self.app.set_group(self.target, "" if g == NO_GROUP else g)

    view: dict | None = None

    def show(self, v: dict) -> None:
        """Show a bulb view (or a group aggregate) without sending anything."""
        C = self.app.C
        self.view = v
        self.setting = True
        try:
            if not v.get("online"):
                fill = C["card"]
            elif not v.get("on"):
                fill = C["track"]
            else:
                fill = (kelvin_color(v["kelvin"]) if v.get("mode") == "white" and v.get("kelvin")
                        else C["accent"] if v.get("mode") == "scene" else (v.get("color") or C["track"]))
            self.dot.itemconfigure(self.dot_item, fill=fill)
            if self.bulb:
                self.switch.set(bool(v.get("on")))
                if self.app.focused() is not self.name and self.name.get() != v["name"]:
                    self.name.delete(0, "end")
                    self.name.insert(0, v["name"])
                self.group.configure(values=[*self.app.S["groups"], NO_GROUP, NEW_GROUP])
                self.group.set(v["group"] or NO_GROUP)
                self.addr.configure(text=v["address"])
                if v.get("online"):
                    self.loader.hide()
                elif v.get("offline"):
                    self.loader.show("switched off or out of range", bad=False, title=v["name"], spin=False,
                                     hint="it reconnects by itself when it is back",
                                     action=lambda: self.app.reconnect(self.target, v["name"]))
                else:
                    self.loader.show("connecting…" if v.get("connecting") else "not reachable - retrying",
                                     bad=not v.get("connecting"), title=v["name"])
            else:
                n, up, off = v["n"], v["n_online"], v.get("n_offline", 0)
                self.count.configure(text=f"{n} bulb{'s' if n != 1 else ''} · {v['n_on']} on"
                                          + (f" · {up}/{n} connected" if up < n - off else "")
                                          + (f" · {off} off" if off else ""))
                if up == 0 and off == n:
                    self.loader.show("switched off or out of range", bad=False, title=self.title_text, spin=False,
                                     hint="they reconnect by themselves when they are back",
                                     action=lambda: self.app.reconnect(self.target, self.title_text))
                elif up == 0:
                    self.loader.show(f"connecting… 0/{n}", bad=False, title=self.title_text)
                else:
                    self.loader.hide()
                waiting = 0 < up < n - off  # some bulbs are still being connected (not the switched-off ones)
                if waiting and not self.mini_shown:
                    self.mini.pack(side="left", padx=(0, self.app.px(8)), before=self.title_box)
                    self.mini.start()
                    self.mini_shown = True
                elif not waiting and self.mini_shown:
                    self.mini.pack_forget()
                    self.mini.stop()
                    self.mini_shown = False
            if (self.app.focused() is not self.effect and not self.app.list_open()
                    and time.monotonic() >= self.effect_hold_until):
                self.effect.set(self.app.effect_label.get(v.get("effect") or "", "")
                                if v.get("mode") == "scene" and v.get("on") else "")
            if not self.app.dragging() and time.monotonic() >= self.hold_until:
                if v.get("brightness"):
                    self.bright.set(v["brightness"])
                self.bright_val.configure(text=f"{v['brightness']}%" if v.get("brightness") else "–")
                if v.get("mode") == "white" and v.get("kelvin"):
                    self.white.set(v["kelvin"])
                    self.white_val.configure(text=f"{v['kelvin']} K")
                else:  # colour mode: park the white slider in the middle
                    self.white.set(sum(self.app.kelvin) / 2)
                    self.white_val.configure(text="–")
        finally:
            self.setting = False


class App:
    POLL_MS = 2000

    def __init__(self, root: tk.Tk, lights: Lights):
        self.root, self.lights = root, lights
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.inbox: queue.Queue = queue.Queue()
        self.S: dict | None = None
        self.shape = None
        self.cards: dict[str, Card] = {}
        self.busy = 0
        self.gen = 0
        self.quiet_until = 0.0
        self.polling = False
        self.live_timers: dict = {}
        self.live_pending: dict = {}
        self.closing = False
        self._msg_timer = None
        scale = root.winfo_fpixels("1i") / 96
        self.px = lambda n: max(1, round(n * scale))
        self.kelvin = (2700, 6500)
        cats = lights.effects()["categories"]
        self.effect_label = {n: f"{c} · {n}" for c, names in cats.items() for n in names}
        self.effect_values = list(self.effect_label.values())
        self._theme()
        self._layout()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._callback_error
        root.after(40, self._pump)
        self.refresh(force=True)
        root.after(self.POLL_MS, self._tick)

    # ---- look ----------------------------------------------------------------------
    def _theme(self) -> None:
        dark = windows_dark_mode()
        C = self.C = DARK if dark else LIGHT
        r = self.root
        r.configure(bg=C["bg"])
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure(".", background=C["bg"], foreground=C["text"], font=("Segoe UI", 10), bordercolor=C["line"],
                    lightcolor=C["line"], darkcolor=C["line"], troughcolor=C["track"], fieldbackground=C["card"],
                    insertcolor=C["text"], focuscolor=C["accent"], selectbackground=C["accent"],
                    selectforeground=C["accent_text"], arrowcolor=C["muted"])
        s.configure("TButton", background=C["card"], padding=(self.px(10), self.px(4)))
        s.map("TButton", background=[("pressed", C["line"]), ("active", C["hover"])])
        s.configure("Accent.TButton", background=C["accent"], foreground=C["accent_text"], bordercolor=C["accent"])
        s.map("Accent.TButton", background=[("active", C["accent"])])
        s.configure("Card.TLabel", background=C["card"])
        s.configure("Title.Card.TLabel", background=C["card"], font=("Segoe UI Semibold", 12))
        s.configure("Muted.Card.TLabel", background=C["card"], foreground=C["muted"], font=("Segoe UI", 9))
        s.configure("Bad.Card.TLabel", background=C["card"], foreground=C["bad"], font=("Segoe UI", 9))
        s.configure("Muted.TLabel", foreground=C["muted"])
        s.configure("Head.TLabel", font=("Segoe UI Semibold", 16))
        s.configure("Section.TLabel", foreground=C["muted"], font=("Segoe UI Semibold", 9))
        s.configure("Card.TCheckbutton", background=C["card"], indicatorbackground=C["card"],
                    indicatorforeground=C["accent"], font=("Segoe UI Semibold", 10))
        s.map("Card.TCheckbutton", background=[("active", C["card"])], indicatorbackground=[("selected", C["card"])])
        s.configure("Card.Horizontal.TScale", background=C["accent"], troughcolor=C["track"], bordercolor=C["card"],
                    lightcolor=C["accent"], darkcolor=C["accent"], gripcount=0, sliderlength=self.px(16))
        s.map("Card.Horizontal.TScale", background=[("active", C["text"])])
        s.configure("Name.TEntry", fieldbackground=C["card"], bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"])
        s.map("Name.TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])])
        s.configure("Card.TCombobox", fieldbackground=C["card"], background=C["card"])
        s.map("Card.TCombobox", fieldbackground=[("readonly", C["card"])], foreground=[("readonly", C["text"])])
        r.option_add("*TCombobox*Listbox.background", C["card"])
        r.option_add("*TCombobox*Listbox.foreground", C["text"])
        r.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        r.option_add("*TCombobox*Listbox.selectForeground", C["accent_text"])
        if dark:  # dark title bar (Windows 10 2004+ / 11)
            try:
                r.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(r.winfo_id())
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
            except Exception:  # noqa: BLE001 - purely cosmetic
                pass
        try:  # the rainbow bulb, also used by the desktop / Start menu shortcut
            r.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass

    def _layout(self) -> None:
        C, px, r = self.C, self.px, self.root
        self.banner = tk.Label(r, text="DEMO with simulated bulbs: this window does not control your real lights.",
                               bg="#7c2d12", fg="#ffffff", font=("Segoe UI Semibold", 10), pady=px(6))
        head = ttk.Frame(r, padding=(px(20), px(14), px(20), px(6)))
        head.pack(fill="x")
        ttk.Label(head, text="Govee lights", style="Head.TLabel").pack(side="left")
        self.summary = ttk.Label(head, style="Muted.TLabel")
        self.summary.pack(side="left", padx=px(14), pady=(px(6), 0))
        self.discover_btn = ttk.Button(head, text="Discover", command=self.discover)
        self.discover_btn.pack(side="right")
        ttk.Button(head, text="All off", command=lambda: self.command("all", {"on": False})).pack(side="right", padx=px(6))
        ttk.Button(head, text="All on", command=lambda: self.command("all", {"on": True})).pack(side="right")
        bar = ttk.Frame(r, padding=(px(20), px(4), px(20), px(10)))
        bar.pack(fill="x")
        ttk.Label(bar, text="SCENES", style="Section.TLabel").pack(side="left", padx=(0, px(10)))
        self.scene_box = ttk.Frame(bar)
        self.scene_box.pack(side="left")
        ttk.Button(bar, text="Save current", style="Accent.TButton", command=self.save_scene).pack(side="right")
        self.scene_name = ttk.Entry(bar, width=16)
        self.scene_name.pack(side="right", padx=px(6))
        self.scene_name.bind("<Return>", lambda e: self.save_scene())
        ttk.Label(bar, text="new scene:", style="Muted.TLabel").pack(side="right")
        self.message = ttk.Label(r, padding=(px(20), px(6)), style="Muted.TLabel")
        self.message.pack(side="bottom", fill="x")
        # scrollable body
        outer = ttk.Frame(r)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=(px(20), 0, px(20), px(20)))
        win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        r.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

    # ---- background work -------------------------------------------------------------
    def run(self, fn, done=None, fail=None, **kw) -> None:
        """Run a (blocking) Lights call off the UI thread; `done(result)` runs back on the UI thread."""
        fut = self.pool.submit(fn, **kw)
        fut.add_done_callback(lambda f: self.inbox.put((done, fail, f)))

    def _pump(self) -> None:
        while True:
            try:
                done, fail, fut = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - shown in the message line
                self.say(str(e) if isinstance(e, LightsError) else f"{type(e).__name__}: {e}", error=True)
                if fail:
                    fail(e)
                continue
            if done:
                try:
                    done(res)
                except Exception as e:  # noqa: BLE001
                    self.say(f"{type(e).__name__}: {e}", error=True)
        if not self.closing:
            self.root.after(40, self._pump)

    def say(self, text: str, error: bool = False) -> None:
        self.message.configure(text=text, foreground=self.C["bad"] if error else self.C["muted"])
        if self._msg_timer:
            self.root.after_cancel(self._msg_timer)
        self._msg_timer = self.root.after(8000 if error else 4000, lambda: self.message.configure(text=""))

    def focused(self):
        """The widget with keyboard focus, or None (also while a dropdown list is open)."""
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):  # focus is inside a combobox's popdown list
            return None

    def list_open(self) -> bool:
        """Is a dropdown list (effect / group picker) open right now?"""
        try:
            return "popdown" in str(self.root.tk.call("focus"))
        except tk.TclError:
            return False

    def dragging(self) -> bool:
        return time.monotonic() < self.quiet_until

    def _callback_error(self, exc, value, tb) -> None:
        import traceback
        path = log_error("".join(traceback.format_exception(exc, value, tb)))
        self.say(f"Error: {value} (details in {os.path.basename(path)})", error=True)

    # ---- state -------------------------------------------------------------------------
    def _tick(self) -> None:
        if self.closing:
            return
        focus = self.focused()
        if (not self.busy and not self.dragging() and not self.list_open()
                and not isinstance(focus, (ttk.Entry, ttk.Combobox))):
            self.refresh()
        self.root.after(self.POLL_MS, self._tick)

    def refresh(self, force: bool = False) -> None:
        if self.polling and not force:
            return
        self.polling = True
        gen = self.gen

        def done(s):
            self.polling = False
            if force or gen == self.gen:  # a command meanwhile already showed newer values
                self.apply(s)

        self.run(self.lights.state, done, lambda e: setattr(self, "polling", False))

    def apply(self, s: dict) -> None:
        self.S = s
        self.kelvin = tuple(s["kelvin"])
        shape = (tuple((b["id"], b["name"], b["group"]) for b in s["bulbs"]), tuple(s["groups"]),
                 tuple(s["scenes"]), self.kelvin, s.get("demo"))
        if shape != self.shape:
            self.shape = shape
            self._build()
        self._show()

    def merge(self, views: list[dict]) -> None:
        by_id = {v["id"]: v for v in views or []}
        if self.S:
            self.S["bulbs"] = [by_id.get(b["id"], b) for b in self.S["bulbs"]]
        self._show()

    def _build(self) -> None:
        S, px = self.S, self.px
        if S.get("demo"):
            self.root.title("Govee lights (DEMO)")
            self.banner.pack(side="top", fill="x", before=self.root.winfo_children()[1])
        for w in self.scene_box.winfo_children():
            w.destroy()
        for name in S["scenes"]:
            b = ttk.Button(self.scene_box, text=name, command=lambda n=name: self.apply_scene(n))
            b.pack(side="left", padx=(0, px(6)))
            b.bind("<Button-3>", lambda e, n=name: self._scene_menu(e, n))
        for w in self.body.winfo_children():
            w.destroy()
        self.cards = {}
        if not S["bulbs"]:
            ttk.Label(self.body, text="No bulbs known yet.\n\nSwitch the bulbs on at the wall, close the Govee app on "
                      "your phone, then click Discover.", style="Muted.TLabel", justify="left").grid(row=0, column=0, sticky="w")
            return
        for c in range(3):
            self.body.columnconfigure(c, weight=1, uniform="cards")
        row = 0

        def section(title):
            nonlocal row
            ttk.Label(self.body, text=title.upper(), style="Section.TLabel").grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(px(16) if row else px(4), px(6)))
            row += 1

        def place(cards):
            nonlocal row
            for i, card in enumerate(cards):
                card.frame.grid(row=row + i // 3, column=i % 3, sticky="nsew", padx=(0 if i % 3 == 0 else px(6), 0), pady=(0, px(8)))
            row += (len(cards) + 2) // 3

        section("Groups")
        groups = [("all", "All")] + [(g, g[:1].upper() + g[1:]) for g in S["groups"]
                                     if any(b["group"] == g for b in S["bulbs"])]
        place([self._card(t, title, False) for t, title in groups])
        for g in [*S["groups"], ""]:
            members = [b for b in S["bulbs"] if b["group"] == g]
            if members:
                section(g or "No group")
                place([self._card(b["id"], b["name"], True) for b in members])

    def _card(self, target: str, title: str, bulb: bool) -> Card:
        card = Card(self, self.body, target, title, bulb)
        self.cards[target if bulb else "group:" + target] = card
        return card

    def _show(self) -> None:
        S = self.S
        if not S:
            return
        online = sum(b["online"] for b in S["bulbs"])
        self.summary.configure(text=f"{online} of {len(S['bulbs'])} bulbs connected" if S["bulbs"] else "")
        for b in S["bulbs"]:
            if b["id"] in self.cards:
                self.cards[b["id"]].show(b)
        for key, card in self.cards.items():
            if key.startswith("group:"):
                g = key[6:]
                card.show(aggregate([b for b in S["bulbs"] if g == "all" or b["group"] == g]))

    # ---- actions -------------------------------------------------------------------------
    def live(self, target: str, change: dict) -> None:
        """While a slider is dragged: send at most every 150 ms, without waiting for confirmation."""
        self.quiet_until = time.monotonic() + 2.5
        key = (target, tuple(change))
        self.live_pending[key] = change
        if key not in self.live_timers:
            self.live_timers[key] = self.root.after(150, lambda: self._flush(key))

    def _flush(self, key) -> None:
        self.live_timers.pop(key, None)
        change = self.live_pending.pop(key, None)
        if change:
            self.run(self.lights.control, target=key[0], verify=False, **change)

    def command(self, target: str, change: dict) -> None:
        for key in [k for k in self.live_timers if k[0] == target]:
            self.root.after_cancel(self.live_timers.pop(key))
            self.live_pending.pop(key, None)
        self.busy += 1
        self.gen += 1

        def done(r):
            self.busy -= 1
            self.quiet_until = 0
            for card in self.cards.values():  # the bulbs have answered: show what they really do
                card.effect_hold_until = 0.0
            self.merge(r["bulbs"])
            if r["warnings"]:
                self.say(" · ".join(r["warnings"]), error=True)

        self.run(self.lights.control, done, lambda e: setattr(self, "busy", self.busy - 1),
                 target=target, verify=True, **change)

    def apply_scene(self, name: str) -> None:
        self.busy += 1
        self.gen += 1

        def done(r):
            self.busy -= 1
            self.merge(r["bulbs"])
            self.say(f"Scene {r['scene']}: " + " · ".join(r["warnings"]) if r["warnings"] else f"Scene {r['scene']} applied",
                     error=bool(r["warnings"]))

        self.run(self.lights.scene_apply, done, lambda e: setattr(self, "busy", self.busy - 1), name=name)

    def save_scene(self) -> None:
        name = self.scene_name.get().strip()
        if not name:
            self.scene_name.focus_set()
            return self.say("Type a name for the new scene first.", error=True)

        def done(r):
            self.scene_name.delete(0, "end")
            self.say(f"Saved scene {r['scene']} ({len(r['saved'])} bulbs)"
                     + (f" · not reachable: {', '.join(r['skipped'])}" if r["skipped"] else ""), error=bool(r["skipped"]))
            self.refresh(force=True)

        self.run(self.lights.scene_save, done, name=name)

    def _scene_menu(self, event, name: str) -> None:
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label=f"Apply {name}", command=lambda: self.apply_scene(name))
        m.add_command(label=f"Delete {name}", command=lambda: self.run(
            self.lights.scene_delete, lambda r: (self.say(f"Deleted scene {r['deleted']}"), self.refresh(force=True)), name=name))
        m.tk_popup(event.x_root, event.y_root)

    def rename(self, target: str, name: str) -> None:
        self.run(self.lights.rename, lambda r: self.refresh(force=True), lambda e: self.refresh(force=True),
                 bulb=target, name=name)

    def set_group(self, target: str, group: str) -> None:
        self.run(self.lights.set_group, lambda r: self.refresh(force=True), lambda e: self.refresh(force=True),
                 bulb=target, group=group)

    def reconnect(self, target: str, name: str) -> None:
        self.say(f"Looking for {name}… (up to ~25 s)")
        self.run(self.lights.reconnect, target=target)

    def identify(self, target: str) -> None:
        self.say("Flashing the bulb…")
        self.run(self.lights.blink, lambda r: self.say(f"Flashed {', '.join(r['bulbs'])}"), target=target)

    def discover(self) -> None:
        self.discover_btn.configure(state="disabled", text="Searching…")

        def done(r):
            self.discover_btn.configure(state="normal", text="Discover")
            self.apply(r)
            self.say(f"Found {r['found']} bulb(s)" + (f" · new: {', '.join(r['new'])}" if r["new"] else ""))

        self.run(self.lights.discover, done, lambda e: self.discover_btn.configure(state="normal", text="Discover"), seconds=8)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.say("Letting go of the bulbs…")
        fut = self.pool.submit(self.lights.close)
        deadline = time.monotonic() + 5

        def wait():
            if fut.done() or time.monotonic() > deadline:
                self.pool.shutdown(wait=False)
                self.root.destroy()
            else:
                self.root.after(100, wait)

        wait()


def main(lights: Lights | None = None) -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:  # noqa: BLE001
        pass
    try:  # own taskbar button with our icon, grouped with the pinned shortcut (same ID in the .lnk)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001
        pass
    if lights is None and bring_existing_window():
        return  # already open: it was brought to the front instead
    root = tk.Tk()
    root.withdraw()  # tag it for the taskbar before it is first shown
    root.title("Govee lights")
    try:
        tag_window_for_taskbar(root)
    except Exception:  # noqa: BLE001 - cosmetic; the app works without it
        import traceback
        log_error(traceback.format_exc())
    root.deiconify()
    try:
        lights = lights or Lights(live=True)
    except LightsError as e:
        from tkinter import messagebox
        messagebox.showerror("Govee lights", str(e))
        return
    app = App(root, lights)
    left, top, right, bottom = work_area_at_mouse()  # open on the screen you are looking at
    w, h = min(app.px(1120), right - left - 40), min(app.px(900), bottom - top - 40)
    root.geometry(f"{w}x{h}+{left + (right - left - w) // 2}+{top + (bottom - top - h) // 2}")
    root.minsize(app.px(760), app.px(480))
    root.mainloop()


def tag_window_for_taskbar(root: tk.Tk) -> None:
    """Tell the taskbar this window is "Govee lights", relaunched by Govee lights.exe with our icon.
    Without this, pinning the running window pins the Store Python itself (which then opens nothing)."""
    here = os.path.dirname(os.path.abspath(__file__))
    values = {5: APP_ID,  # System.AppUserModel.ID
              2: '"' + os.path.join(here, "Govee lights.exe") + '"',  # RelaunchCommand
              3: ICON + ",0",  # RelaunchIconResource
              4: "Govee lights"}  # RelaunchDisplayNameResource

    class Guid(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

    def guid(a, b, c, d):
        return Guid(a, b, c, (ctypes.c_ubyte * 8)(*d))

    class Key(ctypes.Structure):
        _fields_ = [("fmtid", Guid), ("pid", ctypes.c_ulong)]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("p", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    iid = guid(0x886D8EEB, 0x8CF2, 0x4446, (0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))  # IPropertyStore
    store = ctypes.c_void_p()
    if ctypes.windll.shell32.SHGetPropertyStoreForWindow(ctypes.c_void_p(hwnd), ctypes.byref(iid), ctypes.byref(store)):
        return
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(Key), ctypes.POINTER(PropVariant))(vtbl[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    keep = []
    for pid in (2, 3, 4, 5):  # the ID last, as the docs recommend
        text = ctypes.create_unicode_buffer(values[pid])
        keep.append(text)
        key = Key(guid(0x9F4C2855, 0x9F79, 0x4B39, (0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3)), pid)
        set_value(store, ctypes.byref(key), ctypes.byref(PropVariant(31, 0, 0, 0, ctypes.cast(text, ctypes.c_void_p))))
    commit(store)
    release(store)


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4), ("rcWork", ctypes.c_long * 4),
                ("dwFlags", ctypes.c_ulong)]


def work_area_at_mouse() -> tuple[int, int, int, int]:
    """Work area (left, top, right, bottom) of the monitor under the mouse pointer."""
    try:
        u = ctypes.windll.user32
        pt = (ctypes.c_long * 2)()
        u.GetCursorPos(ctypes.byref(pt))
        u.MonitorFromPoint.restype = ctypes.c_void_p
        mon = u.MonitorFromPoint(ctypes.c_longlong(pt[0] & 0xFFFFFFFF | (pt[1] << 32)), 2)  # NEAREST
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(mi)
        u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        return tuple(mi.rcWork)
    except Exception:  # noqa: BLE001
        return 0, 0, 1920, 1080


def bring_existing_window() -> bool:
    """If the window is already open (maybe on another screen), bring it here instead of opening a second one."""
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        title, cls = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
        u.GetWindowTextW(ctypes.c_void_p(hwnd), title, 64)
        u.GetClassNameW(ctypes.c_void_p(hwnd), cls, 64)
        if title.value == "Govee lights" and cls.value == "TkTopLevel" and u.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True

    u.EnumWindows(visit, None)
    if not found:
        return False
    hwnd = ctypes.c_void_p(found[0])
    u.ShowWindow(hwnd, 9)  # SW_RESTORE
    r = (ctypes.c_long * 4)()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r[2] - r[0], r[3] - r[1]
    left, top, right, bottom = work_area_at_mouse()
    if not (left <= r[0] < right and top <= r[1] < bottom):  # on another screen: move it here
        u.SetWindowPos(hwnd, None, left + max(0, (right - left - w) // 2), top + max(0, (bottom - top - h) // 2),
                       0, 0, 0x0001 | 0x0004)  # NOSIZE | NOZORDER
    u.SetForegroundWindow(hwnd)
    return True


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - show it instead of vanishing silently under pythonw
        import traceback
        from tkinter import messagebox
        where = log_error(traceback.format_exc())
        try:
            messagebox.showerror("Govee lights", f"The app stopped because of an error.\nDetails: {where}")
        except Exception:  # noqa: BLE001
            pass
