import customtkinter as ctk
import pyperclip
import json
import os
import datetime
import qrcode
from tkinter import messagebox, filedialog

from license_generator import generate_license

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

DB_FILE = "licenses_db.json"


def load_db():
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class LicenseGUI(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Arabic Data Cleaner — License Manager")
        self.geometry("900x650")

        self.db = load_db()

        title = ctk.CTkLabel(
            self,
            text="Arabic Data Cleaner\nLicense Manager",
            font=("Arial", 24, "bold")
        )
        title.pack(pady=20)

        self.build_form()
        self.build_result()
        self.build_stats()
        self.build_history()

    # ─────────────────────────────
    # FORM
    # ─────────────────────────────

    def build_form(self):

        frame = ctk.CTkFrame(self)
        frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(frame, text="Machine ID").grid(row=0, column=0, padx=10, pady=10)

        self.machine = ctk.CTkEntry(frame, width=300)
        self.machine.grid(row=0, column=1, padx=10)

        paste_btn = ctk.CTkButton(frame, text="Paste", width=80, command=self.paste_mid)
        paste_btn.grid(row=0, column=2)

        ctk.CTkLabel(frame, text="Customer").grid(row=1, column=0, padx=10)

        self.customer = ctk.CTkEntry(frame, width=300)
        self.customer.grid(row=1, column=1, padx=10)

        ctk.CTkLabel(frame, text="License Type").grid(row=2, column=0)

        self.lic_type = ctk.CTkOptionMenu(
            frame,
            values=["permanent", "yearly", "trial_30", "trial_7"]
        )
        self.lic_type.grid(row=2, column=1, padx=10)

        gen_btn = ctk.CTkButton(frame, text="Generate License", command=self.generate)
        gen_btn.grid(row=3, column=1, pady=15)

    def paste_mid(self):
        try:
            self.machine.delete(0, "end")
            self.machine.insert(0, pyperclip.paste())
        except:
            pass

    # ─────────────────────────────
    # RESULT
    # ─────────────────────────────

    def build_result(self):

        frame = ctk.CTkFrame(self)
        frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(frame, text="License Code").pack(anchor="w", padx=10)

        self.result = ctk.CTkTextbox(frame, height=90)
        self.result.pack(fill="x", padx=10, pady=5)

        btn_frame = ctk.CTkFrame(frame)
        btn_frame.pack(fill="x")

        ctk.CTkButton(btn_frame, text="Copy", command=self.copy).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Save File", command=self.save_file).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Generate QR", command=self.qr_code).pack(side="left", padx=10)

    def copy(self):
        code = self.result.get("1.0", "end").strip()
        if code:
            pyperclip.copy(code)
            messagebox.showinfo("Copied", "License copied")

    def save_file(self):

        code = self.result.get("1.0", "end").strip()
        if not code:
            return

        fname = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text File", "*.txt")]
        )

        if not fname:
            return

        with open(fname, "w", encoding="utf-8") as f:
            f.write(code)

        messagebox.showinfo("Saved", "License saved")

    def qr_code(self):

        code = self.result.get("1.0", "end").strip()
        if not code:
            return

        img = qrcode.make(code)
        path = "license_qr.png"
        img.save(path)

        messagebox.showinfo("QR", f"QR saved:\n{path}")

    # ─────────────────────────────
    # GENERATE
    # ─────────────────────────────

    def generate(self):

        mid = self.machine.get().strip()
        cust = self.customer.get().strip()
        typ = self.lic_type.get()

        if not mid:
            messagebox.showerror("Error", "Machine ID required")
            return

        code = generate_license(mid, typ, cust)

        self.result.delete("1.0", "end")
        self.result.insert("1.0", code)

        entry = {
            "machine_id": mid,
            "customer": cust,
            "type": typ,
            "date": str(datetime.date.today()),
            "code": code
        }

        self.db.append(entry)
        save_db(self.db)

        self.update_stats()
        self.update_history()

    # ─────────────────────────────
    # STATS
    # ─────────────────────────────

    def build_stats(self):

        self.stats = ctk.CTkLabel(self, text="")
        self.stats.pack(pady=10)

        self.update_stats()

    def update_stats(self):

        total = len(self.db)
        yearly = len([x for x in self.db if x["type"] == "yearly"])
        trial = len([x for x in self.db if "trial" in x["type"]])
        perm = len([x for x in self.db if x["type"] == "permanent"])

        txt = f"Total Licenses: {total} | Permanent: {perm} | Yearly: {yearly} | Trial: {trial}"

        self.stats.configure(text=txt)

    # ─────────────────────────────
    # HISTORY
    # ─────────────────────────────

    def build_history(self):

        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=10)

        ctk.CTkLabel(frame, text="License History").pack(anchor="w")

        self.history = ctk.CTkTextbox(frame)
        self.history.pack(fill="both", expand=True)

        self.update_history()

    def update_history(self):

        self.history.delete("1.0", "end")

        for item in reversed(self.db[-20:]):

            line = f'{item["date"]} | {item["customer"]} | {item["machine_id"]} | {item["type"]}\n'
            self.history.insert("end", line)


if __name__ == "__main__":
    app = LicenseGUI()
    app.mainloop()