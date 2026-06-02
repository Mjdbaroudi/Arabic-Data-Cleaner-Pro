"""
Arabic Data Cleaner — License Generator
⚠️  هذا الملف يبقى عندك أنت فقط — لا يُوزَّع أبداً مع البرنامج.
"""
import base64, json, datetime
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

PRIVATE_KEY_B64 = "zx7OLxaifGc6sfFNr9fgTVvP13xy4x0M1NvMrlNgZN4="

LICENSE_TYPES = {
    "1": ("permanent", "دائم"),
    "2": ("yearly",    "سنة كاملة"),
    "3": ("trial_30",  "تجريبي 30 يوم"),
    "4": ("trial_7",   "تجريبي 7 أيام"),
}

def generate_license(machine_id, license_type, customer=""):
    now     = datetime.datetime.utcnow()
    payload = {"mid": machine_id.strip(), "type": license_type,
               "issued": now.strftime("%Y-%m-%d"), "customer": customer}
    if   license_type == "yearly":   payload["expires"] = (now + datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    elif license_type == "trial_30": payload["expires"] = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    elif license_type == "trial_7":  payload["expires"] = (now + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    else:                            payload["expires"] = "never"

    pk    = Ed25519PrivateKey.from_private_bytes(base64.b64decode(PRIVATE_KEY_B64))
    sig   = pk.sign(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
    bundle = {"payload": payload, "sig": base64.b64encode(sig).decode()}
    return base64.b64encode(json.dumps(bundle, ensure_ascii=False, separators=(",",":")).encode()).decode()

def main():
    print("╔══════════════════════════════════════════╗")
    print("║   Arabic Data Cleaner — مولّد التراخيص   ║")
    print("╚══════════════════════════════════════════╝\n")
    mid = input("أدخل Machine ID للمستخدم: ").strip()
    if not mid: print("خطأ: Machine ID فارغ"); return
    print("\nنوع الترخيص:")
    for k,(t,l) in LICENSE_TYPES.items(): print(f"  {k}. {l}")
    choice = input("\nاختر رقماً: ").strip()
    if choice not in LICENSE_TYPES: print("خطأ: اختيار غير صحيح"); return
    ltype, label = LICENSE_TYPES[choice]
    customer = input("اسم العميل (اختياري): ").strip()
    code = generate_license(mid, ltype, customer)
    print(f"\n✅ ترخيص {label}  |  الجهاز: {mid}")
    print("─" * 60)
    print(code)
    print("─" * 60)
    fname = f"license_{mid[:8]}_{datetime.date.today()}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"Machine ID : {mid}\nالنوع      : {label}\nالتاريخ    : {datetime.date.today()}\nالعميل     : {customer or '—'}\n\nكود الترخيص:\n{code}\n")
    print(f"📄 محفوظ في: {fname}")

if __name__ == "__main__":
    main()