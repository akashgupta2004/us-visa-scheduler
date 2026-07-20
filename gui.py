import os
import sys
import json
import subprocess
import threading
import tkinter as tk
import shutil
import time
from datetime import datetime
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path
from tkcalendar import DateEntry

from src.common.utils import safe_id
from src.common.state import update_state

# ─── Script Paths ────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ACCOUNTS_FILE = BASE_DIR / "accounts.json"
ORCHESTRATOR_SCRIPT = BASE_DIR / "src" / "orchestrator.py"

CITY_OPTIONS = ["CHENNAI", "MUMBAI", "HYDERABAD", "DELHI", "KOLKATA"]

# ─── Colors ──────────────────────────────────────────────────
BG           = "#0f172a"
SURFACE      = "#1e293b"
ACCENT       = "#3b82f6"
ACCENT_HOVER = "#2563eb"
SUCCESS      = "#22c55e"
DANGER       = "#ef4444"
WARNING      = "#f59e0b"
TEXT         = "#f8fafc"
SUBTEXT      = "#94a3b8"
ENTRY_BG     = "#0f172a"
ENTRY_FG     = "#f8fafc"
BORDER       = "#334155"

# ─────────────────────────────────────────────────────────────
# Account Manager & Orchestrator App
# ─────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VisaBot - Orchestrator & Accounts Manager")
        self.geometry("1000x850")
        self.configure(bg=BG)

        self.accounts = []
        self.closed_bots: set = set()
        self.current_account_idx = None
        self.orchestrator_proc = None
        self.polling_proc = None

        self._configure_styles()
        self._load_accounts()
        self._build_ui()
        
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _on_closing(self):
        """Ensure all background processes are killed when the user closes the GUI window with the 'X' button."""
        self.destroy()

    def _configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # General
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Surface.TLabel", background=SURFACE, foreground=TEXT)

        # Notebook
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=SURFACE, foreground=TEXT, 
                        padding=(15, 8), borderwidth=0, font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#ffffff")])

        # Buttons
        style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", 
                        font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        style.map("Primary.TButton", background=[("active", ACCENT_HOVER)])

        style.configure("Success.TButton", background=SUCCESS, foreground="#ffffff", 
                        font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        style.map("Success.TButton", background=[("active", "#16a34a")])

        style.configure("Danger.TButton", background=DANGER, foreground="#ffffff", 
                        font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#dc2626")])

        # Pill style for Checkbuttons
        style.configure("Toolbutton", background=ENTRY_BG, foreground=TEXT, 
                        font=("Segoe UI", 9), padding=(10, 5), borderwidth=1, bordercolor=BORDER)
        style.map("Toolbutton",
                  background=[("selected", SUCCESS), ("active", SURFACE)],
                  foreground=[("selected", "#ffffff"), ("active", TEXT)])

        # Labels & Entries
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), background=SURFACE, foreground=TEXT)
        style.configure("Subhead.TLabel", font=("Segoe UI", 11, "bold"), background=SURFACE, foreground=SUBTEXT)
        style.configure("DateEntry.TEntry", fieldbackground=ENTRY_BG, foreground=ENTRY_FG, insertcolor=TEXT)
        style.map("DateEntry.TEntry",
                  fieldbackground=[("readonly", ENTRY_BG)],
                  foreground=[("readonly", ENTRY_FG)])

    def _load_accounts(self):
        if ACCOUNTS_FILE.exists():
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    self.accounts = json.load(f)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to parse accounts.json:\n{e}")
                self.accounts = []
        else:
            self.accounts = []

    def _save_accounts(self):
        try:
            tmp_file = ACCOUNTS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, indent=2)
            tmp_file.replace(ACCOUNTS_FILE)
            return True
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save accounts.json:\n{e}")
            return False

    def _build_ui(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Tabs
        self.tab_accounts = ttk.Frame(self.notebook, style="TFrame")
        self.tab_orchestrator = ttk.Frame(self.notebook, style="TFrame")
        self.tab_polling = ttk.Frame(self.notebook, style="TFrame")
        self.tab_settings = ttk.Frame(self.notebook, style="TFrame")

        self.notebook.add(self.tab_accounts, text="  Accounts Manager  ")
        self.notebook.add(self.tab_orchestrator, text="  Orchestrator Control  ")
        self.notebook.add(self.tab_polling, text="  Polling Control  ")
        self.notebook.add(self.tab_settings, text="  Settings  ")

        self._build_accounts_tab()
        self._build_orchestrator_tab()
        self._build_polling_tab()
        self._build_settings_tab()

    # ─── Accounts Tab ────────────────────────────────────────────────────────

    def _build_accounts_tab(self):
        # Left side: Listbox of accounts
        left_frame = ttk.Frame(self.tab_accounts, width=250, style="Surface.TFrame")
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)
        left_frame.pack_propagate(False)

        ttk.Label(left_frame, text="Accounts", style="Header.TLabel").pack(pady=(15, 10))

        self.listbox = tk.Listbox(left_frame, bg=ENTRY_BG, fg=ENTRY_FG, font=("Segoe UI", 11),
                                  selectbackground=ACCENT, borderwidth=0, highlightthickness=1, 
                                  highlightbackground=BORDER)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.listbox.bind("<<ListboxSelect>>", self._on_account_select)

        btn_frame = ttk.Frame(left_frame, style="Surface.TFrame")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Add New Account", style="Primary.TButton", 
                   command=self._on_add_account).pack(fill=tk.X)

        # Right side: Form in a scrollable Canvas
        self.right_frame = ttk.Frame(self.tab_accounts, style="TFrame")
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(0, 10), pady=10)

        self.canvas = tk.Canvas(self.right_frame, bg=SURFACE, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.right_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas, style="Surface.TFrame")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw", width=680)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # Vars
        self.var_customer = tk.StringVar()
        self.var_username = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_action_mode = tk.StringVar(value="SNIPER")
        self.var_account_role = tk.StringVar(value="POLLING_ONLY")

        self.var_ofc_vars = {city: tk.BooleanVar(value=True) for city in CITY_OPTIONS}
        self.var_consular_vars = {city: tk.BooleanVar(value=True) for city in CITY_OPTIONS}
        
        self.var_ofc_start = tk.StringVar()
        self.var_ofc_end = tk.StringVar()
        self.var_cons_start = tk.StringVar()
        self.var_cons_end = tk.StringVar()
        
        self.var_sync_consular = tk.BooleanVar(value=True)
        self.var_prevent_immediate = tk.BooleanVar(value=False)
        self.var_multi_person = tk.BooleanVar(value=False)
        self.sq_rows = []

        self._build_form()
        self._refresh_listbox()

    def _add_row(self, parent, row, label, var, show=None):
        ttk.Label(parent, text=label, style="Subhead.TLabel").grid(row=row, column=0, sticky="e", padx=(0, 15), pady=8)
        ent = tk.Entry(parent, textvariable=var, bg=ENTRY_BG, fg=ENTRY_FG, font=("Segoe UI", 11), 
                       insertbackground=TEXT, borderwidth=0, highlightthickness=1, highlightbackground=BORDER)
        if show:
            ent.config(show=show)
        ent.grid(row=row, column=1, sticky="we", pady=8)
        parent.columnconfigure(1, weight=1)

    def _add_city_grid(self, parent, label_text, vars_dict):
        header = ttk.Frame(parent, style="Surface.TFrame")
        header.pack(fill=tk.X, pady=(15, 5))
        
        ttk.Label(header, text=label_text, font=("Segoe UI", 10, "bold"), foreground="#6366f1", style="Surface.TLabel").pack(side=tk.LEFT)
        lbl_all = tk.Label(header, text="Select All", font=("Segoe UI", 9, "bold"), fg="#6366f1", bg=SURFACE, cursor="hand2")
        lbl_all.pack(side=tk.RIGHT)
        def toggle_all(e):
            all_checked = all(v.get() for v in vars_dict.values())
            new_val = not all_checked
            for v in vars_dict.values():
                v.set(new_val)
            lbl_all.config(text="Select All" if all_checked else "Deselect All")

        def update_lbl(*args):
            all_checked = all(v.get() for v in vars_dict.values())
            lbl_all.config(text="Deselect All" if all_checked else "Select All")

        for var in vars_dict.values():
            var.trace_add("write", update_lbl)
            
        update_lbl()

        lbl_all.bind("<Button-1>", toggle_all)

        grid_frame = ttk.Frame(parent, style="Surface.TFrame")
        grid_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Grid layout for pills (e.g. 4 columns)
        for i, (city, var) in enumerate(vars_dict.items()):
            row = i // 4
            col = i % 4
            btn = ttk.Checkbutton(grid_frame, text=city, variable=var, style="Toolbutton", width=12)
            btn.grid(row=row, column=col, padx=5, pady=5)

    def _parse_date_value(self, value):
        if not value:
            return None
        raw = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def _format_date_iso(self, value):
        dt = self._parse_date_value(value)
        return dt.strftime("%Y-%m-%d") if dt else ""

    def _format_date_display(self, value):
        dt = self._parse_date_value(value)
        return dt.strftime("%d-%m-%Y") if dt else ""

    def _add_date_range(self, parent, start_var, end_var):
        frame = ttk.Frame(parent, style="Surface.TFrame")
        frame.pack(fill=tk.X, pady=(5, 15))
        
        start_frame = ttk.Frame(frame, style="Surface.TFrame")
        start_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        start_hdr_frame = ttk.Frame(start_frame, style="Surface.TFrame")
        start_hdr_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(start_hdr_frame, text="START DATE", font=("Segoe UI", 9, "bold"), foreground="#94a3b8", style="Surface.TLabel").pack(side=tk.LEFT, anchor="w")
        de_start = DateEntry(start_frame, textvariable=start_var, date_pattern='yyyy-mm-dd', 
                             style="DateEntry.TEntry",
                             background=ENTRY_BG, foreground=ENTRY_FG, headersbackground=SURFACE, 
                             headersforeground=TEXT, selectbackground=ACCENT_HOVER, selectforeground=TEXT,
                             borderwidth=0, font=("Segoe UI", 10))
        de_start.pack(fill=tk.X)

        end_frame = ttk.Frame(frame, style="Surface.TFrame")
        end_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        ttk.Label(end_frame, text="END DATE", font=("Segoe UI", 9, "bold"), foreground="#94a3b8", style="Surface.TLabel").pack(anchor="w", pady=(0, 5))
        de_end = DateEntry(end_frame, textvariable=end_var, date_pattern='yyyy-mm-dd', 
                           style="DateEntry.TEntry",
                           background=ENTRY_BG, foreground=ENTRY_FG, headersbackground=SURFACE, 
                           headersforeground=TEXT, selectbackground=ACCENT_HOVER, selectforeground=TEXT,
                           borderwidth=0, font=("Segoe UI", 10))
        de_end.pack(fill=tk.X)

        ttk.Label(frame, text="Stored in accounts.json as YYYY-MM-DD", font=("Segoe UI", 8), foreground="#94a3b8", style="Surface.TLabel").pack(fill=tk.X, pady=(5, 0))

    def _build_form(self):
        container = ttk.Frame(self.scrollable_frame, style="Surface.TFrame")
        container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Basic Info
        basic_frame = ttk.Frame(container, style="Surface.TFrame")
        basic_frame.pack(fill=tk.X, pady=(0, 10))
        self._add_row(basic_frame, 0, "Customer Name:", self.var_customer)
        self._add_row(basic_frame, 1, "Username/Email:", self.var_username)
        self._add_row(basic_frame, 2, "Password:", self.var_password, show="*")

        ttk.Separator(container).pack(fill=tk.X, pady=10)

        # ── Account Mode Selector ──────────────────────────────────────────────
        mode_frame = ttk.Frame(container, style="Surface.TFrame")
        mode_frame.pack(fill=tk.X, pady=(5, 10))
        ttk.Label(mode_frame, text="ACCOUNT MODE", font=("Segoe UI", 12, "bold"), foreground="#3b82f6", style="Surface.TLabel").pack(anchor="w")
        ttk.Separator(mode_frame).pack(fill=tk.X, pady=(5, 10))

        radio_frame = ttk.Frame(mode_frame, style="Surface.TFrame")
        radio_frame.pack(fill=tk.X)
        ttk.Radiobutton(radio_frame, text="Full Booking (OFC + Consular)",
                        variable=self.var_action_mode, value="SNIPER",
                        style="Toolbutton", command=self._on_mode_change).pack(side=tk.LEFT, padx=(0, 10), pady=5)
        ttk.Radiobutton(radio_frame, text="Full Reschedule (OFC + Consular)",
                        variable=self.var_action_mode, value="RESCHEDULE_FULL",
                        style="Toolbutton", command=self._on_mode_change).pack(side=tk.LEFT, padx=(0, 10), pady=5)
        ttk.Radiobutton(radio_frame, text="Consular Reschedule Only",
                        variable=self.var_action_mode, value="RESCHEDULE_CONSULAR",
                        style="Toolbutton", command=self._on_mode_change).pack(side=tk.LEFT, pady=5)

        ttk.Separator(container).pack(fill=tk.X, pady=10)

        # ── Account Role Selector ──────────────────────────────────────────────
        role_frame = ttk.Frame(container, style="Surface.TFrame")
        role_frame.pack(fill=tk.X, pady=(5, 10))
        ttk.Label(role_frame, text="ACCOUNT ROLE", font=("Segoe UI", 12, "bold"), foreground="#3b82f6", style="Surface.TLabel").pack(anchor="w")
        ttk.Separator(role_frame).pack(fill=tk.X, pady=(5, 10))

        role_radio_frame = ttk.Frame(role_frame, style="Surface.TFrame")
        role_radio_frame.pack(fill=tk.X)
        ttk.Radiobutton(role_radio_frame, text="Polling Only",
                        variable=self.var_account_role, value="POLLING_ONLY",
                        style="Toolbutton").pack(side=tk.LEFT, padx=(0, 10), pady=5)
        ttk.Radiobutton(role_radio_frame, text="Reserved for Booking",
                        variable=self.var_account_role, value="RESERVED_BOOKING",
                        style="Toolbutton").pack(side=tk.LEFT, pady=5)

        ttk.Separator(container).pack(fill=tk.X, pady=10)

        # OFC Section
        self.ofc_frame = ttk.Frame(container, style="Surface.TFrame")
        self.ofc_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(self.ofc_frame, text="OFC BIOMETRICS FOCUS", font=("Segoe UI", 12, "bold"), foreground="#3b82f6", style="Surface.TLabel").pack(anchor="w")
        ttk.Separator(self.ofc_frame).pack(fill=tk.X, pady=(5, 5))
        
        self._add_city_grid(self.ofc_frame, "TARGET CITIES", self.var_ofc_vars)
        self._add_date_range(self.ofc_frame, self.var_ofc_start, self.var_ofc_end)

        # Sync Checkbox
        self.sync_frame = ttk.Frame(container, style="Surface.TFrame")
        self.sync_frame.pack(fill=tk.X, pady=15)
        
        def on_sync_toggle(*args):
            if self.var_sync_consular.get():
                self.cons_frame.pack_forget()
            else:
                self.cons_frame.pack(fill=tk.X, before=self.options_frame)

        self.var_sync_consular.trace_add("write", on_sync_toggle)
        cb_sync = ttk.Checkbutton(self.sync_frame, text="Keep Consular Location & Dates identical to OFC", 
                                 variable=self.var_sync_consular, style="Toolbutton")
        cb_sync.pack(anchor="w", pady=5)

        # Consular Section
        self.cons_frame = ttk.Frame(container, style="Surface.TFrame")
        
        ttk.Label(self.cons_frame, text="CONSULAR INTERVIEW FOCUS", font=("Segoe UI", 12, "bold"), foreground="#3b82f6", style="Surface.TLabel").pack(anchor="w")
        ttk.Separator(self.cons_frame).pack(fill=tk.X, pady=(5, 5))
        
        self._add_city_grid(self.cons_frame, "TARGET CITIES", self.var_consular_vars)
        self._add_date_range(self.cons_frame, self.var_cons_start, self.var_cons_end)

        # Initialize toggle state (will be corrected by _on_mode_change)
        if not self.var_sync_consular.get():
            self.cons_frame.pack(fill=tk.X)

        # Global Options
        self.options_frame = ttk.Frame(container, style="Surface.TFrame")
        self.options_frame.pack(fill=tk.X, pady=(5, 0))
        
        cb_prevent = ttk.Checkbutton(self.options_frame, text="Prevent Immediate Booking (Dynamically skips slots within 3 days of today)", 
                                 variable=self.var_prevent_immediate, style="Toolbutton")
        cb_prevent.pack(anchor="w", pady=5)
        
        cb_multiperson = ttk.Checkbutton(self.options_frame, text="Multi-Person Booking (Book for all dependent family members)", 
                                 variable=self.var_multi_person, style="Toolbutton")
        cb_multiperson.pack(anchor="w", pady=5)

        # Security Questions
        self.sq_main_frame = ttk.Frame(container, style="Surface.TFrame")
        self.sq_main_frame.pack(fill=tk.X, pady=(15, 0))
        
        sq_header_frame = ttk.Frame(self.sq_main_frame, style="Surface.TFrame")
        sq_header_frame.pack(fill=tk.X, pady=(10, 5))
        ttk.Label(sq_header_frame, text="Security Questions", font=("Segoe UI", 12, "bold"), foreground=TEXT, style="Surface.TLabel").pack(side=tk.LEFT)
        ttk.Button(sq_header_frame, text="+ Add Question", style="Primary.TButton", command=self._add_sq_row).pack(side=tk.RIGHT)
        ttk.Separator(self.sq_main_frame).pack(fill=tk.X, pady=(0, 10))

        self.sq_container = ttk.Frame(self.sq_main_frame, style="Surface.TFrame")
        self.sq_container.pack(fill=tk.X)

        # Action Buttons
        action_frame = ttk.Frame(container, style="Surface.TFrame")
        action_frame.pack(fill=tk.X, pady=30)
        ttk.Button(action_frame, text="Delete", style="Danger.TButton", command=self._on_delete_account).pack(side=tk.LEFT)
        ttk.Button(action_frame, text="Save Changes", style="Success.TButton", command=self._on_save_account).pack(side=tk.RIGHT)

    def _clear_sq_rows(self):
        for child in self.sq_container.winfo_children():
            child.destroy()
        self.sq_rows.clear()

    def _add_sq_row(self, keyword="", answer=""):
        row_frame = ttk.Frame(self.sq_container, style="Surface.TFrame")
        row_frame.pack(fill=tk.X, pady=4)
        
        var_k = tk.StringVar(value=keyword)
        var_a = tk.StringVar(value=answer)
        
        ent_k = tk.Entry(row_frame, textvariable=var_k, width=15, bg=ENTRY_BG, fg=ENTRY_FG, font=("Segoe UI", 11), insertbackground=TEXT, borderwidth=0, highlightthickness=1, highlightbackground=BORDER)
        ent_k.pack(side=tk.LEFT, padx=(0, 10))
        
        ent_a = tk.Entry(row_frame, textvariable=var_a, bg=ENTRY_BG, fg=ENTRY_FG, font=("Segoe UI", 11), insertbackground=TEXT, borderwidth=0, highlightthickness=1, highlightbackground=BORDER)
        ent_a.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        btn_del = ttk.Button(row_frame, text="❌", style="Danger.TButton", width=3, command=lambda f=row_frame, vk=var_k, va=var_a: self._delete_sq_row(f, vk, va))
        btn_del.pack(side=tk.RIGHT)
        
        self.sq_rows.append((var_k, var_a))

    def _delete_sq_row(self, frame, var_k, var_a):
        frame.destroy()
        if (var_k, var_a) in self.sq_rows:
            self.sq_rows.remove((var_k, var_a))

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for acc in self.accounts:
            name = acc.get("customer_name", "Unknown")
            role = acc.get("role", "POLLING_ONLY")
            role_str = "Polling"
            if role == "RESERVED_BOOKING": role_str = "Booking"
            
            # Format date like "30th Sep"
            end_date_str = acc.get("ofcEndDate", "")
            if not end_date_str and acc.get("action_mode") == "RESCHEDULE_CONSULAR":
                end_date_str = acc.get("consularEndDate", "")
                
            friendly_date = ""
            if end_date_str:
                try:
                    dt = datetime.strptime(end_date_str, "%Y-%m-%d")
                    day = dt.day
                    suffix = 'th' if 11 <= day <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                    friendly_date = f" (Before {day}{suffix} {dt.strftime('%b')})"
                except:
                    pass

            display_text = f"{name} [{role_str}]{friendly_date}"
            self.listbox.insert(tk.END, display_text)
        
        self._update_active_bots_list()

    def _on_mode_change(self):
        """Show/hide OFC section and sync toggle based on the selected account mode."""
        mode = self.var_action_mode.get()
        
        # Safely hide all dynamic frames first
        self.ofc_frame.pack_forget()
        self.sync_frame.pack_forget()
        self.cons_frame.pack_forget()

        if mode == "RESCHEDULE_CONSULAR":
            # Only show consular
            self.cons_frame.pack(fill=tk.X, before=self.options_frame)
        else:  # SNIPER
            # Pack in correct top-to-bottom order before options_frame
            self.ofc_frame.pack(fill=tk.X, pady=(10, 0), before=self.options_frame)
            self.sync_frame.pack(fill=tk.X, pady=15, before=self.options_frame)
            
            # Consular visibility controlled by sync toggle
            if not self.var_sync_consular.get():
                self.cons_frame.pack(fill=tk.X, before=self.options_frame)

    def _on_account_select(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.current_account_idx = sel[0]
        acc = self.accounts[self.current_account_idx]

        self.var_customer.set(acc.get("customer_name", ""))
        self.var_username.set(acc.get("username", ""))
        self.var_password.set(acc.get("password", ""))
        self.var_action_mode.set(acc.get("action_mode", "SNIPER"))
        acc_role = acc.get("role", "POLLING_ONLY")
        if acc_role == "STANDARD": acc_role = "POLLING_ONLY"
        self.var_account_role.set(acc_role)

        ofc_cities = acc.get("ofcCities", [])
        for city, var in self.var_ofc_vars.items():
            var.set(city in ofc_cities)

        consular_cities = acc.get("consularCities", [])
        for city, var in self.var_consular_vars.items():
            var.set(city in consular_cities)

        ofc_start = acc.get("ofcStartDate", "")
        ofc_end = acc.get("ofcEndDate", "")
        cons_start = acc.get("consularStartDate", "")
        cons_end = acc.get("consularEndDate", "")

        self.var_ofc_start.set(self._format_date_display(ofc_start))
        self.var_ofc_end.set(self._format_date_display(ofc_end))
        self.var_cons_start.set(self._format_date_display(cons_start or ofc_start))
        self.var_cons_end.set(self._format_date_display(cons_end or ofc_end))

        # Check if lists and dates match to set the sync toggle
        if (sorted(ofc_cities) == sorted(consular_cities) and 
            ofc_start == cons_start and 
            ofc_end == cons_end):
            self.var_sync_consular.set(True)
        else:
            self.var_sync_consular.set(False)

        self.var_prevent_immediate.set(acc.get("prevent_immediate", False))
        self.var_multi_person.set(acc.get("multiPerson", False))

        self._on_mode_change()

        self._clear_sq_rows()
        sq = acc.get("security_questions", {})
        for k, v in sq.items():
            self._add_sq_row(k, v)
            
        while len(self.sq_rows) < 3:
            self._add_sq_row()

    def _on_add_account(self):
        self.current_account_idx = None
        self.listbox.selection_clear(0, tk.END)
        self.var_customer.set("")
        self.var_username.set("")
        self.var_password.set("")
        self.var_action_mode.set("SNIPER")
        self.var_account_role.set("POLLING_ONLY")
        for var in self.var_ofc_vars.values(): var.set(True)
        for var in self.var_consular_vars.values(): var.set(True)
        self.var_ofc_start.set("2026-01-01")
        self.var_ofc_end.set("2026-12-31")
        self.var_cons_start.set("2026-01-01")
        self.var_cons_end.set("2026-12-31")
        self.var_sync_consular.set(True)
        self.var_prevent_immediate.set(False)
        self.var_multi_person.set(False)
        self._on_mode_change()
        self._clear_sq_rows()
        for _ in range(3): self._add_sq_row()

    def _on_save_account(self):
        customer = self.var_customer.get().strip()
        if not customer:
            messagebox.showwarning("Warning", "Customer name cannot be empty.")
            return

        sq_dict = {}
        for var_k, var_a in self.sq_rows:
            k = var_k.get().strip()
            a = var_a.get().strip()
            if k: sq_dict[k] = a

        action_mode = self.var_action_mode.get()

        if action_mode == "RESCHEDULE_CONSULAR":
            # Only consular fields are relevant
            ofc_cities = []
            ofc_start = ""
            ofc_end = ""
            consular_cities = [city for city, var in self.var_consular_vars.items() if var.get()]
            consular_start = self._format_date_iso(self.var_cons_start.get().strip())
            consular_end = self._format_date_iso(self.var_cons_end.get().strip())
        else:
            ofc_cities = [city for city, var in self.var_ofc_vars.items() if var.get()]
            ofc_start = self._format_date_iso(self.var_ofc_start.get().strip())
            ofc_end = self._format_date_iso(self.var_ofc_end.get().strip())
            if self.var_sync_consular.get():
                consular_cities = ofc_cities
                consular_start = ofc_start
                consular_end = ofc_end
            else:
                consular_cities = [city for city, var in self.var_consular_vars.items() if var.get()]
                consular_start = self._format_date_iso(self.var_cons_start.get().strip())
                consular_end = self._format_date_iso(self.var_cons_end.get().strip())

        acc_data = {
            "customer_name": customer,
            "username": self.var_username.get().strip(),
            "password": self.var_password.get().strip(),
            "action_mode": action_mode,
            "role": self.var_account_role.get(),
            "ofcCities": ofc_cities,
            "ofcStartDate": ofc_start,
            "ofcEndDate": ofc_end,
            "consularCities": consular_cities,
            "consularStartDate": consular_start,
            "consularEndDate": consular_end,
            "security_questions": sq_dict,
            "prevent_immediate": self.var_prevent_immediate.get(),
            "multiPerson": self.var_multi_person.get()
        }

        if self.current_account_idx is not None:
            self.accounts[self.current_account_idx] = acc_data
        else:
            self.accounts.append(acc_data)
            self.current_account_idx = len(self.accounts) - 1

        if self._save_accounts():
            self._refresh_listbox()
            self.listbox.selection_set(self.current_account_idx)
            messagebox.showinfo("Success", "Account saved successfully.")

    def _on_delete_account(self):
        if self.current_account_idx is None: return
        if messagebox.askyesno("Confirm Delete", "Are you sure you want to delete this account?"):
            del self.accounts[self.current_account_idx]
            if self._save_accounts():
                self._refresh_listbox()
                self._on_add_account()
                self.update_idletasks()

    # ─── Orchestrator Tab ────────────────────────────────────────────────────
    # Keep the same as before
    def _build_orchestrator_tab(self):
        top_frame = ttk.Frame(self.tab_orchestrator, style="Surface.TFrame")
        top_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top_frame, text="Orchestrator Control", style="Header.TLabel").pack(side=tk.LEFT, padx=15, pady=15)

        self.btn_start = ttk.Button(top_frame, text="▶ Start All Bots", style="Success.TButton", command=self._start_orchestrator)
        self.btn_start.pack(side=tk.RIGHT, padx=15)

        self.btn_stop = ttk.Button(top_frame, text="⏹ Stop All", style="Danger.TButton", command=self._stop_orchestrator)
        self.btn_stop.pack(side=tk.RIGHT, padx=5)
        self.btn_stop.state(["disabled"])

        monitor_frame = ttk.Frame(self.tab_orchestrator, style="Surface.TFrame")
        monitor_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.var_run_monitor = tk.BooleanVar(value=True)
        self.chk_monitor = ttk.Checkbutton(monitor_frame, text=" Run Slot Monitor ", variable=self.var_run_monitor, style="Toolbutton")
        self.chk_monitor.pack(side=tk.LEFT, padx=15, pady=10)

        self.var_enable_logging = tk.BooleanVar(value=True)
        self.chk_logging = ttk.Checkbutton(monitor_frame, text=" Enable MongoDB Logging ", variable=self.var_enable_logging, style="Toolbutton", command=self._on_toggle_logging)
        self.chk_logging.pack(side=tk.LEFT, padx=5, pady=10)

        self.bots_frame = ttk.Frame(self.tab_orchestrator, style="Surface.TFrame")
        self.bots_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(self.bots_frame, text="Active Accounts:", style="Subhead.TLabel").pack(side=tk.TOP, anchor=tk.W, padx=15, pady=5)
        
        self.bots_canvas = tk.Canvas(self.bots_frame, bg=SURFACE, highlightthickness=0, height=200)
        self.bots_scrollbar = ttk.Scrollbar(self.bots_frame, orient="vertical", command=self.bots_canvas.yview)
        self.bots_inner_frame = ttk.Frame(self.bots_canvas, style="Surface.TFrame")

        self.bots_inner_frame.bind(
            "<Configure>",
            lambda e: self.bots_canvas.configure(
                scrollregion=self.bots_canvas.bbox("all")
            )
        )

        self.bots_canvas_window = self.bots_canvas.create_window((0, 0), window=self.bots_inner_frame, anchor="nw")
        
        def _on_bots_canvas_configure(event):
            self.bots_canvas.itemconfig(self.bots_canvas_window, width=event.width)
            
        self.bots_canvas.bind("<Configure>", _on_bots_canvas_configure)
        self.bots_canvas.configure(yscrollcommand=self.bots_scrollbar.set)

        self.bots_canvas.pack(side="left", fill="both", expand=True, padx=(15, 0), pady=5)
        self.bots_scrollbar.pack(side="right", fill="y", padx=(0, 15), pady=5)

        def _on_mousewheel_bots(event):
            self.bots_canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        self.bots_canvas.bind("<Enter>", lambda e: self.bots_canvas.bind_all("<MouseWheel>", _on_mousewheel_bots))
        self.bots_canvas.bind("<Leave>", lambda e: self.bots_canvas.unbind_all("<MouseWheel>"))

        log_frame = ttk.Frame(self.tab_orchestrator, style="Surface.TFrame")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 10))

        header_frame = ttk.Frame(log_frame, style="Surface.TFrame")
        header_frame.pack(fill=tk.X, padx=15, pady=(10, 5))
        
        ttk.Label(header_frame, text="Live Output", style="Subhead.TLabel").pack(side=tk.LEFT)
        
        self.var_autoscroll = tk.BooleanVar(value=False)
        ttk.Checkbutton(header_frame, text=" Auto-scroll ", variable=self.var_autoscroll, style="Toolbutton").pack(side=tk.RIGHT)

        self.txt_log = scrolledtext.ScrolledText(log_frame, bg="#0f172a", fg="#10b981", font=("Consolas", 10),
                                                 borderwidth=0, highlightthickness=0)
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 15))
        self.txt_log.config(state=tk.DISABLED)
        
        self._update_active_bots_list()

    def _on_toggle_logging(self):
        try:
            from src.common.db_logger import MongoDBLogger
            logger = MongoDBLogger()
            logger.toggle_logging(self.var_enable_logging.get())
        except Exception as e:
            self._log(f"Failed to toggle logging: {e}")

    def _log(self, text):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.insert(tk.END, text + "\n")
        # Keep only the last 5000 lines to prevent UI freezing
        try:
            line_count = int(self.txt_log.index('end-1c').split('.')[0])
            if line_count > 5000:
                self.txt_log.delete('1.0', f'{line_count - 5000}.0')
        except Exception:
            pass
        if getattr(self, "var_autoscroll", None) and self.var_autoscroll.get():
            self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)
        self._write_log_to_file(text)

    def _write_log_to_file(self, text):
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        filename = "orchestrator.log"

        try:
            with open(log_dir / filename, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _start_orchestrator(self):
        if self.orchestrator_proc is not None:
            return

        if not self.accounts:
            messagebox.showwarning("Warning", "No accounts configured. Please add an account first.")
            self.notebook.select(self.tab_accounts)
            return

        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self.chk_monitor.state(["disabled"])
        self.closed_bots.clear()
        
        # Clear logs directory on restart
        log_dir = BASE_DIR / "logs"
        if log_dir.exists():
            try:
                shutil.rmtree(log_dir)
            except Exception:
                pass
        log_dir.mkdir(exist_ok=True)
        

        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete(1.0, tk.END)
        self.txt_log.config(state=tk.DISABLED)

        self._log("[GUI] Starting orchestrator.py ...")

        cmd = [sys.executable, str(ORCHESTRATOR_SCRIPT)]
        
        if not self.var_run_monitor.get():
            cmd.append("--no-monitor")
        
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["FROM_GUI"] = "1"
        
        # Ensure absolute imports work (e.g. from src.auth...)
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = str(BASE_DIR)

        self.orchestrator_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE, text=True, encoding="utf-8", 
            bufsize=1, cwd=str(BASE_DIR), env=env
        )

        # Update bots list now that orchestrator_proc is set
        self._update_active_bots_list()

        threading.Thread(target=self._read_orchestrator_output, daemon=True).start()

    def _read_orchestrator_output(self):
        try:
            for line in iter(self.orchestrator_proc.stdout.readline, ''):
                if line:
                    text = line.rstrip()
                    if "[POLLING-RESULT]" in text:
                        clean_text = text.split("[POLLING-RESULT]")[-1].strip()
                        self.after(0, self._log_poll, f"[Background Poll] {clean_text}")
                    else:
                        self.after(0, self._log, text)
        except Exception:
            pass
        self.after(0, self._on_orchestrator_exit)

    def _stop_orchestrator(self):
        if self.orchestrator_proc:
            self._log("[GUI] Sending termination signal to orchestrator and all child processes...")
            self.btn_stop.state(["disabled"])
            try:
                import subprocess
                # /T kills the process tree (including all spawned bots and Chrome windows)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.orchestrator_proc.pid)],
                    capture_output=True
                )
            except Exception as e:
                self._log(f"[GUI] Error during process tree termination: {e}")
        self._update_active_bots_list()

    def _on_orchestrator_exit(self):
        self.orchestrator_proc = None
        self.btn_start.state(["!disabled"])
        self.btn_stop.state(["disabled"])
        self.chk_monitor.state(["!disabled"])
        self._log("[GUI] Orchestrator has stopped.")
        self._update_active_bots_list()

    def _update_active_bots_list(self):
        if not hasattr(self, 'bots_inner_frame'): return
        
        for widget in self.bots_inner_frame.winfo_children():
            widget.destroy()

        if not self.orchestrator_proc:
            ttk.Label(self.bots_inner_frame, text="No bots are running.", foreground="#94a3b8").pack(pady=5, anchor=tk.W)
            return

        active_accounts = [a for a in self.accounts if a.get("username")]
        for acc in active_accounts:
            uname = acc.get("username")
            cname = acc.get("customer_name") or uname
            row = ttk.Frame(self.bots_inner_frame)
            row.pack(fill=tk.X, pady=2)
            
            ttk.Label(row, text=f"🤖 {cname}", font=("Segoe UI", 10, "bold"), width=25).pack(side=tk.LEFT, padx=(0, 10))
            
            if uname in self.closed_bots:
                ttk.Label(row, text="(Stopped)", foreground="#94a3b8").pack(side=tk.LEFT, padx=10)
                btn_start_bot = ttk.Button(row, text="▶ Start Bot", style="Success.TButton",
                                       command=lambda u=uname: self._on_start_bot(u))
                btn_start_bot.pack(side=tk.RIGHT, padx=5)
            else:
                action_mode = acc.get("action_mode", "SNIPER")
                if action_mode in ("SNIPER", "RESCHEDULE_FULL"):
                    btn_text = "⚡ Manual Book" if action_mode == "SNIPER" else "⚡ Manual Reschedule"
                    btn_book = ttk.Button(row, text=btn_text, style="Primary.TButton",
                                          command=lambda u=uname, a=action_mode: self._on_manual_book(u, a))
                    btn_book.pack(side=tk.LEFT, padx=5)
                elif action_mode == "RESCHEDULE_CONSULAR":
                    btn_reschedule = ttk.Button(row, text="📅 Consular Reschedule", style="Primary.TButton",
                                                command=lambda u=uname: self._on_consular_reschedule(u))
                    btn_reschedule.pack(side=tk.LEFT, padx=5)

                btn_close = ttk.Button(row, text="🛑 Close Bot", style="Danger.TButton",
                                       command=lambda u=uname: self._on_close_bot(u))
                btn_close.pack(side=tk.LEFT, padx=5)

    def _get_account_info(self, username):
        if not username: return None, None, None
        acc = next((a for a in self.accounts if a.get("username") == username), None)
        if not acc: return None, None, None
        uid = safe_id(acc.get("username", ""))
        customer = acc.get("customer_name") or uid
        return acc, uid, customer

    def _trigger_booking_action(self, username, action_type):
        acc, uid, customer = self._get_account_info(username)
        if not acc: return
        
        state_path = BASE_DIR / "src" / f"state_{uid}.json"
        try:
            existing = {}
            if state_path.exists():
                try:
                    existing = json.loads(state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            if existing.get("extension_running"):
                force = messagebox.askyesno(
                    "Already Running?",
                    f"The booking runner for '{customer}' reports it is already executing.\n\n"
                    f"This may be stale. Do you want to force a {action_type} anyway?"
                )
                if not force:
                    return

            updates = {
                "extension_running": False,
                "pending": True,
                "trigger_timestamp": time.time(),
                "action_type": action_type,
                "customer_name": customer,
                "prevent_immediate": acc.get("prevent_immediate", False),
                "multiPerson": acc.get("multiPerson", False),
                "completed": False,
            }
            
            # Action specific data
            if action_type in ("SNIPER", "RESCHEDULE_FULL"):
                updates.update({
                    "ofcCities": acc.get("ofcCities", []),
                    "ofcStartDate": acc.get("ofcStartDate", ""),
                    "ofcEndDate": acc.get("ofcEndDate", ""),
                    "consularCities": acc.get("consularCities", []),
                    "consularStartDate": acc.get("consularStartDate", ""),
                    "consularEndDate": acc.get("consularEndDate", ""),
                })
            else:
                updates.update({
                    "consularCities": acc.get("consularCities", []),
                    "consularStartDate": acc.get("consularStartDate", ""),
                    "consularEndDate": acc.get("consularEndDate", ""),
                })
                
            update_state(state_path, updates)
            self._log(f"[GUI] ⚡ {action_type} trigger queued for '{customer}'.")
            messagebox.showinfo("Triggered", f"{action_type} queued for '{customer}'.\nBooking runner will pick it up within 0.5s.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to write state file: {e}")

    def _on_close_bot(self, username):
        acc, uid, customer = self._get_account_info(username)
        if not acc: return

        if self.orchestrator_proc and self.orchestrator_proc.stdin:
            try:
                self.orchestrator_proc.stdin.write(f"STOP:{uid}\n")
                self.orchestrator_proc.stdin.flush()
                self._log(f"[GUI] 🛑 Stop signal sent for '{customer}'.")
            except Exception as e:
                self._log(f"[GUI] Warning: could not send stop signal for '{customer}': {e}")

        self.closed_bots.add(username)
        self._update_active_bots_list()

    def _on_start_bot(self, username):
        acc, uid, customer = self._get_account_info(username)
        if not acc: return

        state_path = BASE_DIR / "src" / f"state_{uid}.json"
        try:
            update_state(state_path, {"completed": False})
        except Exception:
            pass

        if self.orchestrator_proc and self.orchestrator_proc.stdin:
            try:
                self.orchestrator_proc.stdin.write(f"START:{uid}\n")
                self.orchestrator_proc.stdin.flush()
                self._log(f"[GUI] ▶️ Start signal sent for '{customer}'.")
            except Exception as e:
                self._log(f"[GUI] Warning: could not send start signal for '{customer}': {e}")

        if username in self.closed_bots:
            self.closed_bots.remove(username)
        self._update_active_bots_list()

    def _on_manual_book(self, username, action_type="SNIPER"):
        self._trigger_booking_action(username, action_type)

    def _on_consular_reschedule(self, username):
        self._trigger_booking_action(username, "RESCHEDULE_CONSULAR")

    def _build_polling_tab(self):
        top_frame = ttk.Frame(self.tab_polling, style="Surface.TFrame")
        top_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top_frame, text="Polling Control", style="Header.TLabel").pack(side=tk.LEFT, padx=15, pady=15)

        self.btn_start_poll = ttk.Button(top_frame, text="▶ Start Polling", style="Success.TButton", command=self._start_polling)
        self.btn_start_poll.pack(side=tk.RIGHT, padx=15)

        self.btn_stop_poll = ttk.Button(top_frame, text="⏹ Stop Polling", style="Danger.TButton", command=self._stop_polling)
        self.btn_stop_poll.pack(side=tk.RIGHT, padx=5)
        self.btn_stop_poll.state(["disabled"])

        settings_frame = ttk.Frame(self.tab_polling, style="Surface.TFrame")
        settings_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(settings_frame, text="⏱ Cooldown (sec):", style="Surface.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(15, 5), pady=10)
        self.var_cooldown = tk.StringVar(value="600")
        tk.Entry(settings_frame, textvariable=self.var_cooldown, width=7,
                 font=("Consolas", 11), bg=ENTRY_BG, fg=ENTRY_FG,
                 insertbackground=ENTRY_FG, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1).pack(side=tk.LEFT, padx=5)

        ttk.Label(settings_frame, text="🔄 Gap (sec):", style="Surface.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(20, 5), pady=10)
        self.var_gap = tk.StringVar(value="60")
        tk.Entry(settings_frame, textvariable=self.var_gap, width=7,
                 font=("Consolas", 11), bg=ENTRY_BG, fg=ENTRY_FG,
                 insertbackground=ENTRY_FG, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1).pack(side=tk.LEFT, padx=5)

        ttk.Label(settings_frame, text="⚡ Instant:", style="Surface.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(20, 5), pady=10)
        self.var_instant_booking = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings_frame, variable=self.var_instant_booking, style="Surface.TCheckbutton").pack(side=tk.LEFT, padx=5)

        ttk.Label(settings_frame, text="💤 Rest (hr):", style="Surface.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(20, 5), pady=10)
        self.var_rest_hours = tk.StringVar(value="1.0")
        tk.Entry(settings_frame, textvariable=self.var_rest_hours, width=5,
                 font=("Consolas", 11), bg=ENTRY_BG, fg=ENTRY_FG,
                 insertbackground=ENTRY_FG, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1).pack(side=tk.LEFT, padx=5)

        log_frame = ttk.Frame(self.tab_polling, style="Surface.TFrame")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 10))

        ttk.Label(log_frame, text="Polling Logs", style="Subhead.TLabel").pack(side=tk.TOP, anchor=tk.W, padx=15, pady=(10, 5))

        self.txt_poll_log = scrolledtext.ScrolledText(log_frame, bg="#0f172a", fg="#3b82f6", font=("Consolas", 10), borderwidth=0, highlightthickness=0)
        self.txt_poll_log.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 15))
        self.txt_poll_log.config(state=tk.DISABLED)

    def _log_poll(self, text):
        self.txt_poll_log.config(state=tk.NORMAL)
        self.txt_poll_log.insert(tk.END, text + "\n")
        try:
            line_count = int(self.txt_poll_log.index('end-1c').split('.')[0])
            if line_count > 5000:
                self.txt_poll_log.delete('1.0', f'{line_count - 5000}.0')
        except Exception:
            pass
        self.txt_poll_log.see(tk.END)
        self.txt_poll_log.config(state=tk.DISABLED)
        self._write_log_to_file(text)

    def _start_polling(self):
        self.btn_start_poll.state(["disabled"])
        self.btn_stop_poll.state(["!disabled"])
        
        state_path = BASE_DIR / "src" / "polling_state.json"
        
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "is_active": True,
                    "cooldown": int(self.var_cooldown.get()),
                    "gap": int(self.var_gap.get()),
                    "instant_booking": self.var_instant_booking.get(),
                    "rest_hours": float(self.var_rest_hours.get())
                }, f)
            self._log_poll(f"[GUI] ✅ Polling activated. Cooldown={self.var_cooldown.get()}s, Gap={self.var_gap.get()}s, Instant Booking={'ON' if self.var_instant_booking.get() else 'OFF'}, Rest={self.var_rest_hours.get()}h. Waiting for first poll cycle...")
        except Exception as e:
            self._log_poll(f"[GUI] ❌ Error activating polling: {e}")

    def _stop_polling(self):
        self.btn_stop_poll.state(["disabled"])
        self.btn_start_poll.state(["!disabled"])
        
        state_path = BASE_DIR / "src" / "polling_state.json"
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"is_active": False}, f)
            self._log_poll("[GUI] 🛑 Polling deactivated. In-session polling stopped.")
        except Exception as e:
            self._log_poll(f"[GUI] ❌ Error stopping polling: {e}")

    def _build_settings_tab(self):
        top_frame = ttk.Frame(self.tab_settings, style="Surface.TFrame")
        top_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top_frame, text="System Settings", style="Header.TLabel").pack(side=tk.LEFT, padx=15, pady=15)
        ttk.Button(top_frame, text="💾 Save Settings", style="Success.TButton", command=self._on_save_settings).pack(side=tk.RIGHT, padx=15)

        env_frame = ttk.Frame(self.tab_settings, style="Surface.TFrame")
        env_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        ttk.Label(env_frame, text="Remote Architecture (Tailscale)", font=("Segoe UI", 12, "bold"), foreground="#3b82f6", style="Surface.TLabel").pack(anchor="w", padx=15, pady=(15,5))
        ttk.Separator(env_frame).pack(fill=tk.X, padx=15, pady=5)
        
        ttk.Label(env_frame, text="Laptop Role:", style="Subhead.TLabel").pack(anchor="w", padx=20, pady=(10,0))
        self.var_laptop_role = tk.StringVar(value="BOOKING")
        roles_cb = ttk.Combobox(env_frame, textvariable=self.var_laptop_role, values=["POLLING", "BOOKING", "ALL_IN_ONE"], state="readonly", font=("Segoe UI", 11))
        roles_cb.pack(fill=tk.X, padx=20, pady=(5,10))
        ttk.Label(env_frame, text="POLLING: Runs scout accounts.\nBOOKING: Runs VIP accounts.\nALL_IN_ONE: Runs everything on one laptop.", font=("Segoe UI", 9), foreground="#94a3b8", style="Surface.TLabel").pack(anchor="w", padx=20)
        
        ttk.Label(env_frame, text="REMOTE_TRIGGER_URL:", style="Subhead.TLabel").pack(anchor="w", padx=20, pady=(10,0))
        
        self.var_remote_url = tk.StringVar()
        ent_remote = tk.Entry(env_frame, textvariable=self.var_remote_url, font=("Consolas", 11), bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ENTRY_FG, relief="flat", highlightbackground=BORDER, highlightthickness=1)
        ent_remote.pack(fill=tk.X, padx=20, pady=(5,15))
        
        ttk.Label(env_frame, text="Example: http://100.x.x.x:8000/trigger\nSet this on the Polling laptop to point to the Booking laptop's Tailscale IP.", font=("Segoe UI", 9), foreground="#94a3b8", style="Surface.TLabel").pack(anchor="w", padx=20)
        
        self._load_settings()

    def _load_settings(self):
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("REMOTE_TRIGGER_URL="):
                        self.var_remote_url.set(line.strip().split("=", 1)[1])
                    elif line.startswith("LAPTOP_ROLE="):
                        self.var_laptop_role.set(line.strip().split("=", 1)[1])

    def _on_save_settings(self):
        env_path = BASE_DIR / ".env"
        lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
        new_url = self.var_remote_url.get().strip()
        new_role = self.var_laptop_role.get().strip()
        
        found_url = False
        found_role = False
        for i, line in enumerate(lines):
            if line.startswith("REMOTE_TRIGGER_URL="):
                lines[i] = f"REMOTE_TRIGGER_URL={new_url}\n"
                found_url = True
            elif line.startswith("LAPTOP_ROLE="):
                lines[i] = f"LAPTOP_ROLE={new_role}\n"
                found_role = True
                
        if not found_url:
            lines.append(f"REMOTE_TRIGGER_URL={new_url}\n")
        if not found_role:
            lines.append(f"LAPTOP_ROLE={new_role}\n")
            
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
            
        from tkinter import messagebox
        messagebox.showinfo("Success", "Settings saved successfully!\nPlease restart your bots for changes to apply.")

    def destroy(self):
        if self.orchestrator_proc:
            try:
                import subprocess
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.orchestrator_proc.pid)], capture_output=True)
            except:
                pass
        super().destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
