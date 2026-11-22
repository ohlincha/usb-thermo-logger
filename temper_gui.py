#!/usr/bin/env python3
"""TEMPer Monitor: live display, plot and CSV logging for PCsensor TEMPer sensors (stdlib only, Tk)."""

import csv
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import temper as tp  # noqa: E402

COLOURS = ["#1f4e79", "#b5543c", "#3c8d6e", "#c8892a", "#6a4c93", "#2a7f9e", "#9c3d5a"]
SPANS = {"10 min": 600, "1 h": 3600, "6 h": 21600, "24 h": 86400, "all": None}


class Reader(threading.Thread):
    """Reads every sensor at the set interval (all HID access on this thread) and puts results on a queue."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.interval = 10.0
        self.stop_flag = threading.Event()
        self.rescan_flag = threading.Event()
        self.rescan_flag.set()
        self.sensors = []

    def run(self):
        next_t = 0.0
        while not self.stop_flag.is_set():
            if self.rescan_flag.is_set():
                self.rescan_flag.clear()
                for s in self.sensors:
                    s.close()
                self.sensors, errors = tp.open_all()
                self.out.put(("sensors", [s.label for s in self.sensors], errors))
                next_t = 0.0
            now = time.time()
            if now >= next_t:
                next_t = now + self.interval
                for s in self.sensors:
                    try:
                        self.out.put(("reading", s.label, now, s.read(), s.last_raw.hex(" ")))
                    except tp.HidError as e:
                        self.out.put(("error", s.label, now, str(e)))
            self.stop_flag.wait(0.2)
        for s in self.sensors:
            s.close()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"TEMPer Monitor {tp.__version__}")
        root.geometry("1000x640")
        root.minsize(760, 480)
        self.q = queue.Queue()
        self.data = {}            # label -> list of (t, temperature, humidity)
        self.rows = {}            # label -> treeview item
        self.logfile = None
        self.writer = None
        self.reader = Reader(self.q)
        self._build()
        self.reader.start()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(200, self.poll)

    # ---- layout
    def _build(self):
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Interval").pack(side="left")
        self.interval = tk.DoubleVar(value=10.0)
        sp = ttk.Spinbox(top, from_=1, to=3600, increment=1, width=6, textvariable=self.interval,
                         command=self.set_interval)
        sp.bind("<Return>", lambda e: self.set_interval())
        sp.bind("<FocusOut>", lambda e: self.set_interval())
        sp.pack(side="left", padx=(4, 2))
        ttk.Label(top, text="s").pack(side="left", padx=(0, 12))
        ttk.Button(top, text="Rescan sensors", command=self.reader.rescan_flag.set).pack(side="left")
        ttk.Label(top, text="Plot").pack(side="left", padx=(16, 4))
        self.span = tk.StringVar(value="1 h")
        cb = ttk.Combobox(top, values=list(SPANS), textvariable=self.span, width=7, state="readonly")
        cb.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        cb.pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear).pack(side="left", padx=(12, 0))
        self.logbtn = ttk.Button(top, text="Log to CSV…", command=self.toggle_log)
        self.logbtn.pack(side="right")
        self.loglabel = ttk.Label(top, text="not logging", foreground="#666")
        self.loglabel.pack(side="right", padx=8)

        cols = ("temperature", "humidity", "updated", "raw")
        self.tree = ttk.Treeview(self.root, columns=cols, height=4, selectmode="none")
        self.tree.heading("#0", text="Sensor")
        self.tree.column("#0", width=260)
        for c, w, title in (("temperature", 120, "Temperature (°C)"), ("humidity", 120, "Humidity (%RH)"),
                            ("updated", 90, "Updated"), ("raw", 220, "Reply bytes")):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="e" if c != "raw" else "w")
        self.tree.pack(fill="x", padx=8)
        big = ("TkDefaultFont", 11)
        ttk.Style().configure("Treeview", font=big, rowheight=24)

        self.canvas = tk.Canvas(self.root, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.status = ttk.Label(self.root, text="", padding=(8, 0, 8, 6), foreground="#666")
        self.status.pack(fill="x")

    # ---- actions
    def set_interval(self):
        try:
            self.reader.interval = max(1.0, float(self.interval.get()))
        except (tk.TclError, ValueError):
            pass

    def clear(self):
        for k in self.data:
            self.data[k] = []
        self.redraw()

    def toggle_log(self):
        if self.logfile:
            self.logfile.close()
            self.logfile = self.writer = None
            self.logbtn.configure(text="Log to CSV…")
            self.loglabel.configure(text="not logging")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=time.strftime("temper_%Y%m%d_%H%M.csv"))
        if not path:
            return
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.logfile = open(path, "a", newline="")
        self.writer = csv.writer(self.logfile)
        if new:
            self.writer.writerow(["time", "sensor", "temperature_C", "humidity_pct", "raw"])
        self.logbtn.configure(text="Stop logging")
        self.loglabel.configure(text="logging to " + os.path.basename(path))

    def quit(self):
        self.reader.stop_flag.set()
        if self.logfile:
            self.logfile.close()
        self.root.destroy()

    # ---- data
    def poll(self):
        changed = False
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "sensors":
                _, labels, errors = msg
                for item in self.tree.get_children():
                    self.tree.delete(item)
                self.rows = {lab: self.tree.insert("", "end", text=lab, values=("—", "—", "", "")) for lab in labels}
                for lab in labels:
                    self.data.setdefault(lab, [])
                txt = f"{len(labels)} sensor(s)"
                if errors:
                    txt += "  ·  " + " | ".join(e.splitlines()[0] for e in errors)
                    if sys.platform.startswith("linux") and any("Permission" in e for e in errors):
                        messagebox.showwarning("TEMPer Monitor", errors[0])
                if not labels and not errors:
                    txt = "No TEMPer sensors found: plug one in and press Rescan sensors."
                self.status.configure(text=txt)
                changed = True
            elif kind == "reading":
                _, lab, t, qd, raw = msg
                temp, hum = qd.get("temperature"), qd.get("humidity")
                self.data.setdefault(lab, []).append((t, temp, hum))
                if lab in self.rows:
                    self.tree.item(self.rows[lab], values=(
                        f"{temp:.2f}" if temp is not None else "—", f"{hum:.1f}" if hum is not None else "—",
                        time.strftime("%H:%M:%S", time.localtime(t)), raw))
                if self.writer:
                    self.writer.writerow([tp.iso_time(t), lab, "" if temp is None else temp,
                                          "" if hum is None else hum, raw])
                    self.logfile.flush()
                changed = True
            elif kind == "error":
                _, lab, t, err = msg
                if lab in self.rows:
                    vals = list(self.tree.item(self.rows[lab], "values"))
                    vals[2] = "error"
                    self.tree.item(self.rows[lab], values=vals)
                self.status.configure(text=f"{lab}: {err}")
        if changed:
            self.redraw()
        self.root.after(250, self.poll)

    # ---- plot (temperature left axis, humidity right axis)
    def redraw(self):
        c = self.canvas
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 100 or H < 80:
            return
        L, R, T, B = 60, 60, 14, 34
        span = SPANS.get(self.span.get())
        now = time.time()
        series = {k: [p for p in v if span is None or p[0] >= now - span] for k, v in self.data.items()}
        pts = [p for v in series.values() for p in v]
        if not pts:
            c.create_text(W / 2, H / 2, text="waiting for readings…", fill="#888")
            return
        # start at the first reading on the left and grow to the right; scroll once the span is full
        t0 = min(p[0] for p in pts)
        t1 = max(now, t0 + 60)
        temps = [p[1] for p in pts if p[1] is not None]
        hums = [p[2] for p in pts if p[2] is not None]

        def rng(vals, minspan):
            lo, hi = min(vals), max(vals)
            if hi - lo < minspan:
                mid = (lo + hi) / 2
                lo, hi = mid - minspan / 2, mid + minspan / 2
            pad = 0.08 * (hi - lo)
            return lo - pad, hi + pad

        tlo, thi = rng(temps, 1.0) if temps else (0, 1)
        hlo, hhi = rng(hums, 5.0) if hums else (0, 100)
        x = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R)
        yt = lambda v: T + (thi - v) / (thi - tlo) * (H - T - B)
        yh = lambda v: T + (hhi - v) / (hhi - hlo) * (H - T - B)
        c.create_rectangle(L, T, W - R, H - B, outline="#bbb")
        for i in range(6):
            v = tlo + (thi - tlo) * i / 5
            y = yt(v)
            c.create_line(L, y, W - R, y, fill="#eee")
            c.create_text(L - 6, y, text=f"{v:.1f}", anchor="e", fill="#555")
            if hums:
                hv = hlo + (hhi - hlo) * i / 5
                c.create_text(W - R + 6, yh(hv), text=f"{hv:.0f}", anchor="w", fill="#555")
        c.create_text(14, (T + H - B) / 2, text="°C", fill="#555")
        if hums:
            c.create_text(W - 14, (T + H - B) / 2, text="%RH", fill="#555")
        for i in range(5):
            t = t0 + (t1 - t0) * i / 4
            c.create_text(x(t), H - B + 12, text=time.strftime("%H:%M" if t1 - t0 > 120 else "%H:%M:%S",
                                                              time.localtime(t)), fill="#555")
        legend_y = T + 8
        names = sorted(series)
        lw = max(len(n) for n in names) * 7 + 150
        legend = []
        for n, (lab, v) in enumerate(sorted(series.items())):
            col = COLOURS[n % len(COLOURS)]
            tp_ = [(x(p[0]), yt(p[1])) for p in v if p[1] is not None]
            hp_ = [(x(p[0]), yh(p[2])) for p in v if p[2] is not None]
            if len(tp_) > 1:
                c.create_line(*[q for pt in tp_ for q in pt], fill=col, width=2)
            for px, py in tp_[-1:]:
                c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=col, outline=col)
            if len(hp_) > 1:
                c.create_line(*[q for pt in hp_ for q in pt], fill=col, width=1, dash=(4, 3))
            legend.append((col, lab + ("  (dashed: %RH)" if hp_ else "")))
        c.create_rectangle(L + 4, T + 1, L + 4 + lw, T + 4 + 16 * len(names), fill="white", outline="#ddd")
        for col, text in legend:
            c.create_line(L + 10, legend_y, L + 30, legend_y, fill=col, width=2)
            c.create_text(L + 36, legend_y, text=text, anchor="w", fill="#333")
            legend_y += 16


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
