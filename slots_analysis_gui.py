import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os

BG = "#1e1e2e"
SURFACE = "#2a2a3e"
ACCENT = "#7c3aed"
TEXT = "#e2e8f0"
SUBTEXT = "#94a3b8"

SLOTS_ANALYSIS_FILE = "slots_data_analysis.csv"

class SlotsAnalyzerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Slots Data Analyzer")
        self.geometry("900x600")
        self.configure(bg=BG)
        
        self.all_data = []
        
        self.build_ui()
        self.load_data()

    def build_ui(self):
        # Header
        header = tk.Frame(self, bg=SURFACE, pady=15, padx=20)
        header.pack(fill="x")
        tk.Label(header, text="Slots Data Analyzer", font=("Segoe UI", 16, "bold"), fg=TEXT, bg=SURFACE).pack(side="left")
        
        tk.Button(header, text="🔄 Reload CSV", command=self.load_data, bg=ACCENT, fg="white", 
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=10, cursor="hand2").pack(side="right")

        # Filters
        filter_frame = tk.Frame(self, bg=BG, pady=10, padx=20)
        filter_frame.pack(fill="x")
        
        tk.Label(filter_frame, text="Filter by City:", font=("Segoe UI", 10), fg=TEXT, bg=BG).pack(side="left")
        
        self.city_filter = ttk.Combobox(filter_frame, state="readonly", width=15)
        self.city_filter.pack(side="left", padx=10)
        self.city_filter.bind("<<ComboboxSelected>>", self.apply_filters)
        
        tk.Label(filter_frame, text="Filter by Type:", font=("Segoe UI", 10), fg=TEXT, bg=BG).pack(side="left")
        
        self.type_filter = ttk.Combobox(filter_frame, state="readonly", width=15, values=["ALL", "OFC", "Consular"])
        self.type_filter.set("ALL")
        self.type_filter.pack(side="left", padx=10)
        self.type_filter.bind("<<ComboboxSelected>>", self.apply_filters)
        
        self.status_lbl = tk.Label(filter_frame, text="", font=("Segoe UI", 9), fg=SUBTEXT, bg=BG)
        self.status_lbl.pack(side="right")

        # Table
        table_frame = tk.Frame(self, bg=BG, padx=20, pady=10)
        table_frame.pack(fill="both", expand=True)
        
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background=SURFACE, foreground=TEXT, fieldbackground=SURFACE, rowheight=25)
        style.configure("Treeview.Heading", background=ACCENT, foreground="white", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#6d28d9")])

        cols = ("Timestamp", "Location", "Type", "Slot Date", "Available Slots")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", style="Treeview")
        
        for col in cols:
            self.tree.heading(col, text=col, command=lambda c=col: self.sort_column(c, False))
            self.tree.column(col, width=150, anchor="center")
            
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

    def load_data(self):
        self.all_data = []
        if not os.path.exists(SLOTS_ANALYSIS_FILE):
            self.status_lbl.config(text=f"File not found: {SLOTS_ANALYSIS_FILE}")
            self.populate_table([])
            return
            
        try:
            with open(SLOTS_ANALYSIS_FILE, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.all_data.append(row)
            
            # Extract unique cities for the dropdown
            cities = sorted(list(set(r.get("visa_location", "") for r in self.all_data if r.get("visa_location"))))
            self.city_filter.config(values=["ALL"] + cities)
            if not self.city_filter.get():
                self.city_filter.set("ALL")
                
            self.apply_filters()
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read CSV: {e}")

    def apply_filters(self, event=None):
        city = self.city_filter.get()
        vtype = self.type_filter.get()
        
        filtered = self.all_data
        
        if city and city != "ALL":
            filtered = [r for r in filtered if r.get("visa_location") == city]
            
        if vtype and vtype != "ALL":
            filtered = [r for r in filtered if r.get("appointment_type") == vtype]
            
        # Sort by timestamp descending by default
        filtered.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        self.populate_table(filtered)
        self.status_lbl.config(text=f"Showing {len(filtered)} records")

    def populate_table(self, data):
        self.tree.delete(*self.tree.get_children())
        for row in data:
            self.tree.insert("", "end", values=(
                row.get("timestamp", ""),
                row.get("visa_location", ""),
                row.get("appointment_type", ""),
                row.get("start_date", ""),
                row.get("slots", "")
            ))

    def sort_column(self, col, reverse):
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        
        # Try to sort numerically if it's the slots column
        if col == "Available Slots":
            data.sort(key=lambda t: int(t[0]) if t[0].isdigit() else 0, reverse=reverse)
        else:
            data.sort(reverse=reverse)
            
        for index, (val, k) in enumerate(data):
            self.tree.move(k, "", index)
            
        self.tree.heading(col, command=lambda: self.sort_column(col, not reverse))

if __name__ == "__main__":
    app = SlotsAnalyzerApp()
    app.mainloop()
