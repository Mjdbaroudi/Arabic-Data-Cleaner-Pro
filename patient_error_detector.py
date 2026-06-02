"""
Patient Error Detector
=======================
يكشف عن أخطاء إدخال البيانات في سجلات المرضى بناءً على أربعة أعمدة:
  - الاسم            (للمقارنة الأساسية)
  - تاريخ الميلاد
  - الجنس
  - رقم التعريف (ID) (يُعرض في النتائج)

المنطق:
  ● تطابق الاسم 100%  + اختلاف تاريخ الميلاد أو الجنس  → خطأ محتمل في البيانات
  ● تشابه الاسم 95-99% + تطابق تاريخ الميلاد والجنس    → خطأ إدخال في الاسم

يُهمَل: تطابق 100% مع تطابق كامل  /  تشابه < 95%
"""

import pandas as pd
import re
import datetime

from arabic_name_engine import similarity as _arabic_similarity, normalize_arabic as _normalize


# ─────────────────────────────────────────
# تطبيع الجنس
# ─────────────────────────────────────────

_MALE   = {"م","ذكر","male","m","1","boy","رجل","مذكر","ذ","ذكور"}
_FEMALE = {"ف","أنثى","انثى","female","f","0","girl","فتاة","مؤنث","ا","إناث","اناث","أنثي","انثي"}

def _normalize_gender(val):
    if pd.isna(val):
        return "?"
    s = str(val).strip().lower()
    s = re.sub(r'[\u200f\u200e\u200b\xa0]', '', s).strip()
    if s in _MALE:   return "M"
    if s in _FEMALE: return "F"
    return "?"


# ─────────────────────────────────────────
# تنظيف وتحليل التاريخ
# ─────────────────────────────────────────

def _parse_date(val):
    if pd.isna(val):
        return None
    if hasattr(val, 'date'):
        try:
            return val.date()
        except Exception:
            pass
    val_str = str(val).strip().split()[0]
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d"]:
        try:
            return datetime.datetime.strptime(val_str, fmt).date()
        except ValueError:
            pass
    try:
        return pd.to_datetime(val_str, dayfirst=True).date()
    except Exception:
        return None

def _format_date(val) -> str:
    d = _parse_date(val)
    if d:
        return d.strftime("%Y-%m-%d")
    if pd.isna(val):
        return ""
    return str(val).split()[0]

def _dates_equal(a, b) -> bool:
    if pd.isna(a) and pd.isna(b): return True
    if pd.isna(a) or  pd.isna(b): return False
    da = _parse_date(a)
    db = _parse_date(b)
    if da and db:
        return da == db
    ca = re.sub(r"[^\d]", "", str(a))
    cb = re.sub(r"[^\d]", "", str(b))
    return ca == cb and bool(ca)


# ─────────────────────────────────────────
# الوظيفة الرئيسية
# ─────────────────────────────────────────

def detect_patient_errors(
    df: pd.DataFrame,
    name_col: str,
    birth_col: str = None,
    gender_col: str = None,
    id_col: str = None,
    progress_callback=None,
) -> pd.DataFrame:
    """
    يفحص سجلات المرضى ويُعيد الأزواج التي تحتاج مراجعة.

    المُخرجات:
        RowNum1, ID1, Name1, BirthDate1, Gender1,
        RowNum2, ID2, Name2, BirthDate2, Gender2,
        NameSimilarity, AlertType, Reason
    """

    def pcb(v, t):
        if progress_callback:
            progress_callback(v, t)

    pcb(0.05, "تجهيز السجلات...")

    records = []
    for idx, row in df.iterrows():
        name = row.get(name_col, "")
        if pd.isna(name) or not _normalize(str(name)):
            continue
        excel_row = int(row["__excel_row__"]) if "__excel_row__" in df.columns else idx + 2
        birth  = row.get(birth_col,  "") if birth_col  else ""
        gender = row.get(gender_col, "") if gender_col else ""
        pid    = str(row.get(id_col, "")).strip() if id_col else ""
        if pid.lower() in ("nan", "none"): pid = ""

        records.append({
            "row_num":   excel_row,
            "pid":       pid,
            "name":      str(name).strip(),
            "name_norm": _normalize(str(name)),
            "birth":     birth,
            "birth_fmt": _format_date(birth),
            "gender":    gender,
            "gender_n":  _normalize_gender(gender),
        })

    n = len(records)
    pcb(0.10, f"{n} سجل — جاري المقارنة...")

    results = []
    total_pairs = n * (n - 1) // 2
    done = 0

    for i in range(n):
        r1 = records[i]
        for j in range(i + 1, n):
            r2 = records[j]
            done += 1

            sim = _arabic_similarity(r1["name"], r2["name"])

            # ─── الحالة الأولى: تطابق 100% ───────────────────────────
            if sim >= 100 or r1["name_norm"] == r2["name_norm"]:
                birth_match  = _dates_equal(r1["birth"], r2["birth"]) if birth_col  else None
                gender_match = (r1["gender_n"] == r2["gender_n"] and
                                r1["gender_n"] != "?")                if gender_col else None

                if birth_match is True and gender_match is True:
                    continue
                if birth_col is None and gender_col is None:
                    continue

                reasons = []
                if birth_col  and birth_match  is False:
                    reasons.append(f"تاريخ الميلاد مختلف ({r1['birth_fmt']} ≠ {r2['birth_fmt']})")
                if gender_col and gender_match is False:
                    reasons.append(f"الجنس مختلف ({r1['gender']} ≠ {r2['gender']})")

                if not reasons:
                    continue

                results.append({
                    "RowNum1": r1["row_num"], "ID1": r1["pid"],
                    "Name1": r1["name"], "BirthDate1": r1["birth_fmt"],
                    "Gender1": r1["gender"] if gender_col else "",
                    "RowNum2": r2["row_num"], "ID2": r2["pid"],
                    "Name2": r2["name"], "BirthDate2": r2["birth_fmt"],
                    "Gender2": r2["gender"] if gender_col else "",
                    "NameSimilarity": 100.0,
                    "AlertType": "خطأ_بيانات",
                    "Reason": " | ".join(reasons),
                })

            # ─── الحالة الثانية: تشابه 95-99% ────────────────────────
            elif 95 <= sim < 100:
                birth_match  = _dates_equal(r1["birth"], r2["birth"]) if birth_col  else None
                gender_match = (r1["gender_n"] == r2["gender_n"] and
                                r1["gender_n"] != "?")                if gender_col else None

                show = True
                if birth_col  and birth_match  is not True: show = False
                if gender_col and gender_match is not True: show = False
                if not show: continue

                results.append({
                    "RowNum1": r1["row_num"], "ID1": r1["pid"],
                    "Name1": r1["name"], "BirthDate1": r1["birth_fmt"],
                    "Gender1": r1["gender"] if gender_col else "",
                    "RowNum2": r2["row_num"], "ID2": r2["pid"],
                    "Name2": r2["name"], "BirthDate2": r2["birth_fmt"],
                    "Gender2": r2["gender"] if gender_col else "",
                    "NameSimilarity": round(sim, 1),
                    "AlertType": "خطأ_إدخال_اسم",
                    "Reason": f"تشابه الاسم {sim:.0f}% مع تطابق بيانات الهوية",
                })

            if progress_callback and done % 5000 == 0 and total_pairs > 0:
                pct = 0.10 + (done / total_pairs) * 0.85
                progress_callback(round(pct, 2), f"فحص {done:,}/{total_pairs:,}")

    results.sort(key=lambda x: (x["AlertType"] != "خطأ_بيانات", -x["NameSimilarity"]))

    pcb(1.0, f"اكتمل — {len(results)} حالة تحتاج مراجعة")

    cols = ["RowNum1","ID1","Name1","BirthDate1","Gender1",
            "RowNum2","ID2","Name2","BirthDate2","Gender2",
            "NameSimilarity","AlertType","Reason"]
    return pd.DataFrame(results, columns=cols)