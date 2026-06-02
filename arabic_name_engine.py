import pandas as pd
import re
import numpy as np
from rapidfuzz import fuzz, process
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# ─────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────

def normalize_arabic(text):
    if pd.isna(text):
        return ""
    text = str(text)
    text = re.sub("[إأآا]", "ا", text)
    text = re.sub("ة",      "ه", text)
    text = re.sub("ى",      "ي", text)
    text = re.sub("[ًٌٍَُِّْـ]", "", text)
    text = re.sub(r"\s+",   " ", text)
    return text.strip()

# ─────────────────────────────────────────
# Similarity — للتحقق الدقيق بعد cdist
# ─────────────────────────────────────────

# ─────────────────────────────────────────
# Arabic Name Structure Comparison
# ─────────────────────────────────────────

def _align_names(at, bt):
    """
    محاذاة ذكية بين اسمين:
    - الكلمة الأولى = الاسم الأول (وزن عالٍ، حارس صارم)
    - الكلمات الوسطى = اسم الأب وما بعده
    - إذا كان الطولان مختلفين: الاسم الإضافي في الأطول يُعامل كـ bonus وليس penalty

    يُعيد: قائمة من (كلمة_أ, كلمة_ب, وزن, is_first, is_last)
    """
    la, lb = len(at), len(bt)

    if la == lb:
        pairs = []
        for i in range(la):
            if i == 0:        w = 0.35
            elif i == la - 1: w = 0.35
            else:             w = 0.30 / max(1, la - 2)
            pairs.append((at[i], bt[i], w, i==0, i==la-1))
        return pairs

    # فرق كلمة واحدة: الأقصر محاذى من اليسار (الاسم الأول ثابت)
    # الكلمة الزائدة في الأطول تُعامل كـ bonus اختياري
    short, long_ = (at, bt) if la < lb else (bt, at)
    ls = len(short)

    pairs = []
    # قارن كل كلمات الاسم الأقصر مع مقابلاتها في الأطول
    for i in range(ls):
        if i == 0:        w = 0.35
        elif i == ls - 1: w = 0.30   # الأخير في الأقصر — وزن متوسط (ليس العائلة الحقيقية)
        else:             w = 0.35 / max(1, ls - 2)
        pairs.append((short[i], long_[i], w, i==0, False))

    # الكلمة الزائدة في الأطول: bonus صغير
    # لا نعاقب غيابها في الأقصر
    pairs.append((long_[-1], long_[-1], 0.10, False, True))   # تشابه مع نفسها = 100 دائماً

    return pairs


def similarity(a, b):
    a_n = normalize_arabic(a)
    b_n = normalize_arabic(b)

    if a_n == b_n:
        return 100.0

    at = a_n.split()
    bt = b_n.split()
    la, lb = len(at), len(bt)

    if abs(la - lb) > 1:
        return 0.0

    if la == 1 and lb == 1:
        s = fuzz.ratio(a_n, b_n)
        return round(s, 1) if s >= 90 else 0.0

    pairs = _align_names(at, bt)

    total_weight = 0.0
    total_score  = 0.0

    for wa, wb, w, is_first, is_last in pairs:
        s = fuzz.ratio(wa, wb)

        # حارس الاسم الأول: يجب تشابه كافٍ
        if is_first and s < 70:
            return 0.0

        # حارس الأخير: فقط إذا نفس الطول (الأخير = العائلة الحقيقية)
        if is_last and la == lb and s < 75:
            return 0.0

        total_score  += s * w
        total_weight += w

    if total_weight == 0:
        return 0.0

    score = total_score / total_weight

    # ── منطق بنية الاسم: طولان مختلفان ──
    if la != lb:
        short = at if la < lb else bt
        long_ = bt if la < lb else at
        first_sim = fuzz.ratio(short[0], long_[0])
        # قارن آخر كلمة في الأقصر مع مقابلتها في الأطول (ليس الأخير بالضرورة)
        last_sim  = fuzz.ratio(short[-1], long_[len(short)-1])

        if first_sim >= 85 and last_sim >= 85:
            score = min(100.0, score * 1.08)   # نفس الشخص مع اسم إضافي
        elif first_sim >= 85 and last_sim < 55:
            score *= 0.65                       # الأب مختلف → شخص آخر غالباً

    return round(score, 1)

# ─────────────────────────────────────────
# Blocking
# ─────────────────────────────────────────

def _block_key(norm):
    """
    مفتاح التصنيف الذكي:
    أول حرف من الاسم الأول + آخر حرف من اسم العائلة + عدد الكلمات
    مثال: "محمد احمد علي" → "م_ي_3"
    """
    parts = norm.split()
    if not parts:
        return None
    first_char = parts[0][0]  if parts[0]  else ""
    last_char  = parts[-1][-1] if parts[-1] else ""
    wc         = len(parts)
    return f"{first_char}_{last_char}_{wc}"

def create_blocks(names):
    """
    Multi-key blocking بمفتاح ثلاثي:
      أول حرف + آخر حرف + word_count

    مع overlap ±1 كلمة لالتقاط:
      "محمد احمد"  vs  "محمد احمد علي"
    """
    blocks = defaultdict(list)
    for i, name in enumerate(names):
        norm = normalize_arabic(name)
        if not norm:
            continue
        parts = norm.split()
        wc    = len(parts)
        first_char = parts[0][0]   if parts[0]  else ""
        last_char  = parts[-1][-1] if parts[-1] else ""

        # المفتاح الأساسي
        blocks[f"{first_char}_{last_char}_{wc}"].append(i)

        # overlap مع wc-1 و wc+1 لالتقاط الأسماء الناقصة/الزائدة
        if wc > 1:
            # wc-1: last_char يتغير (العائلة تختفي)
            prev_last = parts[-2][-1] if len(parts) >= 2 else last_char
            blocks[f"{first_char}_{prev_last}_{wc-1}"].append(i)
        # wc+1: نضيف الاسم الحالي لـ block الأطول أيضاً
        blocks[f"{first_char}_{last_char}_{wc+1}"].append(i)

    return blocks

# ─────────────────────────────────────────
# Block processor — cdist على كل block
# ─────────────────────────────────────────

def _process_block(args):
    """
    cdist على block واحد — يُعيد المرشحين فقط.
    يعمل في thread مستقل.
    """
    indices, unique_norms, pre_threshold = args

    if len(indices) < 2:
        return []

    norms_in_block = [unique_norms[i] for i in indices]

    # cdist: C++ backend مع multiprocessing داخلي
    # workers=-1 = استخدم كل أنوية المعالج تلقائياً
    matrix = process.cdist(
        norms_in_block,
        norms_in_block,
        scorer=fuzz.ratio,
        score_cutoff=pre_threshold,
        workers=-1
    )

    candidates = []
    n = len(indices)
    for i in range(n):
        for j in range(i + 1, n):
            score = matrix[i][j]
            if score >= pre_threshold:
                idx1 = indices[i]
                idx2 = indices[j]
                n1   = unique_norms[idx1]
                n2   = unique_norms[idx2]
                if n1 != n2:
                    candidates.append((idx1, idx2, n1, n2))
    return candidates

# ─────────────────────────────────────────
# Detect Duplicates
# ─────────────────────────────────────────

def detect_duplicates(df, name_col, progress_callback=None, threshold=90):
    """
    النسخة المسرّعة:
    - cdist (C++ backend) بدل حلقة fuzz.ratio
    - ThreadPoolExecutor للـ blocks بالتوازي
    - similarity() للتحقق الدقيق على المرشحين فقط
    """

    # ── جمع القيم الخام ──
    raw_list = []
    for idx, row in df.iterrows():
        val = row[name_col]
        if pd.isna(val):
            continue
        raw = str(val)
        if not normalize_arabic(raw):
            continue
        excel_row = int(row["__excel_row__"]) if "__excel_row__" in df.columns else idx + 2
        raw_list.append((raw, excel_row))

    if not raw_list:
        return pd.DataFrame(columns=["Name1", "Name2", "Similarity"])

    if progress_callback:
        progress_callback(0.05, f"تجهيز البيانات...")

    # ── أسماء فريدة بالـ norm ──
    norm_to_first_raw = {}
    unique_norms      = []

    for raw, _ in raw_list:
        n = normalize_arabic(raw)
        if n not in norm_to_first_raw:
            norm_to_first_raw[n] = raw
            unique_norms.append(n)

    if progress_callback:
        progress_callback(0.08, f"تجهيز {len(unique_norms)} اسم فريد...")

    # ── Blocking ──
    norm_names_for_block = [norm_to_first_raw[n] for n in unique_norms]
    blocks     = create_blocks(norm_names_for_block)
    block_list = [(k, v) for k, v in blocks.items() if len(v) >= 2]
    total      = len(block_list)

    if progress_callback:
        progress_callback(0.12, f"تحليل {total} مجموعة بـ cdist...")

    # pre_threshold أقل من threshold لنضمن عدم تفويت أي زوج
    pre_threshold = max(60, threshold - 15)
    tasks = [(v, unique_norms, pre_threshold) for _, v in block_list]

    # ── parallel processing للـ blocks ──
    # cdist workers=-1 يستخدم كل الأنوية داخلياً لكل block
    # ThreadPoolExecutor يشغّل الـ blocks بالتوازي (آمن مع tkinter)
    max_workers = min(6, max(1, total // 20 + 1))
    all_candidates = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for done_idx, candidates in enumerate(executor.map(_process_block, tasks)):
            all_candidates.extend(candidates)
            if progress_callback and total > 0 and done_idx % max(1, total//20) == 0:
                pct = 0.12 + (done_idx / total) * 0.68
                progress_callback(round(pct, 2), f"cdist {done_idx+1}/{total}  ({len(all_candidates)} مرشح)")

    if progress_callback:
        progress_callback(0.82, f"تحقق دقيق من {len(all_candidates)} مرشح...")

    # ── التحقق الدقيق بـ similarity() ──
    # نحذف المكررات أولاً ثم نحسب بالتوازي
    seen_dk      = set()
    unique_pairs = []
    for idx1, idx2, n1, n2 in all_candidates:
        dk = tuple(sorted([n1, n2]))
        if dk not in seen_dk:
            seen_dk.add(dk)
            unique_pairs.append((n1, n2,
                                 norm_to_first_raw[n1],
                                 norm_to_first_raw[n2]))

    def _check_pair(args):
        n1, n2, raw1, raw2 = args
        sim = similarity(raw1, raw2)
        return (raw1, raw2, sim) if sim >= threshold else None

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for res in executor.map(_check_pair, unique_pairs):
            if res:
                results.append(res)

    # ── تكرارات حرفية ──
    norm_count = defaultdict(int)
    for raw, _ in raw_list:
        norm_count[normalize_arabic(raw)] += 1

    exact_done = set()
    for n, count in norm_count.items():
        if count > 1 and n not in exact_done:
            exact_done.add(n)
            raw = norm_to_first_raw[n]
            results.insert(0, (raw, raw, 100.0))

    if progress_callback:
        progress_callback(1.0, f"اكتمل — {len(results)} نتيجة")

    return pd.DataFrame(results, columns=["Name1", "Name2", "Similarity"])

# ─────────────────────────────────────────
# Remove Duplicates
# ─────────────────────────────────────────

def remove_duplicates(df, name_col):
    df = df.copy()
    df["_norm"] = df[name_col].apply(lambda x: normalize_arabic(str(x)))
    df = df.drop_duplicates("_norm")
    df = df.drop(columns=["_norm"])
    return df

if __name__ == "__main__":
    file_path = input("Excel file path: ")
    df        = pd.read_excel(file_path)
    print("\nColumns:", df.columns.tolist())
    name_col  = input("\nName column: ")
    print("\nAnalyzing...")
    dupes     = detect_duplicates(df, name_col)
    dupes.to_excel("duplicates_report.xlsx", index=False)
    print(f"Done. {len(dupes)} duplicates found.")