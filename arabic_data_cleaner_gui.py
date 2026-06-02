import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import pandas as pd
import threading
import json
import urllib.request
import webbrowser
import tempfile
import os
import datetime
from pathlib import Path
from arabic_name_engine import detect_duplicates, normalize_arabic
from license_verify import get_machine_id, verify_license_code, save_license, load_and_verify
from medical_match_engine import (detect_medical_duplicates, COL_TYPES,
                                   DEFAULT_WEIGHTS, composite_score)
from patient_error_detector import detect_patient_errors

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

df           = None
current_file = None
confirmed_duplicates = set()
undo_stack   = []
main_results = []
in_search_mode = False
extra_cols   = []
has_unsaved_changes = False   # تتبع التغييرات غير المحفوظة
main_results_index  = {}      # (n1,n2) → index في main_results للبحث السريع

# ── مجلد الجلسات ──
SESSIONS_DIR = Path.home() / ".arabic_data_cleaner" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

NAME_HINTS = [
    "اسم المريض", "الاسم الثلاثي", "الاسم الكامل",
    "اسم المستفيد", "اسم العميل", "اسم الموظف",
    "الاسم", "اسم", "name", "full name", "fullname", "patient"
]

# ─────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────

def _session_path(session_id):
    return SESSIONS_DIR / f"{session_id}.json"

def mark_unsaved():
    """يُشغَّل عند أي تغيير — يضع نجمة في عنوان النافذة"""
    global has_unsaved_changes
    has_unsaved_changes = True
    title = app.title()
    if not title.startswith("*"):
        app.title("* " + title)

def mark_saved():
    global has_unsaved_changes
    has_unsaved_changes = False
    app.title(app.title().lstrip("* "))

def save_session(auto=False):
    """حفظ الجلسة الحالية"""
    global current_file, main_results, confirmed_duplicates
    if not main_results:
        if not auto:
            messagebox.showinfo("تنبيه", "لا توجد نتائج للحفظ — قم بالتحليل أولاً")
        return

    now       = datetime.datetime.now()
    sid       = now.strftime("%Y%m%d_%H%M%S")
    file_name = os.path.basename(current_file) if current_file else "غير محدد"

    # جمع حالة كل صف من الجدول الحالي
    rows_state = []
    for iid in tree.get_children():
        vals = tree.item(iid)["values"]
        tags = list(tree.item(iid)["tags"])
        rows_state.append({
            "score": str(vals[0]),
            "name1": str(vals[1]),
            "name2": str(vals[2]),
            "tags":  tags,
            "confirmed": iid in confirmed_duplicates
        })

    session = {
        "id":         sid,
        "date":       now.strftime("%Y-%m-%d %H:%M"),
        "file":       current_file or "",
        "file_name":  file_name,
        "col":        name_column.get() if name_column.get() else "",
        "total":      len(rows_state),
        "confirmed":  sum(1 for r in rows_state if r["confirmed"]),
        "rows":       rows_state,
    }

    with open(_session_path(sid), "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)

    if not auto:
        set_status(f"✓ تم حفظ الجلسة  |  {len(rows_state)} زوج  |  {session['confirmed']} مؤكد", "#00c896")
        mark_saved()
        refresh_history_panel()
    else:
        mark_saved()
    return sid

def load_session(sid):
    """استعادة جلسة محفوظة"""
    global main_results, confirmed_duplicates, current_file, in_search_mode

    path = _session_path(sid)
    if not path.exists():
        messagebox.showerror("خطأ", "الجلسة غير موجودة")
        return

    with open(path, encoding="utf-8") as f:
        session = json.load(f)

    # مسح الحالة الحالية
    clear_table()
    confirmed_duplicates.clear()
    main_results       = []
    main_results_index = {}
    in_search_mode     = False
    back_button.configure(state="disabled")

    # استعادة الصفوف — batch insert
    tree.configure(displaycolumns=())
    for i, r in enumerate(session["rows"]):
        tags = tuple(r["tags"])
        iid  = tree.insert("", "end",
                           values=(r["score"], r["name1"], r["name2"]),
                           tags=tags)
        if r.get("confirmed"):
            confirmed_duplicates.add(iid)
        entry = (r["score"], r["name1"], r["name2"],
                 tags[0] if tags else "med", r.get("confirmed", False))
        main_results.append(entry)
        main_results_index[(r["name1"], r["name2"])] = i
    tree.configure(displaycolumns=("score","name1","name2"))

    # استعادة اسم الملف في الـ status
    fname = session.get("file_name", "غير محدد")
    date  = session.get("date", "")
    total = session.get("total", 0)
    conf  = session.get("confirmed", 0)
    update_confirm_label()
    set_status(f"📂 {fname}  |  جلسة {date}  |  {total} زوج  |  {conf} مؤكد", "#4f8ef7")

def delete_session(sid):
    path = _session_path(sid)
    if path.exists():
        path.unlink()
    refresh_history_panel()

def list_sessions():
    """قائمة الجلسات مرتبة من الأحدث للأقدم"""
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fp:
                s = json.load(fp)
            sessions.append(s)
        except Exception:
            pass
    return sorted(sessions, key=lambda x: x.get("date",""), reverse=True)

def refresh_history_panel():
    """تحديث لوحة التاريخ في الـ sidebar"""
    for w in history_frame.winfo_children():
        w.destroy()

    sessions = list_sessions()

    if not sessions:
        ctk.CTkLabel(history_frame,
                     text="لا توجد جلسات محفوظة",
                     font=ctk.CTkFont(size=10),
                     text_color=C["muted"]).pack(pady=8)
        return

    for s in sessions[:8]:   # أحدث 8 جلسات
        sid   = s["id"]
        fname = s.get("file_name", "ملف")[:18]
        date  = s.get("date", "")[-5:]   # HH:MM فقط
        day   = s.get("date", "")[:10]
        conf  = s.get("confirmed", 0)
        total = s.get("total", 0)

        row = ctk.CTkFrame(history_frame, fg_color=C["card"],
                           corner_radius=8)
        row.pack(fill="x", padx=6, pady=2)

        # معلومات الجلسة
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=6, pady=4)

        ctk.CTkLabel(info, text=fname,
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=C["text"],
                     anchor="w").pack(anchor="w")
        ctk.CTkLabel(info,
                     text=f"{day} {date}  ·  {conf}/{total} مؤكد",
                     font=ctk.CTkFont(size=9),
                     text_color=C["muted"],
                     anchor="w").pack(anchor="w")

        # أزرار تحميل وحذف
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.pack(side="right", padx=4)

        ctk.CTkButton(btns, text="↩",
                      width=28, height=26,
                      fg_color=C["accent"], hover_color="#3a7de0",
                      font=ctk.CTkFont(size=13),
                      command=lambda s=sid: load_session(s)
                      ).pack(side="left", padx=2)

        ctk.CTkButton(btns, text="🗑",
                      width=28, height=26,
                      fg_color=C["card"], hover_color="#3d0a14",
                      border_width=1, border_color=C["border"],
                      font=ctk.CTkFont(size=11),
                      command=lambda s=sid: (
                          delete_session(s) if messagebox.askyesno(
                              "حذف", "حذف هذه الجلسة؟") else None
                      )).pack(side="left", padx=2)

def auto_save():
    """حفظ تلقائي كل 3 دقائق"""
    save_session(auto=True)
    app.after(180_000, auto_save)



def find_header_row(file, sheet):
    preview = pd.read_excel(file, sheet_name=sheet, header=None, nrows=20)
    for i, row in preview.iterrows():
        for cell in row:
            if any(h in str(cell).strip().lower() for h in NAME_HINTS):
                return int(i)
    return 0

def auto_detect_name_col(columns):
    cols_lower = [str(c).strip().lower() for c in columns]
    for hint in NAME_HINTS:
        for i, col in enumerate(cols_lower):
            if hint in col:
                return columns[i]
    return columns[0]

def ask_sheet(sheets):
    dialog = tk.Toplevel(app)
    dialog.title("اختر الـ Sheet")
    dialog.geometry("380x300")
    dialog.configure(bg="#2b2b2b")
    dialog.grab_set()
    tk.Label(dialog, text="اختر الـ Sheet التي تحتوي على الأسماء:",
             bg="#2b2b2b", fg="white", font=("Arial", 11)).pack(pady=15)
    chosen = tk.StringVar(value=sheets[0])
    for s in sheets:
        tk.Radiobutton(dialog, text=s, variable=chosen, value=s,
                       bg="#2b2b2b", fg="white", selectcolor="#1f6aa5",
                       font=("Arial", 11), activebackground="#2b2b2b",
                       activeforeground="white").pack(anchor="w", padx=40)
    result = [None]
    def confirm():
        result[0] = chosen.get(); dialog.destroy()
    tk.Button(dialog, text="تأكيد ✓", command=confirm,
              bg="#1f6aa5", fg="white", font=("Arial", 11),
              relief="flat", padx=20, pady=5).pack(pady=15)
    app.wait_window(dialog)
    return result[0] or sheets[0]

def set_status(text, color="#aaaaaa"):
    status_label.configure(text=text, text_color=color)

def set_progress(v, text=""):
    progress_bar.set(v)
    progress_label.configure(text=text)
    app.update_idletasks()

def reset_progress():
    progress_bar.set(0); progress_label.configure(text="")

def update_confirm_label():
    n = len(confirmed_duplicates)
    confirm_label.configure(text=f"✅ مؤكد للحذف: {n}" if n > 0 else "")

# ─────────────────────────────────────────
# Load Excel
# ─────────────────────────────────────────

def deep_clean_text(val):
    """للتصدير فقط"""
    import unicodedata
    if pd.isna(val): return val
    text = str(val)
    invisible = {'\u200f','\u200e','\u200b','\u200c','\u200d','\u202a','\u202b',
                 '\u202c','\u202d','\u202e','\ufeff','\xa0','\u00ad','\u2060'}
    text = "".join(c for c in text if c not in invisible)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("M"))
    return " ".join(text.split())

def load_excel():
    global df, current_file
    file = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx *.xls")])
    if not file: return
    current_file = file
    xl     = pd.ExcelFile(file)
    sheets = xl.sheet_names
    chosen = ask_sheet(sheets) if len(sheets) > 1 else sheets[0]
    hrow   = find_header_row(file, chosen)

    # اقرأ بدون header: index 0,1,2,... = ترتيب الصفوف في pandas
    # رقم الصف في Excel = pandas_index + 1
    df_raw = pd.read_excel(file, sheet_name=chosen, header=None)
    df     = pd.read_excel(file, sheet_name=chosen, header=hrow)

    # أول صف بيانات في df يقابل df_raw[hrow+1]
    data_start = hrow + 1
    n = len(df)
    df["__excel_row__"] = [int(df_raw.index[data_start + i]) + 1 for i in range(n)]

    df.dropna(how="all", inplace=True)
    df.dropna(axis=1, how="all", inplace=True)
    df.reset_index(drop=True, inplace=True)

    cols = [c for c in df.columns if c != "__excel_row__"]
    name_column.configure(values=cols)
    best = auto_detect_name_col(cols)
    name_column.set(best)
    clear_table()
    set_status(f"✓ تم التحميل  |  {chosen}  |  {len(df)} صف  |  عمود: {best}", "#4CAF50")

def manual_search():
    """ابحث عن اسم وشوف كل تكراراته في الملف"""
    global df, in_search_mode
    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return
    query = search_var.get().strip()
    if not query:
        return
    col    = name_column.get()
    q_norm = normalize_arabic(query)

    # ── vectorized بدل iterrows ──
    norm_series = df[col].apply(lambda x: normalize_arabic(str(x)))
    mask        = norm_series.str.contains(q_norm, regex=False, na=False)
    matched_df  = df[mask]

    children = tree.get_children()
    if children:
        tree.delete(*children)

    tree.configure(displaycolumns=())
    for _, row in matched_df.iterrows():
        name      = str(row[col])
        excel_row = int(row["__excel_row__"]) if "__excel_row__" in df.columns else _+2
        tree.insert("", "end",
                    values=(f"صف {excel_row}", name, "— بحث يدوي —"),
                    tags=("search",))
    tree.configure(displaycolumns=("score","name1","name2"))

    in_search_mode = True
    back_button.configure(state="normal")
    set_status(f"نتائج البحث عن '{query}': {len(matched_df)} تطابق  |  اضغط '↩ رجوع' للعودة", "#64B5F6")

# ─────────────────────────────────────────
# Table helpers
# ─────────────────────────────────────────

def clear_table(clear_undo=True):
    # detach كل الصفوف دفعة واحدة بدل حذف واحداً واحداً
    children = tree.get_children()
    if children:
        tree.delete(*children)
    confirmed_duplicates.clear()
    if clear_undo:
        undo_stack.clear()
    update_confirm_label()
    update_undo_button()

def update_undo_button():
    if undo_stack:
        undo_button.configure(state="normal", text=f"↩️ تراجع ({len(undo_stack)})")
    else:
        undo_button.configure(state="disabled", text="↩️ تراجع")

def mark_confirmed(event=None):
    selected = tree.selection()
    if not selected: return
    iid = selected[0]
    old_tags = tree.item(iid)["tags"]
    confirmed_duplicates.add(iid)
    tree.item(iid, tags=("confirmed",))
    undo_stack.append({"action": "confirm", "iid": iid, "old_tags": old_tags})
    # حدّث main_results بالـ index dict
    vals = tree.item(iid)["values"]
    key  = (str(vals[1]), str(vals[2]))
    if key in main_results_index:
        i = main_results_index[key]
        s,n1,n2,tag,_ = main_results[i]
        main_results[i] = (s,n1,n2,tag,True)
    update_confirm_label()
    update_undo_button()
    mark_unsaved()
    nxt = tree.next(iid)
    if nxt: tree.selection_set(nxt); tree.see(nxt)

def mark_different(event=None):
    selected = tree.selection()
    if not selected: return
    iid = selected[0]
    vals     = tree.item(iid)["values"]
    tags     = tree.item(iid)["tags"]
    nxt      = tree.next(iid)
    prev     = tree.prev(iid)
    pos      = tree.index(iid)
    was_confirmed = iid in confirmed_duplicates
    undo_stack.append({
        "action":        "delete",
        "values":        vals,
        "tags":          tags,
        "position":      pos,
        "was_confirmed": was_confirmed,
    })
    # احذف من main_results بالـ index dict
    key = (str(vals[1]), str(vals[2]))
    if key in main_results_index:
        i = main_results_index.pop(key)
        main_results.pop(i)
        # أعد بناء الـ index بعد الحذف
        for j in range(i, len(main_results)):
            k = (main_results[j][1], main_results[j][2])
            main_results_index[k] = j
    confirmed_duplicates.discard(iid)
    tree.delete(iid)
    if nxt: tree.selection_set(nxt); tree.see(nxt)
    elif prev: tree.selection_set(prev); tree.see(prev)
    update_confirm_label()
    update_undo_button()
    filter_count_label.configure(text=f"عرض: {len(tree.get_children())} من {len(main_results)}")
    mark_unsaved()

def undo_last(event=None):
    """تراجع عن آخر عملية"""
    if not undo_stack: return
    last = undo_stack.pop()

    if last["action"] == "delete":
        # أعد إدراج الصف في موضعه الأصلي
        pos  = last["position"]
        iid  = tree.insert("", pos, values=last["values"], tags=last["tags"])
        if last["was_confirmed"]:
            confirmed_duplicates.add(iid)
        tree.selection_set(iid)
        tree.see(iid)

    elif last["action"] == "confirm":
        iid = last["iid"]
        # أعد الحالة السابقة
        confirmed_duplicates.discard(iid)
        tree.item(iid, tags=last["old_tags"])
        tree.selection_set(iid)
        tree.see(iid)

    update_confirm_label()
    update_undo_button()
    mark_unsaved()

def mark_all_confirmed():
    for iid in tree.get_children():
        old_tags = tree.item(iid)["tags"]
        undo_stack.append({"action": "confirm", "iid": iid, "old_tags": old_tags})
        confirmed_duplicates.add(iid)
        tree.item(iid, tags=("confirmed",))
    update_confirm_label()
    update_undo_button()
    mark_unsaved()

def back_to_main():
    """الرجوع إلى نتائج التحليل الرئيسية بعد البحث"""
    global in_search_mode
    if not main_results:
        set_status("لا توجد نتائج تحليل سابقة — اضغط Analyze أولاً", "#FF9800")
        return
    children = tree.get_children()
    if children:
        tree.delete(*children)
    confirmed_duplicates.clear()
    undo_stack.clear()
    tree.configure(displaycolumns=())
    for sc, n1, n2, tag, is_confirmed in main_results:
        iid = tree.insert("", "end", values=(sc, n1, n2),
                          tags=("confirmed" if is_confirmed else tag,))
        if is_confirmed:
            confirmed_duplicates.add(iid)
    tree.configure(displaycolumns=("score","name1","name2"))
    in_search_mode = False
    back_button.configure(state="disabled")
    search_var.set("")
    update_confirm_label()
    update_undo_button()
    set_status(f"إجمالي النتائج: {len(main_results)}  |  راجع كل زوج ✅ مكرر  أو  ❌ مختلف", "#aaaaaa")

def clean_for_excel(text):
    """تنظيف شامل للنص ليكون قابلاً للبحث في Excel"""
    import unicodedata
    text = str(text).strip()
    # إزالة كل الأحرف غير المرئية: RTL/LTR markers، مسافات خاصة، zero-width
    invisible = {
        '\u200f','\u200e','\u200b','\u200c','\u200d',
        '\u202a','\u202b','\u202c','\u202d','\u202e',
        '\ufeff','\xa0','\u00ad','\u2060','\u180e',
    }
    text = "".join(c for c in text if c not in invisible)
    # إزالة التشكيل
    text = "".join(c for c in text if not unicodedata.category(c).startswith("M"))
    # توحيد المسافات
    text = " ".join(text.split())
    return text

def copy_name1(event=None):
    sel = tree.selection()
    if not sel: return
    vals = tree.item(sel[0])["values"]
    if vals and len(vals) > 1:
        app.clipboard_clear(); app.clipboard_append(str(vals[1]))

def copy_name2(event=None):
    sel = tree.selection()
    if not sel: return
    vals = tree.item(sel[0])["values"]
    if vals and len(vals) > 2:
        app.clipboard_clear(); app.clipboard_append(str(vals[2]))

def copy_row(event=None):
    sel = tree.selection()
    if not sel: return
    vals = tree.item(sel[0])["values"]
    if vals:
        app.clipboard_clear(); app.clipboard_append(f"{vals[1]}  ↔  {vals[2]}")

def copy_name1_clean(event=None):
    sel = tree.selection()
    if not sel: return
    vals = tree.item(sel[0])["values"]
    if vals and len(vals) > 1:
        cleaned = clean_for_excel(vals[1])
        app.clipboard_clear(); app.clipboard_append(cleaned)
        set_status(f"✓ نسخ نظيف: {cleaned}", "#00c896")

def copy_name2_clean(event=None):
    sel = tree.selection()
    if not sel: return
    vals = tree.item(sel[0])["values"]
    if vals and len(vals) > 2:
        cleaned = clean_for_excel(vals[2])
        app.clipboard_clear(); app.clipboard_append(cleaned)
        set_status(f"✓ نسخ نظيف: {cleaned}", "#00c896")

def show_context_menu(event):
    row_id = tree.identify_row(event.y)
    if row_id: tree.selection_set(row_id)
    context_menu.tk_popup(event.x_root, event.y_root)
    context_menu.grab_release()

# ─────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────

def run_analysis():
    analyze_button.configure(state="disabled", text="جاري التحليل...")
    reset_progress(); clear_table()
    threading.Thread(target=analyze).start()

def analyze():
    global df
    if df is None:
        app.after(0, lambda: analyze_button.configure(state="normal", text="🔍 Analyze"))
        return
    col = name_column.get()
    app.after(0, lambda: set_progress(0.1, "جاري التحضير..."))

    def pcb(v, t):
        app.after(0, lambda vv=v, tt=t: set_progress(vv, tt))

    # ══════════════════════════════════════
    # وضع المطابقة الطبية المتعددة الأعمدة
    # ══════════════════════════════════════
    if medical_mode and medical_config:
        med_results = detect_medical_duplicates(
            df, col,
            col_config       = medical_config,
            threshold        = medical_threshold,
            date_tolerance   = medical_date_tol,
            patient_id_col   = medical_pid_col or None,
            progress_callback= pcb,
        )

        def update_ui_medical():
            global main_results, in_search_mode, main_results_index
            clear_table()
            main_results       = []
            main_results_index = {}
            in_search_mode     = False
            back_button.configure(state="disabled")

            n_id      = 0
            n_merge   = 0   # تطابق >= 95% يستحق تنبيه دمج

            tree.configure(displaycolumns=())
            for i, row in enumerate(med_results.values[:500]):
                n1, n2, sc = str(row[0]), str(row[1]), float(row[2])
                details_json  = str(row[3])
                is_id_match   = bool(row[4])
                row1_num      = int(row[5]) if not pd.isna(row[5]) else 0
                row2_num      = int(row[6]) if not pd.isna(row[6]) else 0

                # تحديد الـ tag بدقة
                if is_id_match:
                    tag = "exact"
                    n_id += 1
                elif sc >= 95:
                    tag = "high"
                    n_merge += 1
                elif sc >= 85:
                    tag = "med"
                else:
                    tag = "med"

                score_str = f"{sc:.0f}%"
                tree.insert("", "end",
                            values=(score_str, n1, n2),
                            tags=(tag,))
                main_results.append((score_str, n1, n2, tag, False,
                                     details_json, is_id_match,
                                     row1_num, row2_num))
                main_results_index[(n1, n2)] = i
            tree.configure(displaycolumns=("score","name1","name2"))

            set_progress(1.0, f"اكتمل - {len(med_results)} نتيجة")
            analyze_button.configure(state="normal", text="🔍 Analyze")
            _rebuild_filter_buttons()   # ← حدّث أزرار الفلتر للوضع الطبي
            active_filter.set("all")
            apply_filter("all")
            mark_unsaved()

            # ── تنبيه دمج ذكي ──
            if n_id > 0 or n_merge > 0:
                _show_merge_alert(n_id, n_merge, len(med_results))
            else:
                set_status(
                    f"⚕️ مطابقة طبية: {len(med_results)} زوج  |  عتبة {medical_threshold}%",
                    C["muted"]
                )

        app.after(0, update_ui_medical)
        return

    # ══════════════════════════════════════
    # الوضع العادي (اسم فقط)
    # ══════════════════════════════════════
    results = detect_duplicates(df, col, progress_callback=pcb)

    if extra_cols:
        norm_col = df[col].apply(lambda x: normalize_arabic(str(x).strip()))
        lookup   = {}
        for ecol in extra_cols:
            if ecol not in df.columns: continue
            for norm_n, val in zip(norm_col, df[ecol]):
                if pd.isna(val): continue
                lookup.setdefault(norm_n, {}).setdefault(ecol, set()).add(str(val).strip().lower())
        filtered = []
        for _, row in results.iterrows():
            n1, n2, sc = str(row["Name1"]).strip(), str(row["Name2"]).strip(), float(row["Similarity"])
            n1n, n2n   = normalize_arabic(n1), normalize_arabic(n2)
            boost = reject = False
            for ecol in extra_cols:
                s1 = lookup.get(n1n, {}).get(ecol, set())
                s2 = lookup.get(n2n, {}).get(ecol, set())
                if not s1 or not s2: continue
                if s1 & s2:   boost  = True
                else:         reject = True; break
            if reject: continue
            filtered.append((n1, n2, min(100.0, sc+5) if boost else sc))
        results = pd.DataFrame(filtered, columns=["Name1","Name2","Similarity"])

    app.after(0, lambda: set_progress(0.95, "جاري العرض..."))

    def update_ui():
        global main_results, in_search_mode, main_results_index
        clear_table()
        main_results       = []
        main_results_index = {}
        in_search_mode     = False
        back_button.configure(state="disabled")

        tree.configure(displaycolumns=())
        for i, r in enumerate(results.values[:500]):
            n1, n2, sc = str(r[0]), str(r[1]), float(r[2])
            tag       = "exact" if sc >= 99 else ("high" if sc >= 95 else "med")
            score_str = f"{sc:.0f}%"
            tree.insert("", "end", values=(score_str, n1, n2), tags=(tag,))
            main_results.append((score_str, n1, n2, tag, False))
            main_results_index[(n1, n2)] = i
        tree.configure(displaycolumns=("score","name1","name2"))

        extra_info = f"  |  أعمدة إضافية: {', '.join(extra_cols)}" if extra_cols else ""
        set_status(f"إجمالي النتائج: {len(results)}{extra_info}  |  راجع كل زوج ✅ مكرر  أو  ❌ مختلف", "#aaaaaa")
        set_progress(1.0, f"اكتمل - {len(results)} نتيجة")
        analyze_button.configure(state="normal", text="🔍 Analyze")
        _rebuild_filter_buttons()   # ← حدّث أزرار الفلتر للوضع العادي
        active_filter.set("all")
        apply_filter("all")
        mark_unsaved()

    app.after(0, update_ui)

# ─────────────────────────────────────────
# Export
# ─────────────────────────────────────────

def export_clean():
    global df
    if df is None: return
    col = name_column.get()

    # Collect name2 of confirmed pairs to remove
    names_to_remove = set()
    for iid in confirmed_duplicates:
        try:
            vals = tree.item(iid)["values"]
            if vals and len(vals) > 2:
                n1, n2 = str(vals[1]).strip(), str(vals[2]).strip()
                if n1 == n2:
                    names_to_remove.add(normalize_arabic(n1))
                else:
                    names_to_remove.add(normalize_arabic(n2))
        except Exception:
            pass

    if not names_to_remove:
        messagebox.showinfo("تنبيه", "لم تؤكد أي تكرارات بعد!\nاضغط ✅ على الأزواج المكررة أولاً.")
        return

    df_clean = df.copy()
    df_clean["_norm"] = df_clean[col].apply(lambda x: normalize_arabic(str(x).strip()))

    # For exact duplicates: keep first occurrence only
    seen = set()
    keep = []
    for i, row in df_clean.iterrows():
        n = row["_norm"]
        if n in names_to_remove:
            if n not in seen:
                seen.add(n); keep.append(True)
            else:
                keep.append(False)
        else:
            keep.append(True)

    df_clean = df_clean[keep]
    df_clean = df_clean.drop(columns=["_norm"])

    file = filedialog.asksaveasfilename(defaultextension=".xlsx")
    if not file: return
    df_clean.to_excel(file, index=False)
    messagebox.showinfo("تم ✓", f"تم حفظ الملف النظيف\nتم حذف تكرارات {len(names_to_remove)} اسم.")

def finish_file():
    """إنهاء العمل على الملف الحالي مع خيارات الحفظ"""
    global df, current_file, confirmed_duplicates, main_results
    global undo_stack, in_search_mode, has_unsaved_changes

    if df is None and not main_results:
        messagebox.showinfo("تنبيه", "لا يوجد ملف محمل حالياً.")
        return

    # ── بناء ملخص الجلسة ──
    total     = len(main_results)
    n_conf    = len(confirmed_duplicates)
    n_pending = total - n_conf
    fname     = os.path.basename(current_file) if current_file else "غير محدد"

    # ── نافذة الإنهاء ──
    win = tk.Toplevel(app)
    win.title("إنهاء الملف الحالي")
    win.geometry("420x380")
    win.configure(bg=C["panel"])
    win.grab_set()
    win.resizable(False, False)

    # عنوان
    tk.Label(win, text="إنهاء العمل على الملف",
             bg=C["panel"], fg=C["text"],
             font=("Tahoma", 14, "bold")).pack(pady=(20,4))

    # ملخص
    summary = ctk.CTkFrame(win, fg_color=C["card"], corner_radius=10)
    summary.pack(fill="x", padx=20, pady=8)

    for label, value, color in [
        ("الملف",          fname,         C["text"]),
        ("إجمالي الأزواج", str(total),    C["text"]),
        ("مؤكدة للحذف",   str(n_conf),   C["green"] if n_conf else C["muted"]),
        ("لم تُراجع بعد", str(n_pending), C["orange"] if n_pending else C["muted"]),
    ]:
        row = ctk.CTkFrame(summary, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(row, text=label+":", font=ctk.CTkFont(size=12),
                     text_color=C["muted"], width=120, anchor="w").pack(side="left")
        ctk.CTkLabel(row, text=value, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=color).pack(side="left")

    # تحذير إذا فيه غير مراجعة
    if n_pending > 0:
        ctk.CTkLabel(win,
                     text=f"⚠️  يوجد {n_pending} زوج لم يُراجع بعد",
                     font=ctk.CTkFont(size=11),
                     text_color=C["orange"]).pack(pady=(4,0))

    ctk.CTkLabel(win, text="ماذا تريد أن تفعل؟",
                 font=ctk.CTkFont(size=12),
                 text_color=C["muted"]).pack(pady=(12,6))

    def _btn(text, fg, hv, cmd):
        ctk.CTkButton(win, text=text, command=cmd,
                      width=360, height=38,
                      fg_color=fg, hover_color=hv,
                      font=ctk.CTkFont(size=13),
                      corner_radius=8).pack(padx=20, pady=4)

    def do_export_and_close():
        win.destroy()
        export_clean()
        _reset_state()

    def do_save_session_and_close():
        win.destroy()
        save_session(auto=False)
        _reset_state()

    def do_export_and_session():
        win.destroy()
        save_session(auto=False)
        export_clean()
        _reset_state()

    def do_close_only():
        win.destroy()
        _reset_state()

    def _reset_state():
        """مسح كل شيء والبدء من جديد"""
        global df, current_file, confirmed_duplicates, main_results
        global undo_stack, in_search_mode, has_unsaved_changes
        df = None
        current_file = None
        confirmed_duplicates = set()
        undo_stack = []
        main_results = []
        in_search_mode = False
        has_unsaved_changes = False
        clear_table()
        name_column.configure(values=[])
        name_column.set("")
        active_filter.set("all")
        apply_filter("all")
        app.title("Arabic Data Cleaner")
        set_status("في انتظار تحميل ملف...", C["muted"])

    _btn("💾  تصدير الملف النظيف + إغلاق",       "#0d3320","#1a5c38", do_export_and_close)
    _btn("📌  حفظ الجلسة فقط + إغلاق",           "#0d1f4a","#1a3a7a", do_save_session_and_close)
    _btn("💾📌  تصدير + حفظ الجلسة + إغلاق",     "#2d1f00","#5c3d00", do_export_and_session)
    _btn("🗑️   إغلاق بدون حفظ",                  C["card"], C["border"], do_close_only)

    ctk.CTkButton(win, text="↩  إلغاء — العودة للمراجعة",
                  command=win.destroy,
                  width=360, height=32,
                  fg_color="transparent", hover_color=C["card"],
                  border_width=1, border_color=C["border"],
                  text_color=C["muted"],
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(padx=20, pady=(4,16))

# ─────────────────────────────────────────
# AI Verify — Claude API
# ─────────────────────────────────────────

def ai_verify_selected():
    """أرسل الزوج المحدد لـ Claude ليقرر هل هو مكرر"""
    sel = tree.selection()
    if not sel:
        messagebox.showinfo("تنبيه", "اختر زوجاً أولاً")
        return
    vals = tree.item(sel[0])["values"]
    if not vals or len(vals) < 3:
        return
    n1, n2 = str(vals[1]).strip(), str(vals[2]).strip()
    if n1 == n2:
        messagebox.showinfo("AI", f"الاسم '{n1}' مكرر حرفياً في الملف ✓")
        return

    ai_button.configure(state="disabled", text="🤖 جاري السؤال...")
    threading.Thread(target=lambda: _ai_call(n1, n2, sel[0])).start()

def _ai_call(n1, n2, iid):
    prompt = f"""أنت خبير في الأسماء العربية. هل الاسمان التاليان يمكن أن يكونا لنفس الشخص (تكرار) أم أشخاص مختلفون؟

الاسم الأول:  {n1}
الاسم الثاني: {n2}

أجب بـ JSON فقط بهذا الشكل:
{{"decision": "مكرر" أو "مختلف", "confidence": رقم 0-100, "reason": "سبب قصير"}}"""

    try:
        data = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        text = body["content"][0]["text"].strip()
        # parse JSON from response
        text = text[text.find("{"):text.rfind("}")+1]
        result = json.loads(text)
        decision   = result.get("decision", "غير محدد")
        confidence = result.get("confidence", 0)
        reason     = result.get("reason", "")
        app.after(0, lambda: _ai_show_result(iid, n1, n2, decision, confidence, reason))
    except Exception as e:
        app.after(0, lambda: _ai_error(str(e)))

def _ai_show_result(iid, n1, n2, decision, confidence, reason):
    ai_button.configure(state="normal", text="🤖 AI تحقق")
    color = "#f44336" if decision == "مكرر" else "#4CAF50"
    msg = f"القرار: {decision}  ({confidence}% ثقة)\nالسبب: {reason}"
    messagebox.showinfo(f"🤖 Claude AI — {n1} ↔ {n2}", msg)
    # Auto-apply decision
    try:
        if decision == "مكرر":
            confirmed_duplicates.add(iid)
            tree.item(iid, tags=("confirmed",))
            undo_stack.append({"action": "confirm", "iid": iid, "old_tags": ("med",)})
        else:
            vals = tree.item(iid)["values"]
            pos  = tree.index(iid)
            tags = tree.item(iid)["tags"]
            undo_stack.append({"action": "delete", "values": vals, "tags": tags,
                               "position": pos, "was_confirmed": False})
            confirmed_duplicates.discard(iid)
            tree.delete(iid)
        update_confirm_label()
        update_undo_button()
    except Exception:
        pass

def _ai_error(msg):
    ai_button.configure(state="normal", text="🤖 AI تحقق")
    messagebox.showerror("خطأ AI", f"فشل الاتصال بـ Claude API:\n{msg}\n\nتأكد من الاتصال بالإنترنت.")

def _show_merge_alert(n_id: int, n_merge: int, total: int):
    """
    يعرض شريط تنبيه أحمر/أخضر أسفل شريط الـ status
    مع ملخص واضح واقتراح دمج.
    """
    parts = []
    color = C["muted"]

    if n_id > 0:
        parts.append(f"🔑 {n_id} تطابق Patient ID مؤكد")
        color = C["green"]
    if n_merge > 0:
        parts.append(f"⚠️ {n_merge} زوج بتطابق ≥95%")
        color = C["orange"] if not n_id else C["green"]

    summary = "  |  ".join(parts)
    set_status(
        f"⚕️ مطابقة طبية: {total} زوج  |  {summary}"
        f"  —  Double-click لعرض التفاصيل والدمج",
        color
    )

    # تنبيه popup فقط إذا في Patient ID مؤكد
    if n_id > 0:
        messagebox.showinfo(
            "تطابق Patient ID مؤكد ✓",
            f"اكتُشف {n_id} زوج بنفس رقم المريض.\n\n"
            f"هذه السجلات لنفس المريض بالتأكيد.\n"
            f"Double-click على أي زوج لنسخ Patient ID والدمج."
        )


# ─────────────────────────────────────────
# Medical Multi-column Matching System
# ─────────────────────────────────────────

extra_cols      = []    # للتوافق مع الكود القديم
medical_config  = []    # [{col, type, weight, is_patient_id}]
medical_mode    = False # هل نحن في وضع المطابقة الطبية؟
medical_threshold = 80  # عتبة النسبة الإجمالية
medical_date_tol  = 30  # tolerance للتواريخ بالأيام
medical_pid_col   = ""  # عمود Patient ID

TYPE_LABELS = {
    "name":      "🔤 اسم عربي",
    "date":      "📅 تاريخ",
    "gender":    "👤 جنس",
    "id":        "🔑 رقم/كود",
    "diagnosis": "🏥 تشخيص",
    "text":      "📝 نص عام",
}
TYPE_COLORS = {
    "name":      "#4f8ef7",
    "date":      "#ffd32a",
    "gender":    "#7c5ce8",
    "id":        "#00c896",
    "diagnosis": "#ff9f43",
    "text":      "#aaaaaa",
}

def open_multicol_dialog():
    """نافذة إعداد المطابقة الطبية"""
    global medical_config, medical_mode, medical_threshold
    global medical_date_tol, medical_pid_col, extra_cols

    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return

    win = tk.Toplevel(app)
    win.title("إعداد المطابقة الطبية المتعددة الأعمدة")
    win.geometry("620x620")
    win.configure(bg=C["panel"])
    win.grab_set()
    win.resizable(False, True)

    # ── Header ──
    tk.Label(win, text="⚕️  إعداد المطابقة الطبية",
             bg=C["panel"], fg=C["accent"],
             font=("Tahoma", 14, "bold")).pack(pady=(16,2))
    tk.Label(win,
             text="حدّد نوع كل عمود ووزنه في حساب نسبة التطابق",
             bg=C["panel"], fg=C["muted"],
             font=("Tahoma", 10)).pack(pady=(0,10))

    # ── Mode toggle ──
    mode_frame = tk.Frame(win, bg=C["card"], pady=8, padx=16)
    mode_frame.pack(fill="x", padx=20, pady=(0,8))

    mode_var = tk.BooleanVar(value=medical_mode)
    tk.Label(mode_frame, text="وضع المطابقة الطبية المتعددة الأعمدة:",
             bg=C["card"], fg=C["text"],
             font=("Tahoma", 11)).pack(side="left")
    ctk.CTkSwitch(mode_frame, text="", variable=mode_var,
                  width=46, height=22,
                  fg_color=C["border"], progress_color=C["green"],
                  button_color="white").pack(side="left", padx=8)
    tk.Label(mode_frame, textvariable=tk.StringVar(),
             bg=C["card"], fg=C["green"],
             font=("Tahoma", 10)).pack(side="left")

    # ── Scrollable columns area ──
    cols_frame_outer = tk.Frame(win, bg=C["panel"])
    cols_frame_outer.pack(fill="both", expand=True, padx=20)

    canvas2  = tk.Canvas(cols_frame_outer, bg=C["panel"], highlightthickness=0)
    scrollbar2 = ttk.Scrollbar(cols_frame_outer, orient="vertical", command=canvas2.yview)
    cols_inner = tk.Frame(canvas2, bg=C["panel"])
    cols_inner.bind("<Configure>",
                    lambda e: canvas2.configure(scrollregion=canvas2.bbox("all")))
    canvas2.create_window((0,0), window=cols_inner, anchor="nw")
    canvas2.configure(yscrollcommand=scrollbar2.set)
    scrollbar2.pack(side="right", fill="y")
    canvas2.pack(side="left", fill="both", expand=True)
    canvas2.bind("<MouseWheel>",
                 lambda e: canvas2.yview_scroll(int(-1*(e.delta/120)), "units"))

    # ── الأعمدة ──
    available_cols = [c for c in df.columns if c != "__excel_row__"]
    col_name_val   = name_column.get()

    # أعد بناء medical_config إذا لم يكن موجوداً
    existing = {c["col"]: c for c in medical_config}

    col_vars = {}  # col → {enabled, type_var, weight_var, pid_var}

    # ترويسة الجدول
    hdr = tk.Frame(cols_inner, bg=C["header_bg"])
    hdr.pack(fill="x", pady=(0,4))
    for txt, w in [("فعّل","40"),("العمود","160"),("النوع","140"),("الوزن %","80"),("Patient ID","80")]:
        tk.Label(hdr, text=txt, bg=C["header_bg"], fg=C["muted"],
                 font=("Tahoma", 9, "bold"), width=int(w)//8).pack(side="right", padx=4)

    for col in available_cols:
        prev = existing.get(col, {})
        auto_type = _guess_col_type(col)
        ctype  = prev.get("type",         auto_type)
        # استخدم الأوزان المسبقة إذا لم يُعدَّل سابقاً
        weight = prev.get("weight", _PRESET_WEIGHTS.get(auto_type, 10))
        is_pid = prev.get("is_patient_id", False)
        # فعّل تلقائياً: عمود الاسم + الأعمدة الأربعة المعروفة
        auto_enable = (col == col_name_val or auto_type in ("name","date","gender","id"))
        enabled = prev.get("_enabled", auto_enable) if prev else auto_enable

        enabled_var = tk.BooleanVar(value=enabled)
        type_var    = tk.StringVar(value=ctype)
        weight_var  = tk.IntVar(value=weight)
        pid_var     = tk.BooleanVar(value=is_pid)
        col_vars[col] = {"enabled": enabled_var, "type": type_var,
                         "weight": weight_var, "pid": pid_var}

        row_f = tk.Frame(cols_inner, bg=C["card"], pady=4, padx=6)
        row_f.pack(fill="x", pady=2)

        # فعّل
        tk.Checkbutton(row_f, variable=enabled_var,
                       bg=C["card"], activebackground=C["card"],
                       selectcolor=C["accent"]).pack(side="right", padx=4)
        # اسم العمود مع لون نوعه
        type_color = TYPE_COLORS.get(auto_type, C["muted"])
        tk.Label(row_f, text=col[:22], bg=C["card"], fg=type_color,
                 font=("Tahoma", 10, "bold"), width=20, anchor="e").pack(side="right", padx=4)
        # نوع
        type_menu = ttk.Combobox(row_f, textvariable=type_var,
                                  values=list(TYPE_LABELS.keys()),
                                  width=12, state="readonly")
        type_menu.pack(side="right", padx=4)
        # وزن
        weight_spin = tk.Spinbox(row_f, from_=0, to=100, textvariable=weight_var,
                                  width=5, bg=C["card"], fg=C["text"],
                                  relief="flat", font=("Tahoma", 10))
        weight_spin.pack(side="right", padx=4)
        # Patient ID
        tk.Checkbutton(row_f, variable=pid_var, text="PID",
                       bg=C["card"], fg=C["green"],
                       activebackground=C["card"],
                       selectcolor=C["card"],
                       font=("Tahoma", 9)).pack(side="right", padx=4)

        # إذا نوع = id → أضف PID تلقائياً
        if auto_type == "id" and not prev:
            pid_var.set(True)
            weight_var.set(0)

    # ── إعدادات إضافية ──
    settings_frame = tk.Frame(win, bg=C["card"], padx=16, pady=10)
    settings_frame.pack(fill="x", padx=20, pady=8)

    # عتبة النسبة
    tk.Label(settings_frame, text="عتبة التطابق %:",
             bg=C["card"], fg=C["muted"],
             font=("Tahoma", 10)).grid(row=0, column=1, padx=8, sticky="e")
    thresh_var = tk.IntVar(value=medical_threshold)
    tk.Spinbox(settings_frame, from_=50, to=99, textvariable=thresh_var,
               width=5, bg=C["card"], fg=C["text"],
               relief="flat", font=("Tahoma", 11, "bold")
               ).grid(row=0, column=0, padx=4)

    # tolerance التواريخ
    tk.Label(settings_frame, text="هامش التواريخ (أيام):",
             bg=C["card"], fg=C["muted"],
             font=("Tahoma", 10)).grid(row=0, column=3, padx=8, sticky="e")
    tol_var = tk.IntVar(value=medical_date_tol)
    tk.Spinbox(settings_frame, from_=0, to=365, textvariable=tol_var,
               width=5, bg=C["card"], fg=C["text"],
               relief="flat", font=("Tahoma", 11, "bold")
               ).grid(row=0, column=2, padx=4)

    def apply_config():
        global medical_config, medical_mode, medical_threshold
        global medical_date_tol, medical_pid_col, extra_cols

        medical_mode      = mode_var.get()
        medical_threshold = thresh_var.get()
        medical_date_tol  = tol_var.get()
        medical_config    = []
        medical_pid_col   = ""

        for col, cvars in col_vars.items():
            if not cvars["enabled"].get():
                continue
            ctype  = cvars["type"].get()
            weight = cvars["weight"].get()
            is_pid = cvars["pid"].get()
            if is_pid:
                medical_pid_col = col
            medical_config.append({
                "col":           col,
                "type":          ctype,
                "weight":        0 if is_pid else weight,
                "is_patient_id": is_pid,
            })

        # حدّث الـ label
        if medical_mode and medical_config:
            active = [c["col"] for c in medical_config if c["weight"]>0]
            multicol_label.configure(
                text=f"⚕️ طبي: {len(active)} عمود  |  عتبة {medical_threshold}%")
        else:
            multicol_label.configure(text="")

        canvas2.unbind_all("<MouseWheel>")
        win.destroy()

    ctk.CTkButton(win, text="✅  تطبيق الإعداد", command=apply_config,
                  width=560, height=40,
                  fg_color=C["accent"], hover_color="#3a7de0",
                  font=ctk.CTkFont(size=13, weight="bold"),
                  corner_radius=8).pack(padx=20, pady=(0,16))

    win.protocol("WM_DELETE_WINDOW",
                 lambda: [canvas2.unbind_all("<MouseWheel>"), win.destroy()])


def _guess_col_type(col_name: str) -> str:
    """يخمّن نوع العمود من اسمه"""
    c = col_name.lower()
    if any(k in c for k in ["اسم","name"]):                              return "name"
    if any(k in c for k in ["تاريخ","date","birth","ميلاد","dob","visit","زيارة"]): return "date"
    if any(k in c for k in ["جنس","gender","sex"]):                      return "gender"
    if any(k in c for k in ["id","رقم","code","كود","patient","مريض","هوية","هويه"]): return "id"
    if any(k in c for k in ["تشخيص","diagnosis","diag"]):               return "diagnosis"
    return "text"

# أوزان مسبقة للأعمدة الأربعة المعروفة
_PRESET_WEIGHTS = {
    "name":      40,
    "date":      30,
    "gender":    10,
    "id":        0,    # Patient ID = shortcut وليس وزن
    "diagnosis": 20,
    "text":      10,
}



# ─────────────────────────────────────────
# HTML Report Export
# ─────────────────────────────────────────

def export_html_report():
    rows = tree.get_children()
    if not rows:
        messagebox.showinfo("تنبيه", "لا توجد نتائج لتصدير التقرير")
        return

    items = []
    for iid in rows:
        vals = tree.item(iid)["values"]
        tags = tree.item(iid)["tags"]
        if vals:
            is_confirmed = "confirmed" in tags
            items.append((vals, is_confirmed))

    confirmed_count = sum(1 for _, c in items if c)
    total           = len(items)

    rows_html = ""
    for vals, is_confirmed in items:
        sc, n1, n2 = str(vals[0]), str(vals[1]), str(vals[2])
        sc_num = float(sc.replace("%","")) if "%" in sc else 0
        if is_confirmed:
            cls = "confirmed"
        elif sc_num >= 99:
            cls = "exact"
        elif sc_num >= 95:
            cls = "high"
        else:
            cls = "med"
        badge = "✅ مؤكد" if is_confirmed else ""
        rows_html += f"""
        <tr class="{cls}">
          <td class="score">{sc}</td>
          <td class="name">{n1}</td>
          <td class="name">{n2}</td>
          <td class="badge">{badge}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<title>تقرير التكرارات — Arabic Data Cleaner</title>
<style>
  body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #eee; padding: 30px; }}
  h1   {{ color: #64B5F6; text-align: center; }}
  .stats {{ display:flex; gap:20px; justify-content:center; margin:20px 0; }}
  .stat  {{ background:#2b2b2b; padding:15px 30px; border-radius:10px; text-align:center; }}
  .stat .n {{ font-size:2em; font-weight:bold; }}
  .stat .l {{ font-size:0.9em; color:#aaa; }}
  table  {{ width:100%; border-collapse:collapse; margin-top:20px; }}
  th     {{ background:#1f6aa5; padding:12px; text-align:right; }}
  td     {{ padding:10px 12px; border-bottom:1px solid #333; }}
  .exact {{ color:#f44336; }}
  .high  {{ color:#FF9800; }}
  .med   {{ color:#FFD600; }}
  .confirmed {{ color:#4CAF50; background:#1b2e1b; }}
  .score {{ text-align:center; font-weight:bold; width:80px; }}
  .badge {{ text-align:center; width:80px; }}
  tr:hover {{ background:#2a2a3e; }}
  input {{ background:#2b2b2b; border:1px solid #555; color:white; padding:8px; border-radius:5px; width:300px; }}
</style>
</head>
<body>
<h1>📊 تقرير التكرارات</h1>
<div class="stats">
  <div class="stat"><div class="n" style="color:#64B5F6">{total}</div><div class="l">إجمالي النتائج</div></div>
  <div class="stat"><div class="n" style="color:#4CAF50">{confirmed_count}</div><div class="l">مؤكد للحذف</div></div>
  <div class="stat"><div class="n" style="color:#FF9800">{total - confirmed_count}</div><div class="l">بانتظار المراجعة</div></div>
</div>
<input type="text" id="search" placeholder="🔎 ابحث في التقرير..." onkeyup="filterTable()" style="margin-bottom:15px;">
<table id="tbl">
  <tr><th>التشابه</th><th>الاسم الأول</th><th>الاسم الثاني</th><th>الحالة</th></tr>
  {rows_html}
</table>
<script>
function filterTable() {{
  var q = document.getElementById("search").value.toLowerCase();
  var rows = document.querySelectorAll("#tbl tr:not(:first-child)");
  rows.forEach(r => {{
    r.style.display = r.innerText.toLowerCase().includes(q) ? "" : "none";
  }});
}}
</script>
</body></html>"""

    # Save to temp file and open in browser
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html",
                                      mode="w", encoding="utf-8")
    tmp.write(html)
    tmp.close()
    webbrowser.open(f"file://{tmp.name}")

    # Also offer to save
    save = messagebox.askyesno("تقرير HTML", "تم فتح التقرير في المتصفح.\nهل تريد حفظ نسخة؟")
    if save:
        dest = filedialog.asksaveasfilename(defaultextension=".html",
                                             filetypes=[("HTML", "*.html")])
        if dest:
            import shutil
            shutil.copy(tmp.name, dest)


# ─────────────────────────────────────────
# Gender Checker — قاموس الأسماء
# ─────────────────────────────────────────

MALE_NAMES = {
    "محمد","أحمد","علي","عمر","خالد","يوسف","إبراهيم","عبدالله","عبدالرحمن","حسن",
    "حسين","طارق","سامي","ماجد","وليد","فيصل","تركي","نواف","سلطان","ناصر",
    "عبدالعزيز","عبدالكريم","عبدالرحيم","عبدالحميد","عبدالقادر","عبدالوهاب","عبدالمجيد",
    "عبدالجبار","عبدالمنعم","عبدالغني","عبدالله","عبدالرزاق","عبدالستار","عبدالسلام",
    "بكر","عثمان","سعد","سعيد","صالح","راشد","منصور","محمود","مصطفى","كريم",
    "هاشم","هادي","جاسم","حامد","جمال","نبيل","شريف","وسيم","فراس","قاسم",
    "باسم","حازم","غازي","عامر","ياسر","أسامة","بلال","رامي","زياد","نزار",
    "لؤي","معاذ","معتز","مؤيد","مازن","ماهر","باسل","حارث","حيدر","ضياء",
    "عدنان","سليمان","داود","صالح","طه","زكريا","يحيى","موسى","عيسى","إدريس",
    "أنس","أيمن","أكرم","عصام","عزيز","أديب","نادر","وائل","هيثم","مروان",
    "عمار","عماد","إياد","بشار","لقمان","جواد","صادق","حيان","شاكر","ذياب",
    "مشعل","فهد","سطام","حمود","دخيل","ثامر","حمد","سلمان","جابر","حمدان",
    "مبارك","غانم","صقر","زايد","طحنون","مهند","كنان","أيهم","قتيبة","حذيفة",
    "سفيان","عقيل","عقبة","مصعب","طلحة","زبير","حنظلة","عروة","شرحبيل","خزيمة",
    "معاوية","عمرو","منير","حميد","فؤاد","مجدي","سيف","ثائر","نضال","صهيب",
    "أيوب","إلياس","يونس","جبريل","ميكائيل","شعيب","لوط","إسماعيل","إسحاق","يعقوب",
    "حمزة","عباس","جعفر","هارون","مالك","رياض","توفيق","لطفي","صبري","حلمي",
    "حمدي","مدحت","ممدوح","محسن","مهدي","منذر","منتصر","مقداد","أبوبكر",
}

FEMALE_NAMES = {
    "فاطمة","عائشة","مريم","زينب","خديجة","سارة","نور","هند","لينا","رنا",
    "دينا","ريم","نادية","سمر","هبة","أميرة","ملك","غادة","سلمى","ليلى",
    "منى","نهى","وفاء","إيمان","أمل","رحمة","حنان","سناء","نجلاء","شيماء",
    "أسماء","رقية","سكينة","حفصة","صفية","ميمونة","جويرية","هالة","نوال","سهام",
    "عبير","نسرين","ياسمين","رانيا","سلافة","أريج","شذى","أريحا","هيفاء","بسمة",
    "ابتسام","وسام","رشا","لمى","نيفين","صباح","مساء","نجمة","قمر","بدرية",
    "شمسة","شمس","نجوى","وداد","مودة","صداء","لجين","رغد","رهف","جود",
    "تالا","لارا","رلى","تيما","ديما","هيلا","جنى","دلال","ميار","ولاء",
    "آلاء","علاء","بلقيس","ثريا","نضال","ناهد","نجاة","نعيمة","فريدة","كريمة",
    "سعاد","وهيبة","زهرة","زهرا","فاتن","فتون","فتحية","روضة","روز","وردة",
    "شروق","إشراق","إسراء","معراج","عروة","زلفى","حليمة","حليمى","أروى","بثينة",
    "جميلة","نفيسة","عزيزة","كوثر","تسنيم","سلسبيل","رفيدة","خولة","دعاء","رجاء",
    "ابراء","إبراء","براءة","صفاء","شفاء","ضحى","إلهام","أحلام","أفنان","غصون",
    "نسمة","نسيم","زفيرة","زفير","عطر","مسك","عنبر","لبنى","سمية","لمياء",
    "ناريمان","نارين","نارمين","شيرين","شيرزاد","بيان","بهار","جلنار","نيلوفر",
    "روان","ريان","ريانة","جمان","شمام","مها","مهرة","ظبية","غزال","وعد",
    "وعود","هيا","هيام","هيف","هنادي","نيروز","نيلة","نيلوفر","رهام","سدرة",
    "شجرة","طوبى","جنة","عدن","حوراء","عيناء","كحلاء","شهلاء","شهد","عسل",
}

GENDER_VALUES_MALE   = {
    "ذكر","ذكر","م","male","m","1","رجل","ولد","boy","man",
    "دكر","ذكور","males","boys","men","ذ"
}
GENDER_VALUES_FEMALE = {
    "أنثى","انثى","انثي","أنثي","ف","female","f","2","امرأة","بنت",
    "girl","woman","اناث","إناث","females","girls","women","ا","أ"
}

def check_gender():
    """تحقق من تطابق الاسم مع عمود الجنس"""
    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return

    # اختر أعمدة الاسم والجنس
    cols = list(df.columns)
    dialog = tk.Toplevel(app)
    dialog.title("تحقق من الجنس")
    dialog.geometry("380x220")
    dialog.configure(bg="#2b2b2b")
    dialog.grab_set()

    tk.Label(dialog, text="اختر عمود الاسم وعمود الجنس:",
             bg="#2b2b2b", fg="white", font=("Arial", 11)).pack(pady=12)

    frame = tk.Frame(dialog, bg="#2b2b2b")
    frame.pack(padx=30, fill="x")

    tk.Label(frame, text="عمود الاسم:", bg="#2b2b2b", fg="white",
             font=("Arial", 10)).grid(row=0, column=0, sticky="e", pady=6, padx=6)
    name_var = tk.StringVar(value=name_column.get())
    ttk.Combobox(frame, textvariable=name_var, values=cols, width=22,
                 state="readonly").grid(row=0, column=1, pady=6)

    tk.Label(frame, text="عمود الجنس:", bg="#2b2b2b", fg="white",
             font=("Arial", 10)).grid(row=1, column=0, sticky="e", pady=6, padx=6)
    gender_var = tk.StringVar()
    # auto-detect gender column
    for c in cols:
        if any(h in str(c).lower() for h in ["جنس","gender","sex","نوع"]):
            gender_var.set(c); break
    ttk.Combobox(frame, textvariable=gender_var, values=cols, width=22,
                 state="readonly").grid(row=1, column=1, pady=6)

    result = [None]
    def run():
        result[0] = (name_var.get(), gender_var.get())
        dialog.destroy()

    tk.Button(dialog, text="تحقق ✓", command=run,
              bg="#1f6aa5", fg="white", font=("Arial", 11),
              relief="flat", padx=20, pady=5).pack(pady=15)
    app.wait_window(dialog)

    if not result[0]: return
    ncol, gcol = result[0]
    if not gcol:
        messagebox.showwarning("تنبيه", "لم تختر عمود الجنس")
        return

    def get_first_arabic_name(text):
        """استخراج أول كلمة عربية من النص — يتجاهل الأرقام والرموز"""
        import re
        words = str(text).strip().split()
        for word in words:
            # الكلمة عربية إذا احتوت على أحرف عربية فقط
            clean = re.sub(r'[^\u0600-\u06FF]', '', word)
            if len(clean) >= 2:   # كلمة عربية حقيقية (حرفان على الأقل)
                return normalize_arabic(clean)
        return ""
    def clean_gender(val):
        """تنظيف شامل لقيمة الجنس"""
        import unicodedata
        s = str(val).strip()
        # إزالة كل الأحرف غير المرئية والتشكيل والمسافات الخاصة
        s = "".join(c for c in s if not unicodedata.category(c).startswith("M"))
        s = s.replace("\u200f","").replace("\u200e","").replace("\xa0","").replace("\u200b","")
        s = s.strip().lower()
        return s

    # ── نافذة تشخيص: شوف القيم الحقيقية قبل الفحص ──
    sample_vals = set()
    for idx, row in df.iterrows():
        raw = clean_gender(row[gcol])
        if raw and raw not in ("nan","","none"):
            sample_vals.add(raw)
        if len(sample_vals) >= 10:
            break

    # أخبر المستخدم بالقيم الموجودة في عمود الجنس
    diag = tk.Toplevel(app)
    diag.title("تشخيص — قيم عمود الجنس")
    diag.geometry("400x300")
    diag.configure(bg="#2b2b2b")
    diag.grab_set()

    tk.Label(diag, text="القيم الموجودة في عمود الجنس:",
             bg="#2b2b2b", fg="white", font=("Arial", 11, "bold")).pack(pady=10)

    frame_d = tk.Frame(diag, bg="#2b2b2b")
    frame_d.pack(fill="both", expand=True, padx=20)

    for v in sorted(sample_vals):
        hex_repr = " ".join(f"{ord(c):04x}" for c in v)
        tk.Label(frame_d, text=f'"{v}"  →  [{hex_repr}]',
                 bg="#2b2b2b", fg="#FFD600", font=("Courier", 10),
                 anchor="w").pack(anchor="w", pady=2)

    tk.Label(diag, text="هل هذه القيم صحيحة؟ تأكد ثم اضغط متابعة",
             bg="#2b2b2b", fg="#aaaaaa", font=("Arial", 10)).pack(pady=5)

    proceed = [False]
    def do_proceed():
        proceed[0] = True
        diag.destroy()
    def do_cancel():
        diag.destroy()

    bf = tk.Frame(diag, bg="#2b2b2b")
    bf.pack(pady=8)
    tk.Button(bf, text="متابعة ✓", command=do_proceed,
              bg="#1f6aa5", fg="white", font=("Arial", 11),
              relief="flat", padx=15, pady=4).grid(row=0, column=0, padx=8)
    tk.Button(bf, text="إلغاء", command=do_cancel,
              bg="#555", fg="white", font=("Arial", 11),
              relief="flat", padx=15, pady=4).grid(row=0, column=1, padx=8)

    app.wait_window(diag)
    if not proceed[0]:
        return

    # ── فحص كل صف ──
    # بناء sets التطبيع مرة واحدة خارج الحلقة للسرعة
    male_norm_set   = {normalize_arabic(n) for n in MALE_NAMES}
    female_norm_set = {normalize_arabic(n) for n in FEMALE_NAMES}

    errors = []
    for idx, row in df.iterrows():
        name_val = str(row[ncol]).strip()
        if not name_val or name_val.lower() == "nan":
            continue
        first_name = get_first_arabic_name(name_val)
        gender_clean = clean_gender(row[gcol])

        if not first_name or gender_clean in ("nan", "", "none"):
            continue

        in_male   = first_name in male_norm_set
        in_female = first_name in female_norm_set

        if not in_male and not in_female:
            continue

        expected = None
        if in_male and not in_female:   expected = "ذكر"
        if in_female and not in_male:   expected = "أنثى"
        if expected is None:            continue

        # رقم الصف الحقيقي في Excel
        excel_row_num = int(row["__excel_row__"]) if "__excel_row__" in row.index else idx + 2

        if expected == "ذكر" and gender_clean in GENDER_VALUES_FEMALE:
            errors.append((excel_row_num, name_val, str(row[gcol]).strip(), "ذكر"))
        elif expected == "أنثى" and gender_clean in GENDER_VALUES_MALE:
            errors.append((excel_row_num, name_val, str(row[gcol]).strip(), "أنثى"))

    _show_gender_results(errors, ncol, gcol)


def _show_gender_results(errors, ncol, gcol):
    win = tk.Toplevel(app)
    win.title(f"نتائج فحص الجنس — {len(errors)} خطأ محتمل")
    win.geometry("720x500")
    win.configure(bg="#1e1e1e")

    tk.Label(win,
             text=f"✅ لا يوجد أخطاء في الجنس" if not errors else f"⚠️  {len(errors)} تعارض بين الاسم وعمود الجنس",
             bg="#1e1e1e",
             fg="#4CAF50" if not errors else "#FF9800",
             font=("Arial", 13, "bold")).pack(pady=12)

    if not errors:
        return

    # Treeview
    frame = tk.Frame(win, bg="#1e1e1e")
    frame.pack(fill="both", expand=True, padx=15, pady=(0,10))

    style2 = ttk.Style()
    style2.configure("G.Treeview",
        background="#1e1e1e", foreground="#ffffff",
        fieldbackground="#1e1e1e", rowheight=28,
        font=("Arial", 11))
    style2.configure("G.Treeview.Heading",
        background="#1f6aa5", foreground="white",
        font=("Arial", 11, "bold"))

    tv = ttk.Treeview(frame, style="G.Treeview",
                      columns=("row","name","current","expected"),
                      show="headings", selectmode="browse")
    tv.heading("row",      text="رقم الصف",   anchor="center")
    tv.heading("name",     text="الاسم",       anchor="e")
    tv.heading("current",  text="الجنس الحالي", anchor="center")
    tv.heading("expected", text="المتوقع",     anchor="center")
    tv.column("row",      width=80,  anchor="center", stretch=False)
    tv.column("name",     width=280, anchor="e")
    tv.column("current",  width=120, anchor="center", stretch=False)
    tv.column("expected", width=100, anchor="center", stretch=False)
    tv.tag_configure("err", foreground="#FF9800")

    vsb2 = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=vsb2.set)
    vsb2.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)

    for excel_row, name, current, expected in errors:
        tv.insert("", "end",
                  values=(f"صف {excel_row}", name, current, expected),
                  tags=("err",))

    # ── نسخ الاسم ──
    def copy_name_gender():
        sel = tv.selection()
        if not sel: return
        name_val = str(tv.item(sel[0])["values"][1])
        win.clipboard_clear()
        win.clipboard_append(name_val)

    def copy_row_gender():
        sel = tv.selection()
        if not sel: return
        vals = tv.item(sel[0])["values"]
        win.clipboard_clear()
        win.clipboard_append(f"{vals[0]} | {vals[1]} | {vals[2]} ← {vals[3]}")

    gender_menu = tk.Menu(win, tearoff=0, bg="#2b2b2b", fg="white",
                          activebackground="#1f6aa5", activeforeground="white",
                          font=("Arial", 11))
    gender_menu.add_command(label="📋  نسخ الاسم",    command=copy_name_gender)
    gender_menu.add_command(label="📋  نسخ السطر كاملاً", command=copy_row_gender)

    def show_gender_menu(event):
        row_id = tv.identify_row(event.y)
        if row_id: tv.selection_set(row_id)
        gender_menu.tk_popup(event.x_root, event.y_root)
        gender_menu.grab_release()

    tv.bind("<Button-3>", show_gender_menu)
    tv.bind("<Control-c>", lambda e: copy_name_gender())

    # زر تصدير
    def export_errors():
        out = pd.DataFrame(errors, columns=["رقم الصف", "الاسم", "الجنس الحالي", "الجنس المتوقع"])
        f = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                          filetypes=[("Excel","*.xlsx")])
        if f:
            out.to_excel(f, index=False)
            messagebox.showinfo("تم ✓", f"تم حفظ {len(errors)} خطأ في الملف")

    tk.Button(win, text="💾 تصدير الأخطاء كـ Excel", command=export_errors,
              bg="#2e7d32", fg="white", font=("Arial", 11),
              relief="flat", padx=15, pady=6).pack(pady=8)


def export_cleaned_text():
    """تصدير الملف بعد تنظيف كل الأحرف الخفية من كل الخلايا"""
    global df
    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return
    file = filedialog.asksaveasfilename(
        defaultextension=".xlsx",
        filetypes=[("Excel files", "*.xlsx")],
        title="حفظ الملف المنظّف"
    )
    if not file: return
    df_out = df.copy()
    if "__excel_row__" in df_out.columns:
        df_out = df_out.drop(columns=["__excel_row__"])
    for c in df_out.columns:
        if df_out[c].dtype == object:
            df_out[c] = df_out[c].apply(deep_clean_text)
    df_out.to_excel(file, index=False)
    messagebox.showinfo("تم ✓",
        f"تم تصدير الملف المنظّف ✓\n"
        f"الآن يمكنك البحث في Excel بشكل طبيعي\n"
        f"عدد الصفوف: {len(df_out)}")


def diagnose_cell():
    """تشخيص Unicode لخلية — يكشف الحروف الحقيقية"""
    import unicodedata
    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return

    # اجلب اسماً من الجدول أو من البحث
    sel = tree.selection()
    if sel:
        vals = tree.item(sel[0])["values"]
        sample = str(vals[1]) if vals and len(vals) > 1 else ""
    else:
        sample = search_var.get().strip()

    if not sample:
        messagebox.showinfo("تشخيص", "اختر صفاً في الجدول أو اكتب اسماً في البحث أولاً")
        return

    # ابحث عن الاسم في الـ df واجلب قيمته الخام
    col = name_column.get()
    raw_val = None
    norm_sample = normalize_arabic(sample)
    for _, row in df.iterrows():
        if normalize_arabic(str(row[col])) == norm_sample:
            raw_val = str(row[col])
            break

    if raw_val is None:
        raw_val = sample

    # بناء التقرير
    win = tk.Toplevel(app)
    win.title("تشخيص Unicode")
    win.geometry("620x460")
    win.configure(bg="#1e1e1e")

    tk.Label(win, text="تشخيص الحروف الحقيقية في الخلية",
             bg="#1e1e1e", fg="#64B5F6",
             font=("Courier New", 12, "bold")).pack(pady=10)

    # النص الخام
    tk.Label(win, text=f'النص: "{raw_val}"',
             bg="#1e1e1e", fg="white",
             font=("Arial", 12)).pack(pady=4)

    # جدول الحروف
    frame = tk.Frame(win, bg="#1e1e1e")
    frame.pack(fill="both", expand=True, padx=15, pady=5)

    tv = tk.Text(frame, bg="#1e1e1e", fg="#FFD600",
                 font=("Courier New", 11), wrap="word",
                 relief="flat", padx=10, pady=8)
    tv.pack(fill="both", expand=True)

    lines = []
    for i, ch in enumerate(raw_val):
        cat  = unicodedata.category(ch)
        name = unicodedata.name(ch, "UNKNOWN")
        cp   = f"U+{ord(ch):04X}"
        vis  = repr(ch) if cat.startswith("C") or cat.startswith("Z") else ch
        lines.append(f"[{i:3}]  {cp}  {cat:3}  {vis!r:8}  {name}")

    tv.insert("end", "\n".join(lines))
    tv.configure(state="disabled")

    # هل كل الحروف عربية؟
    arabic_range = range(0x0600, 0x06FF + 1)
    non_arabic   = [(i, ch) for i, ch in enumerate(raw_val)
                    if not ch.isspace() and ord(ch) not in arabic_range]

    if non_arabic:
        msg = f"⚠️  {len(non_arabic)} حرف خارج النطاق العربي!"
        color = "#FF9800"
    else:
        msg = "✅ كل الحروف ضمن النطاق العربي الصحيح"
        color = "#4CAF50"

    tk.Label(win, text=msg, bg="#1e1e1e", fg=color,
             font=("Arial", 11, "bold")).pack(pady=6)

    def fix_and_export():
        """إصلاح جذري — تحويل كل الحروف إلى Unicode NFC عربي نظيف"""
        import unicodedata as ud
        def full_normalize(val):
            if pd.isna(val): return val
            # NFC normalization — يوحد كل تمثيلات Unicode
            return ud.normalize("NFC", str(val))

        f = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                          filetypes=[("Excel","*.xlsx")])
        if not f: return
        df_out = df.copy()
        if "__excel_row__" in df_out.columns:
            df_out = df_out.drop(columns=["__excel_row__"])
        for c in df_out.columns:
            if df_out[c].dtype == object:
                df_out[c] = df_out[c].apply(full_normalize)
        df_out.to_excel(f, index=False)
        messagebox.showinfo("تم ✓", "تم تصدير الملف بعد NFC normalization\nجرّب البحث الآن في Excel")
        win.destroy()

    tk.Button(win, text="🔧 إصلاح وتصدير (NFC Normalize)",
              command=fix_and_export,
              bg="#1565c0", fg="white", font=("Arial", 11),
              relief="flat", padx=15, pady=6).pack(pady=8)

app = ctk.CTk()
app.title("Arabic Data Cleaner")
app.geometry("1200x780")
app.minsize(1000, 640)

# ══════════════════════════════════════════
# COLORS & FONTS
# ══════════════════════════════════════════
C = {
    "bg":        "#0f1117",
    "sidebar":   "#161b27",
    "panel":     "#1a1f2e",
    "card":      "#1e2438",
    "border":    "#2a3050",
    "accent":    "#4f8ef7",
    "accent2":   "#7c5ce8",
    "green":     "#00c896",
    "red":       "#ff4d6d",
    "orange":    "#ff9f43",
    "yellow":    "#ffd32a",
    "text":      "#e8eaf6",
    "muted":     "#6b7280",
    "header_bg": "#1a2035",
}

app.configure(fg_color=C["bg"])

# ══════════════════════════════════════════
# LAYOUT: sidebar (left) + main (right)
# ══════════════════════════════════════════
root_frame = ctk.CTkFrame(app, fg_color="transparent")
root_frame.pack(fill="both", expand=True)
root_frame.columnconfigure(1, weight=1)
root_frame.rowconfigure(0, weight=1)

# ── Sidebar ──
sidebar = ctk.CTkFrame(root_frame, fg_color=C["sidebar"], width=220, corner_radius=0)
sidebar.grid(row=0, column=0, sticky="nsew")
sidebar.grid_propagate(False)

# Logo area
logo_frame = ctk.CTkFrame(sidebar, fg_color=C["panel"], height=72, corner_radius=0)
logo_frame.pack(fill="x", pady=(0,2))
logo_frame.pack_propagate(False)
ctk.CTkLabel(logo_frame, text="⬡  DataCleaner",
             font=ctk.CTkFont("Courier New", 15, "bold"),
             text_color=C["accent"]).place(relx=0.5, rely=0.5, anchor="center")

def _sidebar_btn(text, cmd, color=C["accent"], icon=""):
    f = ctk.CTkFrame(sidebar, fg_color="transparent", height=48)
    f.pack(fill="x", padx=10, pady=3)
    f.pack_propagate(False)
    btn = ctk.CTkButton(f, text=f"  {icon}  {text}" if icon else f"  {text}",
                        command=cmd,
                        anchor="w",
                        fg_color="transparent",
                        hover_color=C["card"],
                        text_color=C["text"],
                        font=ctk.CTkFont(size=13),
                        border_width=1,
                        border_color=C["border"],
                        corner_radius=8,
                        height=40)
    btn.pack(fill="both", expand=True)
    return btn

ctk.CTkLabel(sidebar, text="ФАЙЛЫ", font=ctk.CTkFont("Courier New", 9),
             text_color=C["muted"]).pack(anchor="w", padx=18, pady=(14,2))
_sidebar_btn("تحميل Excel",   load_excel,  icon="📂")
_sidebar_btn("إنهاء الملف",   finish_file, icon="✔️")

ctk.CTkLabel(sidebar, text="АНАЛИЗ", font=ctk.CTkFont("Courier New", 9),
             text_color=C["muted"]).pack(anchor="w", padx=18, pady=(14,2))
analyze_button = _sidebar_btn("تحليل التكرارات",   run_analysis,          icon="🔍")
_sidebar_btn("أعمدة إضافية",       open_multicol_dialog,  icon="⚙️")
_sidebar_btn("فحص الجنس",          check_gender,          icon="👤")
_sidebar_btn("فحص أخطاء المرضى",   lambda: open_patient_error_dialog(), icon="🏥")

ctk.CTkLabel(sidebar, text="ЭКСПОРТ", font=ctk.CTkFont("Courier New", 9),
             text_color=C["muted"]).pack(anchor="w", padx=18, pady=(14,2))
_sidebar_btn("تصدير الملف النظيف",   export_clean,          icon="💾")
_sidebar_btn("تصدير بدون أحرف خفية", export_cleaned_text,   icon="🧹")
_sidebar_btn("تشخيص Unicode",         diagnose_cell,         icon="🔬")

# ── جلسات ──
ctk.CTkLabel(sidebar, text="СЕССИИ", font=ctk.CTkFont("Courier New", 9),
             text_color=C["muted"]).pack(anchor="w", padx=18, pady=(14,2))
_sidebar_btn("💾  حفظ الجلسة",  lambda: save_session(auto=False), icon="")

# ── History panel ──
ctk.CTkLabel(sidebar, text="ИСТОРИЯ", font=ctk.CTkFont("Courier New", 9),
             text_color=C["muted"]).pack(anchor="w", padx=18, pady=(10,2))

# إطار قابل للتمرير للجلسات
history_outer = ctk.CTkFrame(sidebar, fg_color="transparent")
history_outer.pack(fill="x", padx=6, pady=(0,4))

history_canvas = tk.Canvas(history_outer, bg=C["sidebar"],
                            highlightthickness=0, bd=0, height=220)
history_scroll = tk.Scrollbar(history_outer, orient="vertical",
                               command=history_canvas.yview)
history_canvas.configure(yscrollcommand=history_scroll.set)
history_canvas.pack(side="left", fill="both", expand=True)
history_scroll.pack(side="right", fill="y")

history_frame = tk.Frame(history_canvas, bg=C["sidebar"])
history_win   = history_canvas.create_window((0, 0), window=history_frame, anchor="nw")

def _on_history_configure(e):
    history_canvas.configure(scrollregion=history_canvas.bbox("all"))
    history_canvas.itemconfig(history_win, width=history_canvas.winfo_width())

history_frame.bind("<Configure>", _on_history_configure)
history_canvas.bind("<Configure>",
    lambda e: history_canvas.itemconfig(history_win, width=e.width))

def _history_scroll(e):
    history_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
history_canvas.bind("<MouseWheel>", _history_scroll)
history_frame.bind("<MouseWheel>",  _history_scroll)

# Shortcuts legend
legend = ctk.CTkFrame(sidebar, fg_color=C["card"], corner_radius=10)
legend.pack(fill="x", padx=10, pady=8)
for key, action in [("Enter","تأكيد مكرر"),("Delete","إزالة"),("Ctrl+Z","تراجع"),("Ctrl+S","حفظ جلسة")]:
    rf = ctk.CTkFrame(legend, fg_color="transparent")
    rf.pack(fill="x", padx=8, pady=1)
    ctk.CTkLabel(rf, text=key, font=ctk.CTkFont("Courier New", 10, "bold"),
                 text_color=C["accent"], width=60, anchor="w").pack(side="left")
    ctk.CTkLabel(rf, text=action, font=ctk.CTkFont(size=11),
                 text_color=C["muted"]).pack(side="left")

# ── Main area ──
main = ctk.CTkFrame(root_frame, fg_color=C["bg"], corner_radius=0)
main.grid(row=0, column=1, sticky="nsew", padx=0)
main.columnconfigure(0, weight=1)
main.rowconfigure(3, weight=1)   # treeview يأخذ المساحة

# ── Top bar ──
topbar = ctk.CTkFrame(main, fg_color=C["header_bg"], height=60, corner_radius=0)
topbar.grid(row=0, column=0, sticky="ew")
topbar.grid_propagate(False)

topbar.columnconfigure(1, weight=1)

# Column selector
col_frame = ctk.CTkFrame(topbar, fg_color="transparent")
col_frame.pack(side="left", padx=16, pady=10)
ctk.CTkLabel(col_frame, text="عمود الاسم", font=ctk.CTkFont(size=11),
             text_color=C["muted"]).pack(side="top", anchor="w")
name_column = ctk.CTkComboBox(col_frame, width=200, height=28,
                               fg_color=C["card"], border_color=C["border"],
                               button_color=C["accent"], dropdown_fg_color=C["panel"],
                               font=ctk.CTkFont(size=12))
name_column.pack(side="top")

# Extra cols label
multicol_label = ctk.CTkLabel(topbar, text="", font=ctk.CTkFont(size=11),
                               text_color=C["accent2"])
multicol_label.pack(side="left", padx=8)

# Status + confirm on right
right_bar = ctk.CTkFrame(topbar, fg_color="transparent")
right_bar.pack(side="right", padx=16)

confirm_label = ctk.CTkLabel(right_bar, text="",
                              font=ctk.CTkFont(size=13, weight="bold"),
                              text_color=C["green"])
confirm_label.pack(side="right", padx=8)

status_label = ctk.CTkLabel(right_bar, text="في انتظار تحميل ملف...",
                             font=ctk.CTkFont(size=11), text_color=C["muted"])
status_label.pack(side="right", padx=8)

# ── Search bar ──
search_bar = ctk.CTkFrame(main, fg_color=C["panel"], height=48, corner_radius=0)
search_bar.grid(row=1, column=0, sticky="ew")
search_bar.grid_propagate(False)
search_bar.columnconfigure(1, weight=1)

ctk.CTkLabel(search_bar, text="🔎", font=ctk.CTkFont(size=16),
             text_color=C["muted"]).grid(row=0, column=0, padx=(16,6), pady=8)
search_var = tk.StringVar()
search_entry = ctk.CTkEntry(search_bar, textvariable=search_var,
                             placeholder_text="ابحث عن اسم في الملف...",
                             fg_color=C["card"], border_color=C["border"],
                             text_color=C["text"],
                             font=ctk.CTkFont(size=13), height=32)
search_entry.grid(row=0, column=1, padx=6, pady=8, sticky="ew")
search_entry.bind("<Return>", lambda e: manual_search())

search_actions = ctk.CTkFrame(search_bar, fg_color="transparent")
search_actions.grid(row=0, column=2, padx=(4,12))

ctk.CTkButton(search_actions, text="بحث", command=manual_search,
              width=70, height=30, fg_color=C["accent"],
              hover_color="#3a7de0", font=ctk.CTkFont(size=12)).pack(side="left", padx=3)

back_button = ctk.CTkButton(search_actions, text="↩ رجوع",
              command=back_to_main, width=80, height=30,
              fg_color=C["card"], hover_color=C["border"],
              border_width=1, border_color=C["border"],
              text_color=C["muted"],
              font=ctk.CTkFont(size=12), state="disabled")
back_button.pack(side="left", padx=3)

# ── Review action bar ──
action_bar = ctk.CTkFrame(main, fg_color=C["card"], height=52, corner_radius=0)

# ── Filter bar ──
filter_bar = ctk.CTkFrame(main, fg_color=C["header_bg"], height=40, corner_radius=0)

# Re-layout: topbar=row0, action_bar=row1, search=row2, filter=row3, table=row4, context=row5
topbar.grid    (row=0, column=0, sticky="ew")
action_bar.grid(row=1, column=0, sticky="ew")
search_bar.grid(row=2, column=0, sticky="ew")
filter_bar.grid(row=3, column=0, sticky="ew")
main.rowconfigure(4, weight=1)

action_bar.grid_propagate(False)
filter_bar.grid_propagate(False)

def _action_btn(parent, text, cmd, fg, hv, width=150):
    return ctk.CTkButton(parent, text=text, command=cmd,
                         width=width, height=36,
                         fg_color=fg, hover_color=hv,
                         font=ctk.CTkFont(size=13, weight="bold"),
                         corner_radius=8)

ab_inner = ctk.CTkFrame(action_bar, fg_color="transparent")
ab_inner.pack(side="left", padx=12, pady=8)

_action_btn(ab_inner, "✅  مكرر — تأكيد",  mark_confirmed,    "#0d3320","#1a5c38").pack(side="left", padx=5)
_action_btn(ab_inner, "❌  مختلف — إزالة", mark_different,    "#3d0a14","#6b1525").pack(side="left", padx=5)
_action_btn(ab_inner, "☑  تأكيد الكل",    mark_all_confirmed,"#0d1f4a","#1a3a7a", width=120).pack(side="left", padx=5)

undo_button = ctk.CTkButton(ab_inner, text="↩ تراجع (0)",
              command=undo_last, width=110, height=36,
              fg_color=C["card"], hover_color=C["border"],
              border_width=1, border_color=C["border"],
              text_color=C["muted"],
              font=ctk.CTkFont(size=12), state="disabled",
              corner_radius=8)
undo_button.pack(side="left", padx=5)

# Progress on right of action bar
prog_frame = ctk.CTkFrame(action_bar, fg_color="transparent")
prog_frame.pack(side="right", padx=16, pady=8)
progress_label = ctk.CTkLabel(prog_frame, text="",
                               font=ctk.CTkFont(size=11), text_color=C["muted"], width=180, anchor="e")
progress_label.pack(side="top", anchor="e")
progress_bar = ctk.CTkProgressBar(prog_frame, width=220, height=6,
                                   fg_color=C["border"], progress_color=C["accent"])
progress_bar.set(0)
progress_bar.pack(side="top", anchor="e")

# ── Filter bar content ──
active_filter = tk.StringVar(value="all")

# الفلاتر العادية (وضع الاسم فقط)
FILTERS_NORMAL = [
    ("all",      "الكل",           C["muted"],   None),
    ("exact",    "100%  تطابق",    C["red"],     (100, 100)),
    ("high",     "95–99%",         C["orange"],  (95,  99)),
    ("med",      "90–94%",         C["yellow"],  (90,  94)),
    ("confirmed","مؤكدة فقط  ✅",  C["green"],   None),
]

# الفلاتر الطبية
FILTERS_MEDICAL = [
    ("all",      "الكل",           C["muted"],   None),
    ("exact",    "🔑 Patient ID",  C["green"],   (100, 100)),
    ("high",     "≥95%  دمج",      C["red"],     (95,  99)),
    ("med",      "80–94%  مراجعة", C["orange"],  (80,  94)),
    ("confirmed","مؤكدة  ✅",      C["green"],   None),
]

FILTERS = FILTERS_NORMAL   # يتبدّل حسب الوضع

filter_btns = {}

def _get_active_filters():
    return FILTERS_MEDICAL if medical_mode else FILTERS_NORMAL

def _rebuild_filter_buttons():
    """يعيد رسم أزرار الفلتر عند التبديل بين الوضعين"""
    for btn in filter_btns.values():
        try: btn.destroy()
        except Exception: pass
    filter_btns.clear()
    for key, label, color, _ in _get_active_filters():
        btn = ctk.CTkButton(filter_inner,
                            text=label,
                            width=100, height=26,
                            fg_color="transparent",
                            hover_color=C["card"],
                            text_color=color,
                            border_width=1,
                            border_color=color,
                            font=ctk.CTkFont(size=11),
                            corner_radius=6,
                            command=lambda k=key: apply_filter(k))
        btn.pack(side="left", padx=3)
        filter_btns[key] = btn
    apply_filter("all")

def apply_filter(key=None):
    global in_search_mode
    if key:
        active_filter.set(key)
    current = active_filter.get()
    active_filters = _get_active_filters()

    # تحديث شكل الأزرار
    for k, btn in filter_btns.items():
        is_active = (k == current)
        fdef = next((f for f in active_filters if f[0]==k), None)
        if not fdef: continue
        color = fdef[2]
        btn.configure(
            fg_color   = color if is_active else "transparent",
            text_color = C["bg"] if is_active else color,
            border_color = color,
        )

    if in_search_mode or not main_results:
        return

    # ── فلتر النتائج ──
    if current == "all":
        filtered = main_results
    elif current == "confirmed":
        filtered = [r for r in main_results if r[4]]
    else:
        rng = next((f[3] for f in active_filters if f[0]==current), None)
        if rng:
            lo, hi = rng
            filtered = []
            for r in main_results:
                try:
                    sc = float(r[0].replace("%",""))
                    # في الوضع الطبي: exact = Patient ID match (is_id_match=True)
                    if medical_mode and current == "exact":
                        is_id = r[6] if len(r) > 6 else False
                        if is_id: filtered.append(r)
                    elif lo <= sc <= hi:
                        filtered.append(r)
                except Exception:
                    pass
        else:
            filtered = main_results

    # ── امسح وأعد الرسم ──
    children = tree.get_children()
    if children:
        tree.delete(*children)
    confirmed_duplicates.clear()

    tree.configure(displaycolumns=())
    for r in filtered:
        score_str, n1, n2, tag = r[0], r[1], r[2], r[3]
        was_confirmed = r[4]
        iid = tree.insert("", "end", values=(score_str, n1, n2), tags=(tag,))
        if was_confirmed:
            confirmed_duplicates.add(iid)
    tree.configure(displaycolumns=("score","name1","name2"))

    total = len(main_results)
    shown = len(filtered)
    if current == "all":
        filter_count_label.configure(text=f"عرض: {shown} من {total}")
    else:
        filter_count_label.configure(text=f"فلتر: {shown} / {total}")

filter_inner = ctk.CTkFrame(filter_bar, fg_color="transparent")
filter_inner.pack(side="left", padx=10, pady=5)

ctk.CTkLabel(filter_inner, text="فلتر:",
             font=ctk.CTkFont(size=11), text_color=C["muted"]).pack(side="left", padx=(0,6))

for key, label, color, _ in FILTERS_NORMAL:
    btn = ctk.CTkButton(filter_inner,
                        text=label,
                        width=100, height=26,
                        fg_color="transparent",
                        hover_color=C["card"],
                        text_color=color,
                        border_width=1,
                        border_color=color,
                        font=ctk.CTkFont(size=11),
                        corner_radius=6,
                        command=lambda k=key: apply_filter(k))
    btn.pack(side="left", padx=3)
    filter_btns[key] = btn

# عداد النتائج على اليمين
filter_count_label = ctk.CTkLabel(filter_bar, text="",
                                   font=ctk.CTkFont(size=11),
                                   text_color=C["muted"])
filter_count_label.pack(side="right", padx=16)

# تفعيل "الكل" بشكل افتراضي
apply_filter("all")

# ── Treeview ──
tf = ctk.CTkFrame(main, fg_color=C["bg"], corner_radius=0)
tf.grid(row=4, column=0, sticky="nsew", padx=0, pady=0)
tf.columnconfigure(0, weight=1)
tf.rowconfigure(0, weight=1)

style = ttk.Style()
style.theme_use("clam")
style.configure("A.Treeview",
    background=C["panel"], foreground=C["text"],
    fieldbackground=C["panel"], rowheight=34,
    font=("Tahoma", 12), borderwidth=0,
    relief="flat")
style.configure("A.Treeview.Heading",
    background=C["header_bg"], foreground=C["muted"],
    font=("Tahoma", 11, "bold"), relief="flat",
    borderwidth=0)
style.map("A.Treeview",
    background=[("selected", C["accent"] + "33")],
    foreground=[("selected", C["text"])])
style.layout("A.Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])

tree = ttk.Treeview(tf, style="A.Treeview",
                    columns=("score","name1","name2"),
                    show="headings", selectmode="browse")

tree.heading("score", text="التشابه", anchor="center")
tree.heading("name1", text="الاسم الأول",  anchor="e")
tree.heading("name2", text="الاسم الثاني", anchor="e")
tree.column("score", width=90,  minwidth=60,  anchor="center", stretch=False)
tree.column("name1", width=460, minwidth=150, anchor="e")
tree.column("name2", width=460, minwidth=150, anchor="e")

tree.tag_configure("exact",     foreground=C["red"])
tree.tag_configure("high",      foreground=C["orange"])
tree.tag_configure("med",       foreground=C["yellow"])
tree.tag_configure("confirmed", foreground=C["green"],  background="#0d2e1f")
tree.tag_configure("search",    foreground=C["accent"])
tree.tag_configure("odd",       background=C["card"])
tree.tag_configure("even",      background=C["panel"])

vsb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
style.configure("Vertical.TScrollbar",
    background=C["border"], troughcolor=C["panel"],
    arrowcolor=C["muted"], relief="flat", borderwidth=0)
tree.configure(yscrollcommand=vsb.set)
vsb.grid(row=0, column=1, sticky="ns")
tree.grid(row=0, column=0, sticky="nsew")

# ── Shortcuts ──
app.bind("<Return>",    lambda e: mark_confirmed())
app.bind("<Delete>",    lambda e: mark_different())
app.bind("<Control-z>", lambda e: undo_last())

# ── اختصارات Clipboard عالمية لكل CTkEntry في البرنامج ──
def _bind_clipboard(widget):
    """يربط Ctrl+C/X/V/A على الـ internal tk.Entry داخل CTkEntry"""
    try:
        inner = widget._entry   # CTkEntry
    except AttributeError:
        inner = widget          # tk.Entry عادي

    def _copy(e):
        try:
            txt = inner.selection_get()
        except:
            txt = inner.get()
        app.clipboard_clear(); app.clipboard_append(txt)
        return "break"

    def _cut(e):
        try:
            txt = inner.selection_get()
            inner.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except:
            txt = inner.get(); inner.delete(0, "end")
        app.clipboard_clear(); app.clipboard_append(txt)
        return "break"

    def _paste(e):
        try:
            txt = app.clipboard_get()
            try:   inner.delete(tk.SEL_FIRST, tk.SEL_LAST)
            except: pass
            inner.insert(tk.INSERT, txt)
        except: pass
        return "break"

    def _select_all(e):
        inner.select_range(0, "end"); inner.icursor("end")
        return "break"

    inner.bind("<Control-c>", _copy,       add=False)
    inner.bind("<Control-x>", _cut,        add=False)
    inner.bind("<Control-v>", _paste,      add=False)
    inner.bind("<Control-a>", _select_all, add=False)

# طبّق على كل CTkEntry موجودة
_bind_clipboard(search_entry)

# طبّق تلقائياً على كل tk.Entry يُنشأ مستقبلاً (في النوافذ المنبثقة)
_orig_entry_init = tk.Entry.__init__
def _patched_entry_init(self, *args, **kwargs):
    _orig_entry_init(self, *args, **kwargs)
    _bind_clipboard(self)
tk.Entry.__init__ = _patched_entry_init

# ── Context Menu ──
context_menu = tk.Menu(app, tearoff=0,
                       bg=C["card"], fg=C["text"],
                       activebackground=C["accent"],
                       activeforeground="white",
                       font=("Tahoma", 11),
                       bd=0, relief="flat")
context_menu.add_command(label="✅  مكرر — تأكيد",                  command=mark_confirmed)
context_menu.add_command(label="❌  مختلف — إزالة من القائمة",      command=mark_different)
context_menu.add_command(label="↩️  تراجع   Ctrl+Z",                command=undo_last)
context_menu.add_separator()
context_menu.add_command(label="📋  نسخ السطر",                     command=copy_row)
context_menu.add_command(label="     نسخ الاسم الأول",              command=copy_name1)
context_menu.add_command(label="     نسخ الاسم الثاني",             command=copy_name2)
context_menu.add_separator()
context_menu.add_command(label="🔍  نسخ الاسم الأول — نظيف للبحث في Excel",  command=copy_name1_clean)
context_menu.add_command(label="🔍  نسخ الاسم الثاني — نظيف للبحث في Excel", command=copy_name2_clean)

tree.bind("<Button-3>", show_context_menu)
tree.bind("<Control-c>", copy_row)

# ── Tooltip للوضع الطبي عند hover ──
_tooltip_win  = None
_tooltip_after = None

def _tree_hover(event):
    global _tooltip_win, _tooltip_after
    if not medical_mode:
        return
    row_id = tree.identify_row(event.y)
    if not row_id:
        _hide_tooltip()
        return
    vals = tree.item(row_id)["values"]
    if not vals:
        return
    n1_h = str(vals[1]); n2_h = str(vals[2])
    record = None
    for r in main_results:
        if str(r[1]) == n1_h and str(r[2]) == n2_h:
            record = r; break
    if not record or len(record) < 6:
        return

    try:
        details = json.loads(record[5])
    except Exception:
        return

    # بناء نص الـ tooltip
    lines = []
    for d in details:
        sim = float(d.get("sim",0))
        bar = "█" * int(sim // 10) + "░" * (10 - int(sim // 10))
        lines.append(f"{d['col'][:14]:14}  {bar}  {sim:.0f}%")
    if not lines:
        return

    tooltip_text = "\n".join(lines)

    def _show():
        global _tooltip_win
        _hide_tooltip()
        tw = tk.Toplevel(app)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{event.x_root+14}+{event.y_root+10}")
        tw.configure(bg=C["card"])
        tk.Frame(tw, bg=C["accent"], height=2).pack(fill="x")
        tk.Label(tw, text="تفصيل التطابق  (double-click للمزيد)",
                 bg=C["card"], fg=C["accent"],
                 font=("Tahoma", 8, "bold"),
                 padx=10, pady=3).pack(anchor="e")
        tk.Label(tw, text=tooltip_text,
                 bg=C["card"], fg=C["text"],
                 font=("Courier New", 9),
                 padx=10, pady=4, justify="left").pack()
        _tooltip_win = tw

    global _tooltip_after
    if _tooltip_after:
        app.after_cancel(_tooltip_after)
    _tooltip_after = app.after(600, _show)   # تأخير 600ms

def _hide_tooltip(event=None):
    global _tooltip_win, _tooltip_after
    if _tooltip_after:
        app.after_cancel(_tooltip_after)
        _tooltip_after = None
    if _tooltip_win:
        try: _tooltip_win.destroy()
        except Exception: pass
        _tooltip_win = None

tree.bind("<Motion>",  _tree_hover)
tree.bind("<Leave>",   _hide_tooltip)
tree.bind("<Button-1>",_hide_tooltip)

def show_medical_details(event=None):
    """يعرض تفاصيل المطابقة الطبية عند double-click"""
    if not medical_mode:
        return
    selected = tree.selection()
    if not selected:
        return
    iid  = selected[0]
    vals = tree.item(iid)["values"]
    if not vals:
        return

    # ابحث عن التفاصيل في main_results
    n1_tree = str(vals[1])
    n2_tree = str(vals[2])
    record  = None
    for r in main_results:
        if str(r[1]) == n1_tree and str(r[2]) == n2_tree:
            record = r
            break
    if not record or len(record) < 8:
        return

    score_str    = record[0]
    details_json = record[5] if len(record) > 5 else "[]"
    is_id_match  = record[6] if len(record) > 6 else False
    row1_num     = record[7] if len(record) > 7 else "—"
    row2_num     = record[8] if len(record) > 8 else "—"

    try:
        details = json.loads(details_json)
    except Exception:
        details = []

    # ── نافذة التفاصيل ──
    win = tk.Toplevel(app)
    win.title("تفاصيل التطابق الطبي")
    win.geometry("560x520")
    win.configure(bg=C["panel"])
    win.grab_set()

    # ── تحديد مستوى الثقة ──
    try:
        score_val = float(score_str.replace("%",""))
    except Exception:
        score_val = 0.0

    if is_id_match:
        confidence_text  = "✅  نفس المريض بالتأكيد  (Patient ID متطابق)"
        confidence_color = C["green"]
        hdr_color        = C["green"]
    elif score_val >= 95:
        confidence_text  = "🔴  احتمال كبير جداً — يُنصح بالدمج"
        confidence_color = C["red"]
        hdr_color        = C["red"]
    elif score_val >= 85:
        confidence_text  = "🟡  احتمال متوسط — يحتاج مراجعة"
        confidence_color = C["orange"]
        hdr_color        = C["orange"]
    else:
        confidence_text  = "🔵  احتمال منخفض — راجع بعناية"
        confidence_color = C["accent"]
        hdr_color        = C["accent"]

    # Header شريط ملون
    tk.Frame(win, bg=hdr_color, height=5).pack(fill="x")

    tk.Label(win, text=f"⚕️  نسبة التطابق الإجمالية:  {score_str}",
             bg=C["panel"], fg=hdr_color,
             font=("Tahoma", 15, "bold")).pack(pady=(14,2))
    tk.Label(win, text=confidence_text,
             bg=C["panel"], fg=confidence_color,
             font=("Tahoma", 11)).pack(pady=(0,8))

    # الأسماء في بطاقتين
    names_f = tk.Frame(win, bg=C["bg"], padx=0, pady=0)
    names_f.pack(fill="x", padx=20, pady=(0,8))
    names_f.columnconfigure(0, weight=1)
    names_f.columnconfigure(1, weight=1)

    for col_idx, (name, color, row_n) in enumerate([
        (n1_tree, C["orange"], row1_num),
        (n2_tree, C["accent"], row2_num),
    ]):
        card = tk.Frame(names_f, bg=C["card"], padx=10, pady=8)
        card.grid(row=0, column=col_idx, sticky="ew", padx=(0 if col_idx else 0, 4 if col_idx==0 else 0))
        tk.Label(card, text=f"صف Excel: {row_n}", bg=C["card"],
                 fg=C["muted"], font=("Tahoma", 8)).pack(anchor="e")
        tk.Label(card, text=name, bg=C["card"], fg=color,
                 font=("Tahoma", 11, "bold"), wraplength=220,
                 justify="right", anchor="e").pack(anchor="e")

    # ── تفاصيل الأعمدة ──
    if details:
        tk.Label(win, text="تفصيل التطابق لكل عمود:",
                 bg=C["panel"], fg=C["muted"],
                 font=("Tahoma", 10, "bold")).pack(anchor="e", padx=22, pady=(4,2))

        for d in details:
            col_name  = d.get("col","")
            ctype     = d.get("type","text")
            sim       = float(d.get("sim", 0))
            weight    = d.get("weight", 0)
            weighted_contrib = sim * weight / 100

            bar_color  = (C["green"]  if sim >= 85 else
                          C["orange"] if sim >= 55 else C["red"])
            type_color = TYPE_COLORS.get(ctype, C["muted"])
            type_lbl   = TYPE_LABELS.get(ctype, ctype)

            row_f = tk.Frame(win, bg=C["card"], padx=10, pady=5)
            row_f.pack(fill="x", padx=20, pady=2)

            # اسم العمود + نوعه
            info = tk.Frame(row_f, bg=C["card"])
            info.pack(side="right", padx=6)
            tk.Label(info, text=col_name, bg=C["card"], fg=C["text"],
                     font=("Tahoma", 10, "bold"), anchor="e").pack(anchor="e")
            tk.Label(info, text=type_lbl, bg=C["card"], fg=type_color,
                     font=("Tahoma", 8)).pack(anchor="e")

            # شريط + نسبة
            bar_area = tk.Frame(row_f, bg=C["card"])
            bar_area.pack(side="left", fill="x", expand=True)

            bar_outer = tk.Frame(bar_area, bg=C["border"], height=10, width=200)
            bar_outer.pack(side="left", padx=4)
            bar_outer.pack_propagate(False)
            tk.Frame(bar_outer, bg=bar_color,
                     height=10, width=max(2, int(sim*2))
                     ).place(x=0, y=0)

            tk.Label(bar_area, text=f"{sim:.0f}%",
                     bg=C["card"], fg=bar_color,
                     font=("Courier New", 10, "bold")).pack(side="left", padx=4)
            tk.Label(bar_area, text=f"(وزن {weight}%  →  {weighted_contrib:.1f}نقطة)",
                     bg=C["card"], fg=C["muted"],
                     font=("Tahoma", 8)).pack(side="left")

    # أزرار
    btns = tk.Frame(win, bg=C["panel"])
    btns.pack(pady=16)

    def copy_pid_action():
        """نسخ Patient ID من السجل الأول"""
        if not medical_pid_col or df is None:
            return
        col_v = name_column.get()
        match = df[df[col_v].apply(lambda x: str(x).strip()) == n1_tree]
        if not match.empty:
            pid = str(match.iloc[0].get(medical_pid_col, ""))
            app.clipboard_clear()
            app.clipboard_append(pid)
            set_status(f"✓ تم نسخ Patient ID: {pid}", C["green"])
        win.destroy()

    if medical_pid_col:
        ctk.CTkButton(btns, text="🔑  نسخ Patient ID للدمج",
                      command=copy_pid_action,
                      width=200, height=36,
                      fg_color=C["green"], hover_color="#009e78",
                      font=ctk.CTkFont(size=12),
                      corner_radius=8).pack(side="left", padx=8)

    ctk.CTkButton(btns, text="✅  تأكيد مكرر",
                  command=lambda: [win.destroy(), mark_confirmed()],
                  width=130, height=36,
                  fg_color="#0d3320", hover_color="#1a5c38",
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(side="left", padx=4)

    ctk.CTkButton(btns, text="❌  مختلف",
                  command=lambda: [win.destroy(), mark_different()],
                  width=100, height=36,
                  fg_color="#3d0a14", hover_color="#6b1525",
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(side="left", padx=4)

    ctk.CTkButton(btns, text="إغلاق",
                  command=win.destroy,
                  width=80, height=36,
                  fg_color="transparent", hover_color=C["card"],
                  border_width=1, border_color=C["border"],
                  text_color=C["muted"],
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(side="left", padx=4)


# ─────────────────────────────────────────
# Patient Error Detection
# ─────────────────────────────────────────

def open_patient_error_dialog():
    """نافذة إعداد فحص أخطاء المرضى"""
    if df is None:
        messagebox.showwarning("تنبيه", "حمّل ملف Excel أولاً")
        return

    cols = [c for c in df.columns if c != "__excel_row__"]

    win = tk.Toplevel(app)
    win.title("إعداد فحص أخطاء المرضى")
    win.geometry("440x420")
    win.configure(bg=C["panel"])
    win.grab_set()
    win.resizable(False, False)

    tk.Label(win, text="🏥  فحص أخطاء بيانات المرضى",
             bg=C["panel"], fg=C["accent"],
             font=("Tahoma", 14, "bold")).pack(pady=(18, 4))
    tk.Label(win,
             text="يكشف عن تناقضات بين الاسم وتاريخ الميلاد والجنس",
             bg=C["panel"], fg=C["muted"],
             font=("Tahoma", 10)).pack(pady=(0, 14))

    frame = tk.Frame(win, bg=C["card"], padx=20, pady=14)
    frame.pack(fill="x", padx=20)

    def _make_row(label_text, auto_hints, default=""):
        tk.Label(frame, text=label_text, bg=C["card"], fg=C["muted"],
                 font=("Tahoma", 10)).pack(anchor="e")
        var = tk.StringVar()
        val = default
        if not val:
            for c in cols:
                if any(h in c.lower() for h in auto_hints):
                    val = c; break
        var.set(val)
        cb = ttk.Combobox(frame, textvariable=var,
                          values=["(لا شيء)"] + cols,
                          width=28, state="readonly")
        cb.pack(pady=(2, 10))
        return var

    name_var   = _make_row("عمود الاسم:",           ["اسم","name"],                  default=name_column.get())
    birth_var  = _make_row("عمود تاريخ الميلاد:",   ["تاريخ","birth","dob","ميلاد"])
    gender_var = _make_row("عمود الجنس:",            ["جنس","gender","sex"])
    id_var     = _make_row("عمود رقم التعريف (ID):", ["id","رقم","كود","code","patient","مريض","هوية"])

    def run():
        ncol = name_var.get()
        bcol = birth_var.get()  if birth_var.get()  not in ("(لا شيء)", "") else None
        gcol = gender_var.get() if gender_var.get() not in ("(لا شيء)", "") else None
        icol = id_var.get()     if id_var.get()     not in ("(لا شيء)", "") else None
        if not ncol:
            messagebox.showwarning("تنبيه", "اختر عمود الاسم")
            return
        win.destroy()
        _run_patient_error_detection(ncol, bcol, gcol, icol)

    ctk.CTkButton(win, text="🔍  بدء الفحص", command=run,
                  width=390, height=42,
                  fg_color=C["accent"], hover_color="#3a7de0",
                  font=ctk.CTkFont(size=13, weight="bold"),
                  corner_radius=8).pack(padx=20, pady=(12, 6))

    ctk.CTkButton(win, text="إلغاء", command=win.destroy,
                  width=390, height=30,
                  fg_color="transparent", hover_color=C["card"],
                  border_width=1, border_color=C["border"],
                  text_color=C["muted"],
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(padx=20)


def _run_patient_error_detection(name_col_r, birth_col_r, gender_col_r, id_col_r=None):
    analyze_button.configure(state="disabled", text="جاري الفحص...")
    reset_progress()
    clear_table()

    def _worker():
        def pcb(v, t):
            app.after(0, lambda vv=v, tt=t: set_progress(vv, tt))

        results_df = detect_patient_errors(
            df, name_col_r, birth_col_r, gender_col_r,
            id_col=id_col_r,
            progress_callback=pcb,
        )

        def _update():
            global main_results, in_search_mode, main_results_index
            clear_table()
            main_results       = []
            main_results_index = {}
            in_search_mode     = False
            back_button.configure(state="disabled")

            tree.configure(displaycolumns=())
            for i, row in enumerate(results_df.values):
                n1    = str(row[2])   # Name1 (col index 2 after RowNum1,ID1)
                n2    = str(row[7])   # Name2 (col index 7 after RowNum2,ID2)
                sim   = float(row[10])
                atype = str(row[11])

                if atype == "خطأ_بيانات":
                    tag = "exact"
                    score_str = f"⚠ {sim:.0f}%"
                else:
                    tag = "high"
                    score_str = f"{sim:.0f}%"

                tree.insert("", "end", values=(score_str, n1, n2), tags=(tag,))
                main_results.append((score_str, n1, n2, tag, False))
                main_results_index[(n1, n2)] = i

            tree.configure(displaycolumns=("score","name1","name2"))

            n_err  = (results_df["AlertType"] == "خطأ_بيانات").sum()
            n_name = (results_df["AlertType"] == "خطأ_إدخال_اسم").sum()
            msg = (f"🏥 فحص أخطاء المرضى: {len(results_df)} حالة  |  "
                   f"⚠ خطأ بيانات: {n_err}  |  ✏ خطأ إدخال اسم: {n_name}")
            set_status(msg, C["orange"] if n_err else C["accent"])
            set_progress(1.0, f"اكتمل — {len(results_df)} حالة")
            analyze_button.configure(state="normal", text="🔍 Analyze")
            _rebuild_filter_buttons()
            active_filter.set("all")
            apply_filter("all")

            if not results_df.empty:
                _show_patient_error_report(results_df)

        app.after(0, _update)

    threading.Thread(target=_worker, daemon=True).start()


def _show_patient_error_report(results_df):
    n_err  = (results_df["AlertType"] == "خطأ_بيانات").sum()
    n_name = (results_df["AlertType"] == "خطأ_إدخال_اسم").sum()

    win = tk.Toplevel(app)
    win.title(f"تقرير أخطاء المرضى — {len(results_df)} حالة")
    win.geometry("1100x580")
    win.configure(bg=C["panel"])

    tk.Frame(win, bg=C["accent"], height=4).pack(fill="x")
    tk.Label(win, text="🏥  تقرير فحص أخطاء بيانات المرضى",
             bg=C["panel"], fg=C["accent"],
             font=("Tahoma", 13, "bold")).pack(pady=(10, 2))

    stats_f = tk.Frame(win, bg=C["card"])
    stats_f.pack(fill="x", padx=20, pady=6)
    for label, val, col in [
        ("إجمالي الحالات",       str(len(results_df)), C["text"]),
        ("⚠  خطأ في البيانات",  str(n_err),           C["red"]),
        ("✏  خطأ إدخال الاسم", str(n_name),           C["orange"]),
    ]:
        f = tk.Frame(stats_f, bg=C["card"])
        f.pack(side="left", padx=20, pady=8)
        tk.Label(f, text=val,   bg=C["card"], fg=col,
                 font=("Tahoma", 18, "bold")).pack()
        tk.Label(f, text=label, bg=C["card"], fg=C["muted"],
                 font=("Tahoma", 9)).pack()

    tf2 = tk.Frame(win, bg=C["panel"])
    tf2.pack(fill="both", expand=True, padx=20, pady=(0,6))

    style3 = ttk.Style()
    style3.configure("PE.Treeview",
        background=C["panel"], foreground=C["text"],
        fieldbackground=C["panel"], rowheight=30,
        font=("Tahoma", 11))
    style3.configure("PE.Treeview.Heading",
        background=C["header_bg"], foreground=C["muted"],
        font=("Tahoma", 10, "bold"))

    tv = ttk.Treeview(tf2, style="PE.Treeview",
                      columns=("row1","pid1","n1","row2","pid2","n2","sim","type","reason"),
                      show="headings", selectmode="browse")
    for col_id, heading, width, anchor in [
        ("row1", "صف",            45,  "center"),
        ("pid1", "ID 1",          80,  "center"),
        ("n1",   "الاسم الأول",  190, "e"),
        ("row2", "صف",            45,  "center"),
        ("pid2", "ID 2",          80,  "center"),
        ("n2",   "الاسم الثاني", 190, "e"),
        ("sim",  "التشابه",       65,  "center"),
        ("type", "النوع",         110, "center"),
        ("reason","السبب",        180, "e"),
    ]:
        tv.heading(col_id, text=heading, anchor=anchor)
        tv.column(col_id,  width=width,  anchor=anchor, stretch=(col_id in ("n1","n2","reason")))

    tv.tag_configure("err",  foreground=C["red"])
    tv.tag_configure("name", foreground=C["orange"])

    vsb3 = ttk.Scrollbar(tf2, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=vsb3.set)
    vsb3.pack(side="right", fill="y")
    tv.pack(side="left", fill="both", expand=True)

    # ── قائمة النسخ ──
    report_menu = tk.Menu(win, tearoff=0,
                          bg=C["card"], fg=C["text"],
                          activebackground=C["accent"],
                          activeforeground="white",
                          font=("Tahoma", 11))

    def _copy_cell(col_idx):
        sel = tv.selection()
        if not sel: return
        val = str(tv.item(sel[0])["values"][col_idx])
        win.clipboard_clear(); win.clipboard_append(val)

    def _copy_row_rep():
        sel = tv.selection()
        if not sel: return
        vals = tv.item(sel[0])["values"]
        text = f"صف {vals[0]} | ID:{vals[1]} | {vals[2]}  ↔  صف {vals[3]} | ID:{vals[4]} | {vals[5]}  |  {vals[6]}  |  {vals[8]}"
        win.clipboard_clear(); win.clipboard_append(text)

    report_menu.add_command(label="📋  نسخ الاسم الأول",  command=lambda: _copy_cell(2))
    report_menu.add_command(label="📋  نسخ الاسم الثاني", command=lambda: _copy_cell(5))
    report_menu.add_command(label="📋  نسخ السطر كاملاً", command=_copy_row_rep)
    report_menu.add_separator()
    report_menu.add_command(label="📋  نسخ ID الأول",      command=lambda: _copy_cell(1))
    report_menu.add_command(label="📋  نسخ ID الثاني",     command=lambda: _copy_cell(4))
    report_menu.add_command(label="📋  نسخ رقم الصف 1",   command=lambda: _copy_cell(0))
    report_menu.add_command(label="📋  نسخ رقم الصف 2",   command=lambda: _copy_cell(3))
    report_menu.add_command(label="📋  نسخ السبب",         command=lambda: _copy_cell(8))

    def _show_report_menu(event):
        row_id = tv.identify_row(event.y)
        if row_id: tv.selection_set(row_id)
        report_menu.tk_popup(event.x_root, event.y_root)
        report_menu.grab_release()

    tv.bind("<Button-3>", _show_report_menu)
    tv.bind("<Control-c>", lambda e: _copy_row_rep())

    for _, row in results_df.iterrows():
        tag = "err" if row["AlertType"] == "خطأ_بيانات" else "name"
        tv.insert("", "end",
                  values=(row["RowNum1"], row["ID1"], row["Name1"],
                          row["RowNum2"], row["ID2"], row["Name2"],
                          f"{row['NameSimilarity']:.0f}%",
                          row["AlertType"], row["Reason"]),
                  tags=(tag,))

    btn_f = tk.Frame(win, bg=C["panel"])
    btn_f.pack(pady=8)

    def export_report():
        f = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                          filetypes=[("Excel","*.xlsx")])
        if not f: return
        results_df.to_excel(f, index=False)
        messagebox.showinfo("تم ✓", f"تم حفظ {len(results_df)} حالة في الملف")

    ctk.CTkButton(btn_f, text="💾  تصدير Excel", command=export_report,
                  width=180, height=36,
                  fg_color=C["accent"], hover_color="#3a7de0",
                  font=ctk.CTkFont(size=12), corner_radius=8).pack(side="left", padx=8)

    ctk.CTkButton(btn_f, text="إغلاق", command=win.destroy,
                  width=90, height=36,
                  fg_color="transparent", hover_color=C["card"],
                  border_width=1, border_color=C["border"],
                  text_color=C["muted"],
                  font=ctk.CTkFont(size=12), corner_radius=8).pack(side="left", padx=4)


# ── re-add the double-click bind (after show_medical_details is defined above) ──
tree.bind("<Double-1>", show_medical_details)

# Right-click context menu for search entry
def search_copy():
    try:
        text = search_entry.selection_get()
        app.clipboard_clear(); app.clipboard_append(text)
    except Exception:
        app.clipboard_clear(); app.clipboard_append(search_var.get())

def search_paste():
    try:
        text = app.clipboard_get()
        widget = search_entry._entry
        try: widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except Exception: pass
        widget.insert(tk.INSERT, text)
    except Exception: pass

def search_cut():
    search_copy()
    try: search_entry._entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
    except Exception: search_var.set("")

def search_select_all():
    search_entry._entry.select_range(0, "end")
    search_entry._entry.icursor("end")

def search_clear():
    search_var.set("")

search_menu = tk.Menu(app, tearoff=0, bg=C["card"], fg=C["text"],
                      activebackground=C["accent"], activeforeground="white",
                      font=("Tahoma", 11))
search_menu.add_command(label="📋  نسخ",        command=search_copy)
search_menu.add_command(label="✂️   قص",         command=search_cut)
search_menu.add_command(label="📌  لصق",        command=search_paste)
search_menu.add_separator()
search_menu.add_command(label="🔲  تحديد الكل", command=search_select_all)
search_menu.add_command(label="🗑️   مسح الكل",  command=search_clear)

def show_search_menu(event):
    search_menu.tk_popup(event.x_root, event.y_root)
    search_menu.grab_release()

search_entry.bind("<Button-3>", show_search_menu)

# ── Ctrl+S لحفظ الجلسة ──
app.bind("<Control-s>", lambda e: save_session(auto=False))

# ── حفظ تلقائي عند الإغلاق ──
def _on_close():
    if has_unsaved_changes and main_results:
        ans = messagebox.askyesnocancel(
            "حفظ الجلسة؟",
            "توجد تغييرات غير محفوظة.\n\nهل تريد حفظ الجلسة قبل الإغلاق؟",
            icon="warning"
        )
        if ans is None:       # Cancel — لا تغلق
            return
        elif ans:             # Yes — احفظ ثم أغلق
            save_session(auto=False)
    app.destroy()
app.protocol("WM_DELETE_WINDOW", _on_close)

# ── تحميل الـ history عند البدء ──
refresh_history_panel()

# ── حفظ تلقائي كل 3 دقائق ──
app.after(180_000, auto_save)

# ══════════════════════════════════════════
# LICENSE CHECK — يعمل عند أول تشغيل
# ══════════════════════════════════════════

def _show_license_window():
    """نافذة الترخيص — تُغلق البرنامج إذا لم يُدخَل كود صحيح"""
    mid = get_machine_id()

    win = tk.Toplevel(app)
    win.title("تفعيل البرنامج")
    win.geometry("520x420")
    win.configure(bg=C["panel"])
    win.grab_set()
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", app.destroy)   # إغلاق النافذة = إغلاق البرنامج

    # ── تمركز في المنتصف ──
    win.update_idletasks()
    x = (win.winfo_screenwidth()  - 520) // 2
    y = (win.winfo_screenheight() - 420) // 2
    win.geometry(f"520x420+{x}+{y}")

    # ── Header ──
    hdr = tk.Frame(win, bg=C["accent"], height=6)
    hdr.pack(fill="x")

    tk.Label(win, text="⬡  Arabic Data Cleaner",
             bg=C["panel"], fg=C["accent"],
             font=("Courier New", 16, "bold")).pack(pady=(22, 2))
    tk.Label(win, text="يرجى إدخال كود الترخيص لتفعيل البرنامج",
             bg=C["panel"], fg=C["muted"],
             font=("Tahoma", 11)).pack(pady=(0, 16))

    # ── Machine ID ──
    mid_frame = tk.Frame(win, bg=C["card"], padx=16, pady=12)
    mid_frame.pack(fill="x", padx=24)

    tk.Label(mid_frame, text="Machine ID  (أرسله لنا للحصول على كودك):",
             bg=C["card"], fg=C["muted"],
             font=("Tahoma", 10)).pack(anchor="w")

    mid_row = tk.Frame(mid_frame, bg=C["card"])
    mid_row.pack(fill="x", pady=(4, 0))

    mid_var = tk.StringVar(value=mid)
    mid_entry = tk.Entry(mid_row, textvariable=mid_var,
                         state="readonly", readonlybackground=C["card"],
                         fg=C["accent"], font=("Courier New", 13, "bold"),
                         relief="flat", bd=0, width=28)
    mid_entry.pack(side="left")

    def copy_mid():
        win.clipboard_clear()
        win.clipboard_append(mid)
        copy_btn.configure(text="✓ تم النسخ")
        win.after(1500, lambda: copy_btn.configure(text="📋 نسخ"))

    copy_btn = ctk.CTkButton(mid_row, text="📋 نسخ", command=copy_mid,
                              width=80, height=26,
                              fg_color=C["border"], hover_color=C["card"],
                              text_color=C["text"], font=ctk.CTkFont(size=11),
                              corner_radius=6)
    copy_btn.pack(side="right")

    # ── حقل الكود ──
    tk.Label(win, text="كود الترخيص:", bg=C["panel"], fg=C["muted"],
             font=("Tahoma", 10)).pack(anchor="w", padx=24, pady=(18, 4))

    code_var = tk.StringVar()
    code_entry = ctk.CTkEntry(win, textvariable=code_var,
                               placeholder_text="الصق كود الترخيص هنا...",
                               fg_color=C["card"], border_color=C["border"],
                               text_color=C["text"],
                               font=ctk.CTkFont(size=12), height=38, width=472)
    code_entry.pack(padx=24)

    error_label = tk.Label(win, text="", bg=C["panel"],
                            fg=C["red"], font=("Tahoma", 10), wraplength=460)
    error_label.pack(pady=(8, 0), padx=24)

    def activate():
        code = code_var.get().strip()
        if not code:
            error_label.configure(text="⚠️  الرجاء إدخال كود الترخيص")
            return

        result = verify_license_code(code)
        if result["valid"]:
            save_license(code)
            win.destroy()
            _show_license_banner(result)   # رسالة ترحيب
        else:
            error_label.configure(text=f"❌  {result['reason']}")

    ctk.CTkButton(win, text="✅  تفعيل البرنامج", command=activate,
                  width=472, height=42,
                  fg_color=C["accent"], hover_color="#3a7de0",
                  font=ctk.CTkFont(size=14, weight="bold"),
                  corner_radius=8).pack(padx=24, pady=(12, 6))

    ctk.CTkButton(win, text="إغلاق", command=app.destroy,
                  width=472, height=32,
                  fg_color="transparent", hover_color=C["card"],
                  border_width=1, border_color=C["border"],
                  text_color=C["muted"],
                  font=ctk.CTkFont(size=12),
                  corner_radius=8).pack(padx=24)

    code_entry.bind("<Return>", lambda e: activate())
    code_entry.focus_set()


def _show_license_banner(result: dict):
    """يعرض شريط ترحيب أخضر في أسفل الشاشة لفترة قصيرة"""
    ltype_ar = {"permanent": "دائم ♾", "yearly": "سنوي",
                "trial_30": "تجريبي 30 يوم", "trial_7": "تجريبي 7 أيام"
               }.get(result.get("type",""), result.get("type",""))
    days     = result.get("days_left")
    expires  = result.get("expires","")
    customer = result.get("customer","")

    parts = [f"✅  ترخيص {ltype_ar}"]
    if customer: parts.append(f"  |  {customer}")
    if days is not None: parts.append(f"  |  {days} يوم متبقٍ")
    elif expires == "never": parts.append("  |  غير محدود")

    set_status("  ".join(parts), C["green"])


def _check_license_on_start():
    """يُشغَّل مرة واحدة عند بدء البرنامج"""
    result = load_and_verify()
    if result["valid"]:
        _show_license_banner(result)
        # تحذير إذا بقي أقل من 14 يوم
        days = result.get("days_left")
        if days is not None and days <= 14:
            set_status(f"⚠️  ينتهي ترخيصك خلال {days} يوم — يرجى التجديد", C["orange"])
    else:
        app.after(200, _show_license_window)   # أعطِ الـ UI وقتاً للرسم أولاً


app.after(100, _check_license_on_start)


if __name__ == "__main__":
    app.mainloop()