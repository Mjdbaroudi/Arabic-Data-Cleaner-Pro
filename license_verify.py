"""
Arabic Data Cleaner — License Verification
يُوزَّع مع البرنامج. يحتوي المفتاح العام فقط — لا يمكن توليد تراخيص منه.
"""
import base64, json, hashlib, platform, datetime
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

PUBLIC_KEY_B64 = "ndYVv9jDDGG6D5kkoYVCJXvVOya+g9IzhDnU74g3cgM="
_APP_DIR     = Path.home() / ".arabic_data_cleaner"
LICENSE_FILE = _APP_DIR / "license.dat"
_CACHE_FILE  = _APP_DIR / ".lc"

def get_machine_id() -> str:
    parts = [platform.node(), platform.system(), platform.machine(), platform.processor()]
    if platform.system() == "Windows":
        try:
            import subprocess
            out = subprocess.check_output("wmic diskdrive get SerialNumber", shell=True,
                                          stderr=subprocess.DEVNULL).decode(errors="ignore")
            s = [l.strip() for l in out.splitlines() if l.strip() and "SerialNumber" not in l]
            if s: parts.append(s[0])
        except Exception: pass
    h = hashlib.sha256("|".join(p for p in parts if p).encode()).hexdigest()[:16].upper()
    return "-".join(h[i:i+4] for i in range(0, 16, 4))

def verify_license_code(code: str) -> dict:
    try:
        bundle  = json.loads(base64.b64decode(code.strip().encode()).decode("utf-8"))
        payload = bundle["payload"]
        sig     = base64.b64decode(bundle["sig"])
    except Exception:
        return {"valid": False, "reason": "كود غير صالح — تأكد من النسخ الكامل"}

    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
    try:
        pub.verify(sig, payload_bytes)
    except InvalidSignature:
        return {"valid": False, "reason": "توقيع غير صحيح — الكود مزيف أو معدَّل"}

    current = get_machine_id()
    if payload.get("mid","").strip() != current:
        return {"valid": False, "reason": f"هذا الكود مرتبط بجهاز آخر.\n\nMachine ID هذا الجهاز:\n{current}"}

    expires = payload.get("expires","never")
    days_left = None
    if expires != "never":
        try:
            exp = datetime.datetime.strptime(expires, "%Y-%m-%d").date()
            days_left = (exp - datetime.date.today()).days
            if days_left < 0:
                return {"valid": False, "reason": f"انتهت صلاحية الترخيص في {expires}"}
        except ValueError: pass

    return {"valid": True, "type": payload.get("type","unknown"), "expires": expires,
            "customer": payload.get("customer",""), "issued": payload.get("issued",""),
            "days_left": days_left}

def save_license(code: str):
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(code.strip(), encoding="utf-8")
    if _CACHE_FILE.exists(): _CACHE_FILE.unlink()

def load_and_verify() -> dict:
    if _CACHE_FILE.exists():
        try:
            c = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if (datetime.datetime.now()-datetime.datetime.fromisoformat(c["_time"])).total_seconds() < 86400 and c.get("valid"):
                return c
        except Exception: pass
    if not LICENSE_FILE.exists():
        return {"valid": False, "reason": "لا يوجد ترخيص مثبَّت"}
    result = verify_license_code(LICENSE_FILE.read_text(encoding="utf-8").strip())
    try:
        d = dict(result); d["_time"] = datetime.datetime.now().isoformat()
        _APP_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception: pass
    return result
