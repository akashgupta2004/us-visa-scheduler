"""
=============================================================
  Visa Bot GUI — gui.py
  A complete desktop control panel for the US Visa Bot system.
  Tabs: Configure | Live Slots | Slot Monitor | Bot2 (OFC)
=============================================================
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import calendar
import json
import csv
import os
import sys
import subprocess
import threading
import queue
import requests
from pathlib import Path
from datetime import datetime

# ─── Script Paths ────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
ENV_FILE      = BASE_DIR / ".env"
SEC_Q_FILE    = BASE_DIR / "security_questions.json"
CSV_FILE      = BASE_DIR / "slot_notification.csv"
MONITOR_SCRIPT = BASE_DIR / "slot_monitor_qualified (1).py"
BOT2_SCRIPT   = BASE_DIR / "bot2_ofc_booking.py"
BOT_SCRIPT    = BASE_DIR / "bot.py"

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
CDP_PORT = 9222

# ─── VisaSlots API ───────────────────────────────────────────
API_URL = "https://app.checkvisaslots.com/slots/v3"
API_HEADERS = {
    "accept": "*/*",
    "extversion": "4.7.0.2",
    "origin": "chrome-extension://beepaenfejnphdgnkmccjcfiieihhogl",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-api-key": "4XYRAN",
}

CITY_OPTIONS = ["CHENNAI", "MUMBAI", "HYDERABAD", "DELHI", "KOLKATA", "ANY"]

# ─── Colors ──────────────────────────────────────────────────
BG           = "#1e1e2e"
SURFACE      = "#2a2a3e"
ACCENT       = "#7c3aed"
ACCENT_HOVER = "#6d28d9"
SUCCESS      = "#22c55e"
DANGER       = "#ef4444"
WARNING      = "#f59e0b"
TEXT         = "#e2e8f0"
SUBTEXT      = "#94a3b8"
ENTRY_BG     = "#16213e"
ENTRY_FG     = "#e2e8f0"
BORDER       = "#3f3f5a"

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def read_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def write_env(env: dict):
    lines = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            l = line.strip()
            if l and not l.startswith("#") and "=" in l:
                k = l.split("=", 1)[0].strip()
                if k in env:
                    lines.append(f"{k}={env.pop(k)}")
                else:
                    lines.append(line)
            else:
                lines.append(line)
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

def read_security_questions() -> dict:
    if SEC_Q_FILE.exists():
        try:
            return json.loads(SEC_Q_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}

def write_security_questions(data: dict):
    SEC_Q_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def read_csv_rows() -> list[dict]:
    defaults = {"customer_name": "", "ofc_location": "CHENNAI",
                "consular_location": "CHENNAI", "need_before": "30 Jun 2026", "min_slots": "2"}
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            if rows:
                return [{**defaults, **r} for r in rows]
    return [defaults.copy()]

def write_csv_rows(rows: list[dict]):
    fieldnames = ["customer_name","ofc_location","consular_location","need_before","min_slots"]
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

# ─────────────────────────────────────────────────────────────
# Calendar Date Picker Popup
# ─────────────────────────────────────────────────────────────

MONTH_NAMES = ["January","February","March","April","May","June",
               "July","August","September","October","November","December"]

class CalendarPicker(tk.Toplevel):
    """Dark-themed calendar popup. Calls on_select(date_str) with 'dd Mon yyyy'."""

    def __init__(self, parent, on_select, initial_date=None):
        super().__init__(parent)
        self.on_select = on_select
        self.overrideredirect(True)          # borderless
        self.configure(bg=SURFACE)
        self.attributes("-topmost", True)

        now = datetime.now()
        if initial_date:
            try:
                parsed = datetime.strptime(initial_date, "%d %b %Y")
                self._year  = parsed.year
                self._month = parsed.month
            except Exception:
                self._year, self._month = now.year, now.month
        else:
            self._year, self._month = now.year, now.month

        self._build()
        self._draw()

        # Position below parent widget
        self.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty() + parent.winfo_height()
        self.geometry(f"+{px}+{py}")

        # Click outside to close
        self.bind("<FocusOut>", self._on_focus_out)
        self.focus_set()

    def _build(self):
        # ── Header (prev / month-year / next) ──
        hdr = tk.Frame(self, bg=ACCENT, pady=6)
        hdr.pack(fill="x")
        tk.Button(hdr, text="‹", command=self._prev_month,
                  bg=ACCENT, fg="white", activebackground=ACCENT_HOVER,
                  relief="flat", font=("Segoe UI", 12, "bold"),
                  cursor="hand2", bd=0, padx=8).pack(side="left")
        tk.Button(hdr, text="›", command=self._next_month,
                  bg=ACCENT, fg="white", activebackground=ACCENT_HOVER,
                  relief="flat", font=("Segoe UI", 12, "bold"),
                  cursor="hand2", bd=0, padx=8).pack(side="right")
        self._header_lbl = tk.Label(hdr, text="", font=("Segoe UI", 10, "bold"),
                                    fg="white", bg=ACCENT)
        self._header_lbl.pack()

        # ── Day-of-week row ──
        dow_frame = tk.Frame(self, bg=SURFACE, pady=4)
        dow_frame.pack(fill="x", padx=4)
        for d in ["Su","Mo","Tu","We","Th","Fr","Sa"]:
            tk.Label(dow_frame, text=d, width=3,
                     font=("Segoe UI", 8, "bold"),
                     fg=SUBTEXT, bg=SURFACE).pack(side="left", padx=2)

        # ── Day grid ──
        self._grid_frame = tk.Frame(self, bg=SURFACE)
        self._grid_frame.pack(padx=4, pady=(0, 6))

    def _draw(self):
        for w in self._grid_frame.winfo_children():
            w.destroy()
        self._header_lbl.config(text=f"{MONTH_NAMES[self._month-1]}  {self._year}")

        cal = calendar.monthcalendar(self._year, self._month)
        today = datetime.now()

        for week in cal:
            row = tk.Frame(self._grid_frame, bg=SURFACE)
            row.pack()
            for day in week:
                if day == 0:
                    tk.Label(row, text="", width=3, bg=SURFACE).pack(side="left", padx=2, pady=2)
                else:
                    is_today = (day == today.day and self._month == today.month
                                and self._year == today.year)
                    bg_col  = ACCENT if is_today else SURFACE
                    fg_col  = "white"  if is_today else TEXT
                    btn = tk.Button(
                        row, text=str(day), width=3,
                        bg=bg_col, fg=fg_col,
                        activebackground=ACCENT_HOVER, activeforeground="white",
                        relief="flat", font=("Segoe UI", 9),
                        cursor="hand2", bd=0,
                        command=lambda d=day: self._pick(d)
                    )
                    btn.pack(side="left", padx=2, pady=2)

    def _prev_month(self):
        if self._month == 1:
            self._month, self._year = 12, self._year - 1
        else:
            self._month -= 1
        self._draw()

    def _next_month(self):
        if self._month == 12:
            self._month, self._year = 1, self._year + 1
        else:
            self._month += 1
        self._draw()

    def _pick(self, day):
        date_str = datetime(self._year, self._month, day).strftime("%d %b %Y")
        self.on_select(date_str)
        self.destroy()

    def _on_focus_out(self, event):
        self.after(100, self._check_focus)

    def _check_focus(self):
        try:
            focused = self.focus_get()
            if focused is None or str(focused) == str(self):
                pass
            else:
                self.destroy()
        except Exception:
            try:
                self.destroy()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# Reusable UI Components
# ─────────────────────────────────────────────────────────────

def styled_label(parent, text, font=("Segoe UI", 10), fg=TEXT, **kw):
    return tk.Label(parent, text=text, font=font, fg=fg, bg=BG, **kw)

def section_label(parent, text):
    f = tk.Frame(parent, bg=BG)
    tk.Label(f, text=text, font=("Segoe UI", 11, "bold"), fg=ACCENT, bg=BG).pack(side="left")
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10,0), pady=6)
    return f

def styled_entry(parent, textvariable=None, show=None, width=30):
    e = tk.Entry(parent, textvariable=textvariable, show=show, width=width,
                 bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ACCENT,
                 relief="flat", font=("Segoe UI", 10),
                 highlightthickness=1, highlightcolor=ACCENT, highlightbackground=BORDER)
    return e

def styled_combo(parent, values, textvariable=None, width=18):
    style = ttk.Style()
    style.configure("Custom.TCombobox",
                    fieldbackground=ENTRY_BG, background=ENTRY_BG,
                    foreground=ENTRY_FG, selectbackground=ACCENT,
                    arrowcolor=ACCENT)
    c = ttk.Combobox(parent, values=values, textvariable=textvariable,
                     width=width, state="readonly", style="Custom.TCombobox",
                     font=("Segoe UI", 10))
    return c

def styled_button(parent, text, command, bg=ACCENT, fg="white", padx=18, pady=6):
    btn = tk.Button(parent, text=text, command=command,
                    bg=bg, fg=fg, activebackground=ACCENT_HOVER, activeforeground="white",
                    font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
                    padx=padx, pady=pady, bd=0)
    btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HOVER))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn

def status_dot(parent, color=SUCCESS):
    c = tk.Canvas(parent, width=10, height=10, bg=SURFACE, highlightthickness=0)
    c.create_oval(1, 1, 9, 9, fill=color, outline="")
    return c

def log_widget(parent):
    st = scrolledtext.ScrolledText(parent, bg="#0d0d1a", fg="#a0e982",
                                   font=("Consolas", 9), relief="flat",
                                   insertbackground=ACCENT, wrap="word",
                                   state="disabled", height=20)
    st.tag_configure("WARN",    foreground=WARNING)
    st.tag_configure("ERROR",   foreground=DANGER)
    st.tag_configure("SUCCESS", foreground=SUCCESS)
    st.tag_configure("INFO",    foreground="#a0e982")
    return st

def log_append(widget, text):
    widget.config(state="normal")
    tag = "INFO"
    if any(x in text for x in ["❌", "error", "Error", "failed", "HTTP 4"]):
        tag = "ERROR"
    elif any(x in text for x in ["⚠️", "warn", "Warning"]):
        tag = "WARN"
    elif any(x in text for x in ["✅", "BOOKED", "Alert sent"]):
        tag = "SUCCESS"
    widget.insert("end", text + "\n", tag)
    widget.see("end")
    widget.config(state="disabled")

# ─────────────────────────────────────────────────────────────
# Tab 1 — Configuration
# ─────────────────────────────────────────────────────────────

class ConfigTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.customer_rows = []   # list of dicts with tk vars
        self._build()
        self._load()

    def _build(self):
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = tk.Frame(canvas, bg=BG)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        p = self.inner

        # ── Credentials ─────
        section_label(p, "  Visa Credentials").pack(fill="x", padx=20, pady=(20, 4))
        row = tk.Frame(p, bg=BG); row.pack(fill="x", padx=20, pady=4)
        styled_label(row, "Username:", width=18, anchor="w").pack(side="left")
        self.username_var = tk.StringVar()
        styled_entry(row, textvariable=self.username_var, width=32).pack(side="left", padx=6)

        row2 = tk.Frame(p, bg=BG); row2.pack(fill="x", padx=20, pady=4)
        styled_label(row2, "Password:", width=18, anchor="w").pack(side="left")
        self.password_var = tk.StringVar()
        self.pw_entry = styled_entry(row2, textvariable=self.password_var, show="●", width=32)
        self.pw_entry.pack(side="left", padx=6)
        self.show_pw = tk.BooleanVar(value=False)
        tk.Checkbutton(row2, text="Show", variable=self.show_pw, command=self._toggle_pw,
                       bg=BG, fg=SUBTEXT, activebackground=BG, selectcolor=SURFACE,
                       font=("Segoe UI", 9)).pack(side="left")

        # ── Security Questions ─────
        section_label(p, "  Security Question Answers").pack(fill="x", padx=20, pady=(18, 4))
        self.sq_entries = {}
        for label_text, key in [
            ("Favourite Food", "favourite food"),
            ("Mother's Maiden Name", "mother's maiden name"),
            ("City", "city were you born"),
        ]:
            r = tk.Frame(p, bg=BG); r.pack(fill="x", padx=20, pady=4)
            styled_label(r, label_text + ":", width=22, anchor="w").pack(side="left")
            var = tk.StringVar()
            styled_entry(r, textvariable=var, width=26).pack(side="left", padx=6)
            self.sq_entries[key] = var

        # ── Booking Criteria — Multi-Customer Table ─────
        section_label(p, "  Booking Criteria (Customers)").pack(fill="x", padx=20, pady=(18, 4))

        # Table header
        hdr = tk.Frame(p, bg=SURFACE)
        hdr.pack(fill="x", padx=20, pady=(4, 0))
        headers = [("Customer Name", 16), ("OFC City", 12), ("Consular City", 12),
                   ("Need Before", 13), ("Min Slots", 6), ("", 3)]
        for txt, w in headers:
            tk.Label(hdr, text=txt, width=w, font=("Segoe UI", 9, "bold"),
                     fg=TEXT, bg=SURFACE, anchor="w").pack(side="left", padx=2, pady=4)

        # Scrollable customer rows container
        self._cust_frame = tk.Frame(p, bg=BG)
        self._cust_frame.pack(fill="x", padx=20, pady=2)

        # Add customer button
        btn_row = tk.Frame(p, bg=BG); btn_row.pack(fill="x", padx=20, pady=(4, 2))
        styled_button(btn_row, "➕  Add Customer", self._add_customer_row, bg="#059669", padx=14, pady=6).pack(side="left")

        # ── Save Button ─────
        save_row = tk.Frame(p, bg=BG); save_row.pack(pady=16)
        styled_button(save_row, "💾  Save All Settings", self._save, padx=24, pady=10).pack()
        self.status_lbl = styled_label(p, "", fg=SUCCESS)
        self.status_lbl.pack()

    def _add_customer_row(self, data: dict | None = None):
        defaults = data or {"customer_name": "", "ofc_location": "CHENNAI",
                            "consular_location": "CHENNAI", "need_before": "30 Jun 2026", "min_slots": "2"}
        row_frame = tk.Frame(self._cust_frame, bg=BG)
        row_frame.pack(fill="x", pady=1)

        name_var = tk.StringVar(value=defaults.get("customer_name", ""))
        styled_entry(row_frame, textvariable=name_var, width=16).pack(side="left", padx=2)

        ofc_var = tk.StringVar(value=defaults.get("ofc_location", "CHENNAI"))
        styled_combo(row_frame, CITY_OPTIONS, textvariable=ofc_var, width=10).pack(side="left", padx=2)

        consular_var = tk.StringVar(value=defaults.get("consular_location", "CHENNAI"))
        styled_combo(row_frame, CITY_OPTIONS, textvariable=consular_var, width=10).pack(side="left", padx=2)

        date_var = tk.StringVar(value=defaults.get("need_before", "30 Jun 2026"))
        date_entry = styled_entry(row_frame, textvariable=date_var, width=11)
        date_entry.pack(side="left", padx=(2, 0))
        cal_btn = tk.Button(
            row_frame, text="📅", bg=ACCENT, fg="white", activebackground=ACCENT_HOVER,
            relief="flat", font=("Segoe UI", 9), cursor="hand2", padx=4, pady=1, bd=0,
            command=lambda: CalendarPicker(cal_btn, on_select=lambda d: date_var.set(d), initial_date=date_var.get())
        )
        cal_btn.pack(side="left")

        minslots_var = tk.StringVar(value=str(defaults.get("min_slots", "2")))
        styled_entry(row_frame, textvariable=minslots_var, width=5).pack(side="left", padx=2)

        entry = {"frame": row_frame, "name": name_var, "ofc": ofc_var,
                 "consular": consular_var, "date": date_var, "minslots": minslots_var}

        remove_btn = tk.Button(
            row_frame, text="✕", bg=DANGER, fg="white", activebackground="#dc2626",
            relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2",
            padx=6, pady=1, bd=0,
            command=lambda e=entry: self._remove_customer_row(e)
        )
        remove_btn.pack(side="left", padx=4)

        self.customer_rows.append(entry)

    def _remove_customer_row(self, entry):
        if len(self.customer_rows) <= 1:
            return   # keep at least one row
        entry["frame"].destroy()
        self.customer_rows.remove(entry)

    def _open_calendar(self):
        CalendarPicker(
            self._cal_btn,
            on_select=lambda d: self.date_var.set(d),
            initial_date=self.date_var.get()
        )

    def _toggle_pw(self):
        self.pw_entry.config(show="" if self.show_pw.get() else "●")

    def _load(self):
        env = read_env()
        self.username_var.set(env.get("VISA_USERNAME", ""))
        self.password_var.set(env.get("VISA_PASSWORD", ""))

        sq = read_security_questions()
        for key, var in self.sq_entries.items():
            val = sq.get(key, "")
            var.set("" if val == "NA" else val)

        # Load multiple customers from CSV
        csv_rows = read_csv_rows()
        for row_data in csv_rows:
            self._add_customer_row(row_data)

    def _save(self):
        # Save credentials
        write_env({
            "VISA_USERNAME": self.username_var.get().strip(),
            "VISA_PASSWORD": self.password_var.get().strip(),
        })

        # Save security questions
        sq = read_security_questions()
        for key, var in self.sq_entries.items():
            val = var.get().strip()
            sq[key] = val if val else "NA"
        write_security_questions(sq)

        # Save all customer rows to CSV
        rows = []
        for entry in self.customer_rows:
            rows.append({
                "customer_name": entry["name"].get().strip(),
                "ofc_location": entry["ofc"].get(),
                "consular_location": entry["consular"].get(),
                "need_before": entry["date"].get().strip(),
                "min_slots": entry["minslots"].get().strip() or "1",
            })
        write_csv_rows(rows)

        self.status_lbl.config(text=f"✅ Saved {len(rows)} customer(s) successfully!", fg=SUCCESS)
        self.after(3000, lambda: self.status_lbl.config(text=""))


# ─────────────────────────────────────────────────────────────
# Tab 2 — Live Slots Viewer
# ─────────────────────────────────────────────────────────────

class SlotsTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._build()

    def _build(self):
        top = tk.Frame(self, bg=BG); top.pack(fill="x", padx=20, pady=(20, 10))
        styled_label(top, "Live Available Slots", font=("Segoe UI", 13, "bold"), fg=TEXT).pack(side="left")
        styled_button(top, "🔄  Refresh", self._fetch, padx=16, pady=7).pack(side="right")

        self.status_bar = styled_label(self, "Click Refresh to fetch latest slots.", fg=SUBTEXT)
        self.status_bar.pack(padx=20, anchor="w")

        cols = ("Location", "Type", "Earliest Date", "Slots")
        container = tk.Frame(self, bg=BG); container.pack(fill="both", expand=True, padx=20, pady=10)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background=SURFACE, foreground=TEXT, fieldbackground=SURFACE,
                        rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=ACCENT, foreground="white",
                        font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", ACCENT)])

        self.tree = ttk.Treeview(container, columns=cols, show="headings", style="Treeview")
        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        widths = [200, 120, 160, 80]
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center")

        self.tree.tag_configure("available", background="#1a3a2a", foreground="#86efac")
        self.tree.tag_configure("empty", background=SURFACE, foreground=SUBTEXT)

    def _fetch(self):
        self.status_bar.config(text="⏳ Fetching...", fg=WARNING)
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self._do_fetch, daemon=True).start()

    def _do_fetch(self):
        try:
            r = requests.get(API_URL, headers=API_HEADERS, timeout=15)
            if r.status_code == 429:
                self.after(0, lambda: self.status_bar.config(
                    text="⚠️ Rate limited (HTTP 429). Switch IP or wait until tomorrow.", fg=DANGER))
                return
            r.raise_for_status()
            data = r.json()
            slots = data.get("slotDetails", [])
            self.after(0, lambda: self._populate(slots))
        except Exception as e:
            self.after(0, lambda: self.status_bar.config(text=f"❌ Error: {e}", fg=DANGER))

    def _populate(self, slots):
        self.tree.delete(*self.tree.get_children())
        available_count = 0
        for s in sorted(slots, key=lambda x: x.get("visa_location", "")):
            loc = s.get("visa_location", "—")
            kind = "OFC (Biometrics)" if "VAC" in loc.upper() else "Consular"
            date = s.get("start_date", "—") or "—"
            count = s.get("slots", 0)
            tag = "available" if int(count or 0) > 0 else "empty"
            if int(count or 0) > 0:
                available_count += 1
            self.tree.insert("", "end", values=(loc, kind, date, count), tags=(tag,))
        now = datetime.now().strftime("%H:%M:%S")
        self.status_bar.config(
            text=f"✅ {len(slots)} entries fetched at {now}. {available_count} locations have available slots.",
            fg=SUCCESS if available_count > 0 else SUBTEXT)


# ─────────────────────────────────────────────────────────────
# Sub-process Tab (base for Monitor + Bot2)
# ─────────────────────────────────────────────────────────────

class ProcessTab(tk.Frame):
    def __init__(self, parent, script_path: Path, title: str, description: str):
        super().__init__(parent, bg=BG)
        self.script_path = script_path
        self.process = None
        self.log_q = queue.Queue()
        self._build(title, description)

    def _build(self, title, description):
        header = tk.Frame(self, bg=SURFACE); header.pack(fill="x")
        tk.Label(header, text=title, font=("Segoe UI", 14, "bold"),
                 fg=TEXT, bg=SURFACE, pady=12, padx=20).pack(side="left")
        self.dot = status_dot(header, color=SUBTEXT)
        self.dot.pack(side="left", padx=4)
        self.status_label = tk.Label(header, text="Stopped", font=("Segoe UI", 9),
                                     fg=SUBTEXT, bg=SURFACE)
        self.status_label.pack(side="left")

        desc_row = tk.Frame(self, bg=BG); desc_row.pack(fill="x", padx=20, pady=(12,4))
        styled_label(desc_row, description, fg=SUBTEXT, font=("Segoe UI", 9)).pack(side="left")

        btn_row = tk.Frame(self, bg=BG); btn_row.pack(fill="x", padx=20, pady=8)
        self.start_btn = styled_button(btn_row, "▶  Start", self.start, bg=SUCCESS, padx=20, pady=8)
        self.start_btn.pack(side="left", padx=(0, 10))
        self.stop_btn = styled_button(btn_row, "⏹  Stop", self.stop, bg=DANGER, padx=20, pady=8)
        self.stop_btn.pack(side="left")
        self.stop_btn.config(state="disabled")
        styled_button(btn_row, "🗑  Clear Log", self._clear_log, bg=SURFACE, padx=16, pady=8).pack(side="right")

        log_frame = tk.Frame(self, bg=BG); log_frame.pack(fill="both", expand=True, padx=20, pady=(0,16))
        self.log = log_widget(log_frame)
        self.log.pack(fill="both", expand=True)

    def _set_status(self, running: bool):
        if running:
            self.dot.delete("all"); self.dot.create_oval(1,1,9,9, fill=SUCCESS, outline="")
            self.status_label.config(text="Running", fg=SUCCESS)
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
        else:
            self.dot.delete("all"); self.dot.create_oval(1,1,9,9, fill=SUBTEXT, outline="")
            self.status_label.config(text="Stopped", fg=SUBTEXT)
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

    def start(self):
        if self.process and self.process.poll() is None:
            return
        log_append(self.log, f"[{datetime.now():%H:%M:%S}] Starting {self.script_path.name}…")
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(self.script_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR),
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                env=env,
            )
            self._set_status(True)
            threading.Thread(target=self._stream_output, daemon=True).start()
            self._poll_queue()
        except Exception as e:
            log_append(self.log, f"❌ Failed to start: {e}")

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
        self._set_status(False)
        log_append(self.log, f"[{datetime.now():%H:%M:%S}] Process stopped by user.")

    def _stream_output(self):
        try:
            for line in iter(self.process.stdout.readline, ""):
                self.log_q.put(line.rstrip())
            self.process.stdout.close()
        except Exception:
            pass
        finally:
            self.log_q.put("__EXIT__")

    def _poll_queue(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                if line == "__EXIT__":
                    self._set_status(False)
                    log_append(self.log, f"[{datetime.now():%H:%M:%S}] Process exited.")
                    return
                log_append(self.log, line)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")


# ─────────────────────────────────────────────────────────────
# Chrome Launcher Helper
# ─────────────────────────────────────────────────────────────

import socket

def _is_chrome_debug_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False

def _find_chrome_exe() -> str | None:
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None

# ─────────────────────────────────────────────────────────────
# Per-Customer Bot Panel
# ─────────────────────────────────────────────────────────────

class CustomerBotPanel(tk.Frame):
    def __init__(self, parent, customer_name: str, cdp_port: int):
        super().__init__(parent, bg=BG)
        self.customer_name = customer_name
        self.cdp_port = cdp_port
        self.bot_process = None
        self.log_q = queue.Queue()
        self._build()
        self._refresh_chrome_status()

    def _build(self):
        # Header
        header = tk.Frame(self, bg=SURFACE); header.pack(fill="x")
        tk.Label(header, text=f"👤 {self.customer_name}", font=("Segoe UI", 13, "bold"),
                 fg=TEXT, bg=SURFACE, pady=12, padx=16).pack(side="left")
                 
        tk.Label(header, text=f"(Port {self.cdp_port})", font=("Segoe UI", 9),
                 fg=SUBTEXT, bg=SURFACE).pack(side="left")

        # Chrome status dot
        self.chrome_dot = status_dot(header, color=SUBTEXT)
        self.chrome_dot.pack(side="right", padx=6)
        self.chrome_status_lbl = tk.Label(header, text="Chrome: ?", font=("Segoe UI", 9),
                                          fg="#c4b5fd", bg=SURFACE)
        self.chrome_status_lbl.pack(side="right", padx=2)

        # Chrome Buttons
        btn_row1 = tk.Frame(self, bg=BG); btn_row1.pack(fill="x", padx=16, pady=(16, 4))
        styled_button(btn_row1, "🌐  Launch Chrome", self._launch_chrome, bg="#059669").pack(side="left", padx=(0, 10))
        styled_label(btn_row1, "(Please login manually in the launched window)", fg=SUBTEXT, font=("Segoe UI", 9)).pack(side="left")

        # Bot Buttons
        btn_row2 = tk.Frame(self, bg=BG); btn_row2.pack(fill="x", padx=16, pady=8)
        self.bot_dot = status_dot(btn_row2, color=SUBTEXT)
        self.bot_dot.pack(side="left", padx=(0, 6))
        self.status_label = tk.Label(btn_row2, text="Bot: Stopped", font=("Segoe UI", 9, "bold"),
                                     fg=SUBTEXT, bg=BG)
        self.status_label.pack(side="left", padx=(0, 16))

        self.start_btn = styled_button(btn_row2, "▶  Start Booking Bot", self.start_bot, bg=SUCCESS)
        self.start_btn.pack(side="left", padx=(0, 10))
        self.stop_btn = styled_button(btn_row2, "⏹  Stop Bot", self.stop_bot, bg=DANGER)
        self.stop_btn.pack(side="left")
        self.stop_btn.config(state="disabled")
        
        styled_button(btn_row2, "🗑  Clear Log", self._clear_log, bg=SURFACE, padx=10).pack(side="right")

        # Log Window
        log_frame = tk.Frame(self, bg=BG); log_frame.pack(fill="both", expand=True, padx=16, pady=(0,16))
        self.log = log_widget(log_frame)
        self.log.pack(fill="both", expand=True)

    def _refresh_chrome_status(self):
        running = _is_chrome_debug_running(self.cdp_port)
        if running:
            self.chrome_dot.delete("all"); self.chrome_dot.create_oval(1,1,9,9, fill=SUCCESS, outline="")
            self.chrome_status_lbl.config(text="Chrome: Connected", fg="#86efac")
        else:
            self.chrome_dot.delete("all"); self.chrome_dot.create_oval(1,1,9,9, fill=DANGER, outline="")
            self.chrome_status_lbl.config(text="Chrome: Not Running", fg="#fca5a5")
        self.after(3000, self._refresh_chrome_status)

    def _launch_chrome(self):
        if _is_chrome_debug_running(self.cdp_port):
            messagebox.showinfo("Chrome", f"Chrome debug port {self.cdp_port} is already active for {self.customer_name}.")
            return
        chrome = _find_chrome_exe()
        if not chrome:
            messagebox.showerror("Chrome Not Found", "Could not find chrome.exe.")
            return
        
        profile_dir = BASE_DIR / f"chrome_profile_{self.customer_name.replace(' ', '_')}"
        profile_dir.mkdir(exist_ok=True)
        try:
            proc = subprocess.Popen([
                chrome,
                f"--remote-debugging-port={self.cdp_port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "https://www.usvisascheduling.com/en-US/",
            ])
            log_append(self.log, f"[{datetime.now():%H:%M:%S}] 🌐 Launched Chrome on port {self.cdp_port}.")
            log_append(self.log, f"      -> Profile: {profile_dir.name}")

        except Exception as e:
            log_append(self.log, f"❌ Failed to launch Chrome: {e}")

    def _set_status(self, running: bool):
        if running:
            self.bot_dot.delete("all"); self.bot_dot.create_oval(1,1,9,9, fill=SUCCESS, outline="")
            self.status_label.config(text="Bot: Running", fg=SUCCESS)
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
        else:
            self.bot_dot.delete("all"); self.bot_dot.create_oval(1,1,9,9, fill=SUBTEXT, outline="")
            self.status_label.config(text="Bot: Stopped", fg=SUBTEXT)
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

    def start_bot(self):
        if not _is_chrome_debug_running(self.cdp_port):
            messagebox.showwarning("Chrome Not Running", f"Please launch Chrome for {self.customer_name} first.")
            return
        if self.bot_process and self.bot_process.poll() is None:
            return
        
        log_append(self.log, f"[{datetime.now():%H:%M:%S}] 🚀 Starting Bot2 for {self.customer_name}...")
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            self.bot_process = subprocess.Popen(
                [sys.executable, "-u", str(BOT2_SCRIPT), "--cdp-port", str(self.cdp_port), "--customer", self.customer_name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR),
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                env=env,
            )
            self._set_status(True)
            threading.Thread(target=self._stream_output, daemon=True).start()
            self._poll_queue()
        except Exception as e:
            log_append(self.log, f"❌ Failed to start Bot: {e}")

    def stop_bot(self):
        if self.bot_process:
            try:
                self.bot_process.terminate()
            except Exception:
                pass
            self.bot_process = None
        self._set_status(False)
        log_append(self.log, f"[{datetime.now():%H:%M:%S}] ⏹ Bot stopped by user.")

    def _stream_output(self):
        try:
            for line in iter(self.bot_process.stdout.readline, ""):
                self.log_q.put(line.rstrip())
            self.bot_process.stdout.close()
        except Exception:
            pass
        finally:
            self.log_q.put("__EXIT__")

    def _poll_queue(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                if line == "__EXIT__":
                    self._set_status(False)
                    log_append(self.log, f"[{datetime.now():%H:%M:%S}] ⏹ Process exited.")
                    return
                log_append(self.log, line)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")


# ─────────────────────────────────────────────────────────────
# Multi-Bot Tab (Container for isolated customers)
# ─────────────────────────────────────────────────────────────

class MultiBotTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.panels = {}
        self.current_panel = None
        self._build()

    def _build(self):
        # Left sidebar for customer list
        sidebar = tk.Frame(self, bg=SURFACE, width=200)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        top_f = tk.Frame(sidebar, bg=SURFACE); top_f.pack(fill="x", pady=10, padx=10)
        tk.Label(top_f, text="Customers", font=("Segoe UI", 11, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        styled_button(top_f, "🔄", self._load_customers, bg=ACCENT, padx=6, pady=2).pack(side="right")

        self.listbox = tk.Listbox(sidebar, bg=ENTRY_BG, fg=TEXT, font=("Segoe UI", 10),
                                  selectbackground=ACCENT, selectforeground="white",
                                  relief="flat", highlightthickness=0)
        self.listbox.pack(fill="both", expand=True, padx=10, pady=(0,10))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # Right main area
        self.main_area = tk.Frame(self, bg=BG)
        self.main_area.pack(side="right", fill="both", expand=True)
        
        self.empty_lbl = tk.Label(self.main_area, text="Select a customer from the sidebar.",
                                  font=("Segoe UI", 11), fg=SUBTEXT, bg=BG)
        self.empty_lbl.pack(expand=True)

        self._load_customers()

    def _load_customers(self):
        self.listbox.delete(0, "end")
        rows = read_csv_rows()
        base_port = 9222
        
        for i, row in enumerate(rows):
            name = row.get("customer_name", "").strip()
            if not name:
                continue
            self.listbox.insert("end", name)
            
            if name not in self.panels:
                port = base_port + i
                self.panels[name] = CustomerBotPanel(self.main_area, name, port)

    def _on_select(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.listbox.get(sel[0])
        self._show_panel(name)

    def _show_panel(self, name):
        if self.current_panel:
            self.current_panel.pack_forget()
        self.empty_lbl.pack_forget()
        
        panel = self.panels.get(name)
        if panel:
            panel.pack(fill="both", expand=True)
            self.current_panel = panel

# ─────────────────────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────────────────────

class VisaBotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("US Visa Bot Control Panel")
        self.geometry("960x720")
        self.minsize(800, 600)
        self.configure(bg=BG)
        self.iconbitmap(default="") 

        self._build_title_bar()
        self._build_tabs()

    def _build_title_bar(self):
        bar = tk.Frame(self, bg=ACCENT, height=48); bar.pack(fill="x")
        tk.Label(bar, text="🛂  US Visa Bot  —  Control Panel",
                 font=("Segoe UI", 13, "bold"), fg="white", bg=ACCENT,
                 pady=10, padx=16).pack(side="left")
        tk.Label(bar, text="Multi-Customer Secure Booker",
                 font=("Segoe UI", 9), fg="#c4b5fd", bg=ACCENT,
                 padx=10).pack(side="left")

    def _build_tabs(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=SURFACE, foreground=SUBTEXT,
                        padding=[18, 8], font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])

        nb = ttk.Notebook(self, style="TNotebook")
        nb.pack(fill="both", expand=True, pady=(0, 0))

        config_tab = ConfigTab(nb)
        slots_tab  = SlotsTab(nb)
        monitor_tab = ProcessTab(nb, MONITOR_SCRIPT,
                                 "Slot Monitor",
                                 "Polls the VisaSlots API every 15-20s and fires trigger_{name}.json when a qualifying slot is found.")
        
        # New Multi-Bot isolation tab
        bots_tab = MultiBotTab(nb)

        nb.add(config_tab,   text="⚙️  Configure")
        nb.add(slots_tab,    text="📅  Live Slots")
        nb.add(monitor_tab,  text="🔍  Slot Monitor")
        nb.add(bots_tab,     text="🤖  OFC Bots")


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = VisaBotApp()
    app.mainloop()
