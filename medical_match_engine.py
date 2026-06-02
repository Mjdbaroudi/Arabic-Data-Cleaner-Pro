"""
Medical Record Matching Engine
================================
محرك مطابقة السجلات الطبية المتعدد الأعمدة.

يحسب نسبة تطابق مركّبة بين كل زوج من السجلات
بناءً على أوزان قابلة للضبط لكل عمود.

الأعمدة المدعومة وطرق مقارنتها:
  - اسم (نصي عربي)    → arabic_name similarity
  - تاريخ ميلاد        → date proximity (±days)
  - جنس               → exact match
  - Patient ID        → exact match (إذا متطابق → 100% فوراً)
  - تشخيص             → fuzzy text match
  - تاريخ زيارة        → date proximity (±days)
"""

import pandas as pd
import numpy as np
import re
import datetime
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

try:
    from rapidfuzz import fuzz as _rf_fuzz
    def _fuzzy_ratio(a, b):
        return _rf_fuzz.token_set_ratio(a, b)
except ImportError:
    import difflib
    def _fuzzy_ratio(a, b):
        return difflib.SequenceMatcher(None, a, b).ratio() * 100

from arabic_name_engine import normalize_arabic, similarity, create_blocks, _process_block


# ─────────────────────────────────────────
# Column type definitions
# ─────────────────────────────────────────

COL_TYPES = {
    "name":       "اسم عربي",
    "date":       "تاريخ",
    "gender":     "جنس",
    "id":         "رقم / كود",
    "text":       "نص عام",
    "diagnosis":  "تشخيص",
}

DEFAULT_WEIGHTS = {
    "name":      40,
    "date":      25,
    "gender":    10,
    "id":        0,     # إذا استُخدم كـ Patient ID فهو مشغّل وليس عامل وزن
    "text":      10,
    "diagnosis": 15,
}


# ─────────────────────────────────────────
# Field-level similarity functions
# ─────────────────────────────────────────

def _name_sim(a: str, b: str) -> float:
    """تشابه الاسم العربي — يستخدم similarity() من العنجن الأصلي"""
    if not a or not b:
        return 0.0
    return similarity(a, b)


def _date_sim(a: str, b: str, tolerance_days: int = 30) -> float:
    """
    تشابه التاريخ بناءً على القرب الزمني.
    tolerance_days: الحد الأقصى للفرق بالأيام الذي يُعتبر تطابقاً
    """
    if not a or not b:
        return 0.0
    da = _parse_date(a)
    db = _parse_date(b)
    if da is None or db is None:
        # جرب مقارنة نصية (قد يكون سنة فقط مثل "1995")
        a_clean = re.sub(r"[^\d]", "", str(a))
        b_clean = re.sub(r"[^\d]", "", str(b))
        if a_clean and b_clean and a_clean == b_clean:
            return 100.0
        elif a_clean and b_clean and a_clean[:4] == b_clean[:4]:
            return 70.0
        return 0.0
    diff = abs((da - db).days)
    if diff == 0:
        return 100.0
    elif diff <= tolerance_days:
        # decay linear
        return max(0.0, 100.0 * (1 - diff / tolerance_days))
    return 0.0


def _parse_date(val: str):
    """يحاول تحليل التاريخ من عدة صيغ شائعة"""
    val = str(val).strip()
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
        "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y",
        "%Y%m%d",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(val, fmt).date()
        except ValueError:
            pass
    # حاول pandas
    try:
        return pd.to_datetime(val, dayfirst=True).date()
    except Exception:
        return None


def _exact_sim(a: str, b: str) -> float:
    """مطابقة حرفية بعد تنظيف"""
    a = str(a).strip().lower()
    b = str(b).strip().lower()
    if not a or not b:
        return 0.0
    return 100.0 if a == b else 0.0


def _gender_sim(a: str, b: str) -> float:
    """مطابقة الجنس مع دعم التسميات المختلفة"""
    MALE   = {"م", "ذكر", "male", "m", "1", "boy", "رجل", "مذكر"}
    FEMALE = {"ف", "أنثى", "انثى", "female", "f", "0", "girl", "فتاة", "مؤنث"}
    a = str(a).strip().lower()
    b = str(b).strip().lower()
    if not a or not b:
        return 50.0   # مجهول → حيادي
    def classify(x):
        if x in MALE:   return "m"
        if x in FEMALE: return "f"
        return "?"
    ca, cb = classify(a), classify(b)
    if ca == "?" or cb == "?":
        return 50.0
    return 100.0 if ca == cb else 0.0


def _text_sim(a: str, b: str) -> float:
    """تشابه نص عام (تشخيص / ملاحظات)"""
    a = str(a).strip()
    b = str(b).strip()
    if not a or not b:
        return 0.0
    # normalize arabic for text fields too
    an = normalize_arabic(a)
    bn = normalize_arabic(b)
    return _fuzzy_ratio(an, bn)


# دالة الـ dispatch
def _field_sim(col_type: str, a, b, date_tolerance: int = 30) -> float:
    a = "" if pd.isna(a) else str(a).strip()
    b = "" if pd.isna(b) else str(b).strip()
    if col_type == "name":
        return _name_sim(a, b)
    elif col_type == "date":
        return _date_sim(a, b, date_tolerance)
    elif col_type == "gender":
        return _gender_sim(a, b)
    elif col_type == "id":
        return _exact_sim(a, b)
    elif col_type in ("text", "diagnosis"):
        return _text_sim(a, b)
    return 0.0


# ─────────────────────────────────────────
# Composite score
# ─────────────────────────────────────────

def composite_score(row1: dict, row2: dict,
                    col_config: list,
                    date_tolerance: int = 30) -> dict:
    """
    يحسب النسبة المركّبة بين سجلين.

    col_config: قائمة من:
        {"col": "اسم العمود", "type": "name"|"date"|..., "weight": 40}

    يُعيد:
        {
          "score":   float  (0-100 النسبة الإجمالية),
          "details": [{"col": ..., "type": ..., "weight": ..., "sim": ...}, ...]
          "is_id_match": bool  (True إذا تطابق Patient ID حرفياً)
        }
    """
    # ── Patient ID shortcut ──
    for cfg in col_config:
        if cfg["type"] == "id" and cfg.get("is_patient_id"):
            v1 = str(row1.get(cfg["col"], "")).strip()
            v2 = str(row2.get(cfg["col"], "")).strip()
            if v1 and v2 and v1 == v2:
                return {
                    "score": 100.0,
                    "details": [{"col": cfg["col"], "type": "id",
                                 "weight": 100, "sim": 100.0}],
                    "is_id_match": True
                }

    # ── حساب عادي ──
    # أعمدة الوزن فقط (نتجاهل id غير Patient ID في الحساب)
    weighted_cols = [c for c in col_config
                     if c["weight"] > 0 and not c.get("is_patient_id")]
    total_weight  = sum(c["weight"] for c in weighted_cols)
    if total_weight == 0:
        return {"score": 0.0, "details": [], "is_id_match": False}

    details = []
    weighted_sum = 0.0
    for cfg in weighted_cols:
        col  = cfg["col"]
        ctype = cfg["type"]
        w    = cfg["weight"]
        sim  = _field_sim(ctype,
                          row1.get(col, ""),
                          row2.get(col, ""),
                          date_tolerance)
        details.append({"col": col, "type": ctype, "weight": w, "sim": sim})
        weighted_sum += sim * (w / total_weight)

    return {
        "score":      round(weighted_sum, 1),
        "details":    details,
        "is_id_match": False,
    }


# ─────────────────────────────────────────
# Main detection function
# ─────────────────────────────────────────

def detect_medical_duplicates(
    df: pd.DataFrame,
    name_col: str,
    col_config: list,
    threshold: float = 80.0,
    date_tolerance: int = 30,
    patient_id_col: str = None,
    progress_callback=None,
) -> pd.DataFrame:
    """
    الكشف عن السجلات المكررة باستخدام مطابقة متعددة الأعمدة.

    Parameters
    ----------
    df              : DataFrame المحمّل
    name_col        : عمود الاسم (للـ blocking)
    col_config      : إعداد الأعمدة والأوزان
    threshold       : الحد الأدنى للنسبة الإجمالية (افتراضي 80)
    date_tolerance  : الفرق الأقصى بالأيام للتواريخ
    patient_id_col  : عمود رقم المريض (اختياري — shortcut فوري)
    progress_callback: دالة callback(value, text)

    Returns
    -------
    DataFrame أعمدته:
        Name1, Name2, Score, Details, IsIDMatch, Row1, Row2
        Details: JSON string لتفصيل كل عمود
    """
    import json

    def pcb(v, t):
        if progress_callback:
            progress_callback(v, t)

    pcb(0.05, "تجهيز البيانات...")

    # ── نظّف وجهّز ──
    records = []
    for idx, row in df.iterrows():
        name = row.get(name_col, "")
        if pd.isna(name) or not normalize_arabic(str(name)):
            continue
        excel_row = int(row["__excel_row__"]) if "__excel_row__" in df.columns else idx + 2
        records.append({
            "__idx__":      idx,
            "__excel_row__": excel_row,
            **{col: row.get(col, "") for col in df.columns if col != "__excel_row__"},
        })

    n = len(records)
    if n < 2:
        return pd.DataFrame(columns=["Name1","Name2","Score","Details","IsIDMatch","Row1","Row2"])

    pcb(0.08, f"تجهيز {n} سجل...")

    # ── Patient ID shortcut pass ──
    id_matches = []
    if patient_id_col and patient_id_col in df.columns:
        pcb(0.10, "فحص Patient ID...")
        pid_groups = defaultdict(list)
        for rec in records:
            pid = str(rec.get(patient_id_col, "")).strip()
            if pid and pid.lower() not in ("nan", "none", ""):
                pid_groups[pid].append(rec)
        for pid, grp in pid_groups.items():
            for i in range(len(grp)):
                for j in range(i+1, len(grp)):
                    r1, r2 = grp[i], grp[j]
                    n1 = str(r1.get(name_col,""))
                    n2 = str(r2.get(name_col,""))
                    id_matches.append({
                        "Name1": n1, "Name2": n2,
                        "Score": 100.0,
                        "Details": json.dumps([{"col": patient_id_col, "type": "id",
                                                "weight": 100, "sim": 100.0}],
                                              ensure_ascii=False),
                        "IsIDMatch": True,
                        "Row1": r1["__excel_row__"],
                        "Row2": r2["__excel_row__"],
                    })

    # ── Name-based blocking للباقي ──
    pcb(0.12, "Blocking بالاسم...")

    # استخدم الـ blocking الأصلي من arabic_name_engine
    names_list = [str(r.get(name_col,"")) for r in records]
    blocks     = create_blocks(names_list)
    block_list = [(k, v) for k, v in blocks.items() if len(v) >= 2]

    pcb(0.15, f"تحليل {len(block_list)} مجموعة...")

    # ── جمع المرشحين من الاسم (pre-filter) ──
    unique_norms = [normalize_arabic(n) for n in names_list]
    pre_threshold = max(55, 70 - 15)   # عتبة أقل لأن الأعمدة الأخرى ستعوّض
    tasks = [(v, unique_norms, pre_threshold) for _, v in block_list]

    max_workers = min(6, max(1, len(block_list)//20 + 1))
    candidate_idx_pairs = set()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for done, cands in enumerate(ex.map(_process_block, tasks)):
            for idx1, idx2, *_ in cands:
                pair = (min(idx1,idx2), max(idx1,idx2))
                candidate_idx_pairs.add(pair)
            if progress_callback and len(block_list) > 0:
                pct = 0.15 + (done / len(block_list)) * 0.45
                progress_callback(round(pct, 2),
                                  f"blocking {done+1}/{len(block_list)}")

    pcb(0.62, f"حساب نسبة مركّبة لـ {len(candidate_idx_pairs)} زوج...")

    # ── حساب النسبة المركّبة ──
    results = list(id_matches)   # ابدأ بنتائج الـ ID

    def _score_pair(args):
        i, j = args
        r1, r2 = records[i], records[j]
        res = composite_score(r1, r2, col_config, date_tolerance)
        if res["score"] >= threshold:
            return {
                "Name1":     str(r1.get(name_col,"")),
                "Name2":     str(r2.get(name_col,"")),
                "Score":     res["score"],
                "Details":   json.dumps(res["details"], ensure_ascii=False),
                "IsIDMatch": res["is_id_match"],
                "Row1":      r1["__excel_row__"],
                "Row2":      r2["__excel_row__"],
            }
        return None

    pair_list = list(candidate_idx_pairs)
    done_count = [0]

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(_score_pair, pair_list):
            done_count[0] += 1
            if res:
                results.append(res)
            if progress_callback and done_count[0] % max(1, len(pair_list)//20) == 0:
                pct = 0.62 + (done_count[0] / max(1, len(pair_list))) * 0.33
                progress_callback(round(pct, 2),
                                  f"تقييم {done_count[0]}/{len(pair_list)}")

    # ── ترتيب: ID matches أولاً ثم بالنسبة تنازلياً ──
    results.sort(key=lambda x: (not x["IsIDMatch"], -x["Score"]))

    pcb(1.0, f"اكتمل — {len(results)} نتيجة")

    return pd.DataFrame(results,
                        columns=["Name1","Name2","Score","Details",
                                 "IsIDMatch","Row1","Row2"])