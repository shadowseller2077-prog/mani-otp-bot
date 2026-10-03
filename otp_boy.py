#!/usr/bin/env python3
"""
Firebase SMS Dashboard Bot — FINAL
- Multi-Firebase (40)
- Force Join + Captcha Verification
- Referral System (1 refer = 3 hours)
- Channel leave → referrer access revoke
- Admin unlimited access
- SMS monitor auto-stop on access revoke
- SMS monitor idle timeout (10 min no button tap)
- Admin Gift Access (single user / all users)
"""

import os
import re
import json
import time
import asyncio
import logging
import gc
import random
from html import escape as html_escape
from collections import Counter
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Set

import aiohttp
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8612239811:AAHaTr3YJyqBf48FVIIWizEp5yNuPgU05do"
ADMIN_IDS = [6799525497, 8425543018]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FirebaseSMSBot")

# ============================================================
# CONSTANTS
# ============================================================
FB_REQUEST_TIMEOUT = 10
FB_RETRY_MAX = 1
FB_RETRY_BACKOFF = 1.2
FB_CLIENTS_MAX_BYTES = 50 * 1024 * 1024
FB_MESSAGES_LIMIT = 5
MAX_FIREBASES = 40
CLEANUP_INTERVAL = 60
SMS_MONITOR_INTERVAL = 1
SMS_MONITOR_DURATION = 300
SMS_MONITOR_IDLE_TIMEOUT = 600  # 10 minutes no button tap → auto stop
ADMIN_PANEL_EDIT_INTERVAL = 5
WELCOME_IMAGE_URL = "https://i.ibb.co/CK3s8vzR/Gemini-Generated-Image-en17gcen17gcen17.png"

# Referral
REFERRAL_HOURS = 3
REFERRAL_SECONDS = REFERRAL_HOURS * 3600

# Gift
GIFT_ACCESS_MAX_HOURS = 24 * 365  # 1 year max per gift

# ============================================================
# SUPABASE CONFIG & INITIALIZATION
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://zgtkqivsywgkyaazhnaw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_w5rvtHSvvDI_blAld0urAA_HpkuSQ4K")

try:
    from supabase import create_client, Client
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except ImportError:
    supabase = None
    logger.warning("Supabase package not installed. Run 'pip install supabase'.")

# Files
GLOBAL_DEVICE_CACHE_FILE = os.getenv("GLOBAL_DEVICE_CACHE_FILE", "global_devices_cache.json")

ACCESS_CHECK_INTERVAL = 30
ADMIN_DEVICE_REFRESH_INTERVAL = 180

DEFAULT_CHANNELS = []  # Force-join channels removed


# ============================================================
# DB HELPERS
# ============================================================
def _parse_channel_items(raw: str):
    channels = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("|", 2)]
        identifier = parts[0]
        label = parts[1] if len(parts) > 1 and parts[1] else identifier
        join_url = parts[2] if len(parts) > 2 and parts[2] else (
            f"https://t.me/{identifier.lstrip('@')}"
            if identifier.startswith("@") else "")
        channels.append({"id": identifier, "label": label, "url": join_url})
    return channels


def _load_required_channels():
    if not supabase: return list(DEFAULT_CHANNELS)
    try:
        res = supabase.table("bot_settings").select("value").eq("key", "required_channels").execute()
        if res.data:
            val = res.data[0]["value"]
            if isinstance(val, str):
                val = json.loads(val)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict) and item.get("id")]
    except Exception as exc:
        logger.warning("could not load required channels from DB: %s", exc)
    return list(DEFAULT_CHANNELS)


def _save_required_channels():
    if not supabase: return
    try:
        supabase.table("bot_settings").upsert({
            "key": "required_channels",
            "value": REQUIRED_CHANNELS
        }).execute()
    except Exception as exc:
        logger.warning("could not save force-join channels: %s", exc)


def _load_user_ids():
    if not supabase: return set()
    try:
        res = supabase.table("bot_users").select("user_id").execute()
        return {int(row["user_id"]) for row in res.data}
    except Exception as exc:
        logger.warning("could not load users from DB: %s", exc)
        return set()


def _save_user_ids():
    if not supabase: return
    try:
        data = [{"user_id": uid} for uid in known_users]
        if data:
            supabase.table("bot_users").upsert(data).execute()
    except Exception as exc:
        logger.warning("could not save user ids: %s", exc)


def _load_maintenance_mode() -> bool:
    if not supabase: return False
    try:
        res = supabase.table("bot_settings").select("value").eq("key", "maintenance_mode").execute()
        if res.data:
            val = res.data[0]["value"]
            if isinstance(val, str):
                return val.lower() == "true"
            return bool(val)
    except Exception:
        pass
    return False


def _save_maintenance_mode():
    if not supabase: return
    try:
        supabase.table("bot_settings").upsert({
            "key": "maintenance_mode", 
            "value": str(maintenance_mode).lower()
        }).execute()
    except Exception as exc:
        logger.warning("could not save maintenance mode: %s", exc)


def _load_captcha_enabled() -> bool:
    if not supabase: return True
    try:
        res = supabase.table("bot_settings").select("value").eq("key", "captcha_enabled").execute()
        if res.data:
            val = res.data[0]["value"]
            if isinstance(val, str):
                return val.lower() == "true"
            return bool(val)
    except Exception:
        pass
    return True


def _save_captcha_enabled():
    if not supabase: return
    try:
        supabase.table("bot_settings").upsert({
            "key": "captcha_enabled", 
            "value": str(captcha_enabled).lower()
        }).execute()
    except Exception as exc:
        logger.warning("could not save captcha state: %s", exc)


def _load_global_firebases():
    if not supabase: return []
    try:
        res = supabase.table("global_firebases").select("*").execute()
        out = []
        for row in res.data:
            u = str(row.get("url")).strip().rstrip("/")
            while u.endswith(".json"):
                u = u[:-5].rstrip("/")
            if "firebaseio.com" in u or "firebasedatabase.app" in u:
                tag = str(row.get("tag"))
                out.append((u, tag))
        return out
    except Exception as exc:
        logger.warning("could not load global firebases from DB: %s", exc)
        return []


def _save_global_firebases():
    if not supabase: return
    try:
        supabase.table("global_firebases").delete().neq("id", -1).execute() # Clear all
        payload = [{"url": url, "tag": tag} for url, tag in global_fb_list]
        if payload:
            supabase.table("global_firebases").insert(payload).execute()
    except Exception as exc:
        logger.warning("could not save global firebases: %s", exc)


def _retag_global_firebases():
    global global_fb_list
    global_fb_list = [(url, f"FB{i + 1}") for i, (url, _) in enumerate(global_fb_list)]


# ============================================================
# GLOBAL STATE
# ============================================================
REQUIRED_CHANNELS = _load_required_channels()
maintenance_mode = _load_maintenance_mode()
captcha_enabled = _load_captcha_enabled()
global_fb_list = _load_global_firebases()
_last_refresh_time: float = time.monotonic()

known_users = _load_user_ids()
user_access_state: Dict[int, bool] = {}
verified_access_users: Set[int] = set()

# ---- Referral DB ----
_referral_lock = asyncio.Lock()


def _load_referral_db() -> Dict[str, dict]:
    if not supabase: return {}
    try:
        res = supabase.table("referral_db").select("*").execute()
        db = {}
        for row in res.data:
            db[str(row["user_id"])] = row
        return db
    except Exception as exc:
        logger.warning("could not load referral db: %s", exc)
    return {}


def _save_referral_db():
    if not supabase: return
    try:
        if not REFERRAL_DB:
            return
        payload = []
        for k, v in REFERRAL_DB.items():
            record = dict(v)
            record["user_id"] = str(k)
            payload.append(record)
        supabase.table("referral_db").upsert(payload).execute()
    except Exception as exc:
        logger.warning("could not save referral db: %s", exc)


REFERRAL_DB: Dict[str, dict] = _load_referral_db()

_PHONE_PATTERNS = [
    re.compile(r'\b(?:\+91|91|0)?([6-9]\d{9})\b'),
    re.compile(r'\b(?:phone|mobile|number)[\s:]*([6-9]\d{9})\b', re.IGNORECASE),
    re.compile(r'[^0-9]([6-9]\d{9})[^0-9]'),
    re.compile(r'(\+91[-\s]?[6-9][0-9]{9})'),
    re.compile(r'(?:\b91)([6-9][0-9]{9})\b'),
    re.compile(r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)'),
]


# ============================================================
# REFERRAL HELPERS
# ============================================================
def _ensure_user_record(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in REFERRAL_DB:
        REFERRAL_DB[uid] = {
            "access_expires_at": 0.0,
            "referrals": [],
            "referred_by": None,
            "referral_count": 0,
            "expired_notified": False,
            "cooldown_until": 0.0,
            "captcha_verified": False,
        }
    else:
        rec = REFERRAL_DB[uid]
        rec.setdefault("access_expires_at", 0.0)
        rec.setdefault("referrals", [])
        rec.setdefault("referred_by", None)
        rec.setdefault("referral_count", len(rec.get("referrals", [])))
        rec.setdefault("expired_notified", False)
        rec.setdefault("cooldown_until", 0.0)
        rec.setdefault("captcha_verified", False)
    return REFERRAL_DB[uid]


def get_remaining_seconds(user_id: int) -> float:
    if user_id in ADMIN_IDS:
        return float('inf')
    rec = _ensure_user_record(user_id)
    return max(0.0, rec.get("access_expires_at", 0.0) - time.time())


def has_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    return get_remaining_seconds(user_id) > 0


def format_remaining_time(user_id: int) -> str:
    if user_id in ADMIN_IDS:
        return "Unlimited ♾️"
    remaining = get_remaining_seconds(user_id)
    if remaining <= 0:
        return "Expired"
    total_minutes = int(remaining // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h"
    return f"{minutes}m"


def grant_access(user_id: int, seconds: int):
    rec = _ensure_user_record(user_id)
    now = time.time()
    current = rec.get("access_expires_at", 0.0)
    if current < now:
        rec["access_expires_at"] = now + seconds
    else:
        rec["access_expires_at"] = current + seconds
    rec["expired_notified"] = False
    _save_referral_db()


def revoke_access(user_id: int):
    rec = _ensure_user_record(user_id)
    rec["access_expires_at"] = 0.0
    rec["expired_notified"] = False
    _save_referral_db()


async def process_referral(referrer_id: int, referred_id: int) -> dict:
    async with _referral_lock:
        if referrer_id == referred_id:
            return {"success": False, "reason": "self_referral"}
        if referrer_id in ADMIN_IDS:
            return {"success": False, "reason": "admin_referrer"}
        if str(referrer_id) not in REFERRAL_DB:
            return {"success": False, "reason": "invalid_referrer"}
        ref_rec = _ensure_user_record(referrer_id)
        new_rec = _ensure_user_record(referred_id)
        if new_rec.get("referred_by") is not None:
            return {"success": False, "reason": "already_referred"}
        if referred_id in ref_rec.get("referrals", []):
            return {"success": False, "reason": "duplicate"}
        now = time.time()
        current_expiry = ref_rec.get("access_expires_at", 0.0)
        if current_expiry < now:
            ref_rec["access_expires_at"] = now + REFERRAL_SECONDS
        else:
            ref_rec["access_expires_at"] = current_expiry + REFERRAL_SECONDS
        ref_rec["expired_notified"] = False
        ref_rec.setdefault("referrals", []).append(referred_id)
        ref_rec["referral_count"] = len(ref_rec["referrals"])
        new_rec["referred_by"] = referrer_id
        _save_referral_db()
        return {
            "success": True,
            "reason": "ok",
            "referrer_remaining": format_remaining_time(referrer_id),
        }


async def check_referred_user_left(referred_id: int):
    """When a referred user leaves channel, revoke referrer's access too."""
    try:
        uid = str(referred_id)
        if uid not in REFERRAL_DB:
            return None
        rec = REFERRAL_DB[uid]
        referrer_id = rec.get("referred_by")
        if not referrer_id:
            return None
        referrer_uid = str(referrer_id)
        if referrer_uid not in REFERRAL_DB:
            return None
        ref_rec = REFERRAL_DB[referrer_uid]
        if referred_id not in ref_rec.get("referrals", []):
            return None
        ref_rec["referrals"] = [
            r for r in ref_rec.get("referrals", []) if r != referred_id
        ]
        ref_rec["referral_count"] = len(ref_rec["referrals"])
        revoke_access(referrer_id)
        rec["referred_by"] = None
        _save_referral_db()
        logger.info(
            "[REFERRAL] User %s left channel → referrer %s access revoked",
            referred_id, referrer_id
        )
        return referrer_id
    except Exception as exc:
        logger.error("[REFERRAL] check_referred_user_left error: %s", exc)
        return None


# ============================================================
# FIREBASE HELPERS
# ============================================================
def normalize_fb_url(url: str) -> Optional[str]:
    try:
        if not url or not isinstance(url, str):
            return None
        u = url.strip()
        if not u.startswith("http"):
            return None
        if "firebaseio.com" not in u and "firebasedatabase.app" not in u:
            return None
        u = u.rstrip("/")
        while u.endswith(".json"):
            u = u[:-5].rstrip("/")
        return u
    except Exception:
        return None


def _load_global_device_cache():
    try:
        with open(GLOBAL_DEVICE_CACHE_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if not isinstance(saved, dict) or not isinstance(saved.get("devices"), dict):
            return {"devices": {}, "online_count": 0, "offline_count": 0,
                    "per_fb": {}, "updated_at": ""}
        if "per_fb" not in saved:
            saved["per_fb"] = {}
        return saved
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {"devices": {}, "online_count": 0, "offline_count": 0,
                "per_fb": {}, "updated_at": ""}


def _save_global_device_cache(cache):
    try:
        tmp = f"{GLOBAL_DEVICE_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, GLOBAL_DEVICE_CACHE_FILE)
    except OSError as exc:
        logger.warning("could not save device cache: %s", exc)


global_device_cache = _load_global_device_cache()


def fb_host_short(url: str) -> str:
    try:
        s = url.replace("https://", "").replace("http://", "")
        s = s.replace(".firebaseio.com", "").replace(".firebasedatabase.app", "")
        return s.split(".")[0][:30]
    except Exception:
        return url[:30]


def build_fb_endpoint(base_url: str, path: str = "", query: str = "") -> str:
    base = base_url.rstrip("/")
    p = (path or "").strip("/")
    q = (query or "").strip().lstrip("?")
    url = f"{base}/{p}/.json" if p else f"{base}/.json"
    if q:
        url += f"?{q}"
    return url


async def fb_get_json(session, url, *, timeout=FB_REQUEST_TIMEOUT,
                      max_bytes=FB_CLIENTS_MAX_BYTES, retries=FB_RETRY_MAX):
    last_err = ""
    for attempt in range(retries + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                status = resp.status
                if status == 413:
                    return None, "TOO_LARGE", "HTTP 413"
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return None, "TOO_LARGE", f"CL {cl}"
                body = b""
                async for chunk in resp.content.iter_chunked(8192):
                    body += chunk
                    if len(body) > max_bytes:
                        return None, "TOO_LARGE", "too big"
                text = body.decode("utf-8", errors="ignore").strip()
                if status in (401, 403):
                    return None, "ACCESS_DENIED", f"HTTP {status}"
                if status == 404:
                    return None, "NOT_FOUND", "HTTP 404"
                if status in (408, 504):
                    last_err = f"HTTP {status}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF ** attempt)
                        continue
                    return None, "TIMEOUT", last_err
                if status == 429 or 500 <= status < 600:
                    last_err = f"HTTP {status}"
                    if attempt < retries:
                        await asyncio.sleep(FB_RETRY_BACKOFF ** attempt)
                        continue
                    return None, "HTTP_ERROR", last_err
                if status >= 400:
                    return None, "HTTP_ERROR", f"HTTP {status}"
                if not text or text == "null":
                    return None, "EMPTY_DATA", "null/empty"
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None, "INVALID_JSON", "json decode"
                if data is None:
                    return None, "EMPTY_DATA", "null"
                return data, "SUCCESS", ""
        except asyncio.TimeoutError:
            last_err = "timeout"
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF ** attempt)
                continue
            return None, "TIMEOUT", last_err
        except aiohttp.ClientError as e:
            last_err = f"{type(e).__name__}"
            if attempt < retries:
                await asyncio.sleep(FB_RETRY_BACKOFF ** attempt)
                continue
            return None, "CONNECTION_ERROR", last_err
        except Exception as e:
            return None, "OTHER_ERROR", str(e)[:80]
    return None, "OTHER_ERROR", last_err or "retries exhausted"


def _get_device_name(info, cid):
    if not isinstance(info, dict):
        return cid
    for key in ("modelName", "model", "deviceName", "name"):
        v = info.get(key)
        if v and str(v).strip() and str(v) != "-":
            return str(v).strip()
    return cid


def _get_mob_no(info):
    if not isinstance(info, dict):
        return ""
    raw = (info.get("mobNo") or info.get("mob_no") or info.get("mobile")
           or info.get("phoneNumber") or info.get("phone") or "")
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return ""


def _newest_messages(msgs, limit=FB_MESSAGES_LIMIT):
    if not isinstance(msgs, dict):
        return []
    try:
        keys = sorted(msgs.keys(), key=lambda x: int(x), reverse=True)
    except (TypeError, ValueError):
        keys = list(msgs.keys())[::-1]
    return [msgs[k] for k in keys[:limit] if isinstance(msgs.get(k), dict)]


def _newest_with_keys(msgs, limit=FB_MESSAGES_LIMIT):
    if isinstance(msgs, list):
        out = []
        for i, value in list(enumerate(msgs))[-limit:][::-1]:
            if isinstance(value, dict):
                out.append((str(i), value))
        return out
    if not isinstance(msgs, dict):
        return []

    def _sort_token(k):
        s = str(k)
        try:
            return (0, int(s), "")
        except (TypeError, ValueError):
            return (1, 0, s)

    keys = sorted(msgs.keys(), key=_sort_token, reverse=True)
    out = []
    for k in keys[:limit]:
        v = msgs.get(k)
        if isinstance(v, dict):
            out.append((str(k), v))
    return out


def _extract_msg_body(m: dict) -> str:
    if not isinstance(m, dict):
        return ""
    for key in ("body", "message", "msg", "text", "content",
                "sms", "smsBody", "messageBody", "SMS", "msgBody"):
        v = m.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _extract_msg_sender(m: dict) -> str:
    if not isinstance(m, dict):
        return "Unknown"
    for key in ("sender", "from", "address", "number", "phone",
                "phoneNumber", "mobile", "mobNo", "src", "originator",
                "senderNumber", "fromNumber"):
        v = m.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "Unknown"


def _extract_msg_time(m: dict, fallback_key: str = "") -> str:
    if isinstance(m, dict):
        for key in ("time", "timestamp", "date", "receivedTime", "received_at",
                    "sentTime", "dateTime", "createdAt"):
            v = m.get(key)
            if v is not None and str(v).strip():
                return _format_time(v)
    if fallback_key:
        return _format_time(fallback_key)
    return "Unknown"


def _extract_otp(body: str) -> Optional[str]:
    if not body:
        return None
    text = str(body).replace("\u200b", " ").replace("\u00a0", " ")
    
    # Universal TEST code detection conditions
    patterns = [
        r'\b(?:OTP|code|verification|verify|login|passcode)\D{0,20}(\d{4,8})\b',
        r'\b(\d{4,8})\b(?:\D{0,20})(?:OTP|code|verification|verify|login)\b',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                candidate = match.group(1)
                if candidate and candidate.isdigit() and 4 <= len(candidate) <= 8:
                    return candidate
            except:
                pass
    
    return None


def _otp_short_message(sender: str, body: str) -> str:
    parts = [part for part in re.split(r"[-_\s]+", str(sender)) if part]
    brand = "SMS"
    for part in parts:
        p = part.strip()
        if len(p) >= 3 and p.upper() not in ("S", "VM", "MSG", "SMS"):
            brand = p
            break
    return f"{brand.title()} login code received."


def _extract_number(sender: str) -> str:
    if not sender:
        return ""
    digits = re.sub(r'\D', '', sender)
    if len(digits) >= 10:
        return digits[-10:]
    return ""


def extract_phone_from_messages(msgs) -> Optional[str]:
    try:
        if not isinstance(msgs, dict):
            return None
        counts = Counter()
        for m in _newest_messages(msgs, limit=15):
            if not isinstance(m, dict):
                continue
            text = _extract_msg_body(m)
            for pat in _PHONE_PATTERNS:
                for num in pat.findall(text):
                    d = re.sub(r'\D', '', num)
                    if len(d) == 10 and d[0] in '6789':
                        counts[d] += 1
                    elif len(d) == 12 and d.startswith('91') and d[2] in '6789':
                        counts[d[2:]] += 1
        return counts.most_common(1)[0][0] if counts else None
    except Exception:
        return None


# ============================================================
# FIREBASE FETCH
# ============================================================
async def _try_fetch_clients(session, fb_url: str):
    data, status, _ = await fb_get_json(session, build_fb_endpoint(fb_url, ""))
    if status == "SUCCESS" and isinstance(data, dict):
        clients = None
        if isinstance(data.get("clients"), dict):
            clients = data["clients"]
        elif isinstance(data.get("devices"), dict):
            clients = data["devices"]
        messages = data.get("messages") if isinstance(data.get("messages"), dict) else {}
        if isinstance(clients, dict) and clients:
            return clients, messages
    (cdata, cst, _), (ddata, dst, _) = await asyncio.gather(
        fb_get_json(session, build_fb_endpoint(fb_url, "clients"),
                    max_bytes=FB_CLIENTS_MAX_BYTES, retries=0),
        fb_get_json(session, build_fb_endpoint(fb_url, "devices"),
                    max_bytes=FB_CLIENTS_MAX_BYTES, retries=0),
    )
    if cst == "SUCCESS" and isinstance(cdata, dict) and cdata:
        return cdata, {}
    if dst == "SUCCESS" and isinstance(ddata, dict) and ddata:
        return ddata, {}
    return {}, {}


async def _fetch_phone_lookup(session, fb_url: str, cid: str):
    try:
        murl = build_fb_endpoint(fb_url, f"messages/{cid}",
                                 query='orderBy="$key"&limitToLast=15')
        fetched, status, _ = await fb_get_json(
            session, murl, max_bytes=FB_CLIENTS_MAX_BYTES,
            timeout=min(FB_REQUEST_TIMEOUT, 4), retries=0)
        if status == "SUCCESS" and isinstance(fetched, dict):
            return cid, extract_phone_from_messages(fetched)
    except Exception:
        pass
    return cid, None


async def fetch_devices_from_one(fb_url: str, fb_tag: str,
                                 only_online: bool = True,
                                 prefetched=None) -> Dict[str, dict]:
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT))
    result = {}
    try:
        clients, messages_node = prefetched or await _try_fetch_clients(session, fb_url)
        if not clients:
            return {}
        phone_lookups = {}
        lookup_ids = [
            str(cid) for cid, info in clients.items()
            if isinstance(info, dict)
            and (not only_online or info.get("status") is True)
            and not _get_mob_no(info)
            and not isinstance(messages_node.get(cid), dict)
        ][:50]
        if lookup_ids:
            lookup_results = await asyncio.gather(*[
                _fetch_phone_lookup(session, fb_url, cid) for cid in lookup_ids
            ], return_exceptions=True)
            phone_lookups = {
                cid: phone for result in lookup_results
                if isinstance(result, tuple)
                for cid, phone in [result]
                if phone
            }
        for cid, info in clients.items():
            try:
                if not isinstance(info, dict):
                    continue
                is_online = info.get("status") is True
                if only_online and not is_online:
                    continue
                if not only_online and is_online:
                    continue
                phone = _get_mob_no(info)
                if not phone:
                    m_data = messages_node.get(cid) if isinstance(messages_node, dict) else None
                    if not isinstance(m_data, dict):
                        try:
                            ext = phone_lookups.get(str(cid))
                            if ext:
                                phone = ext
                        except Exception:
                            pass
                    if isinstance(m_data, dict) and m_data:
                        ext = extract_phone_from_messages(m_data)
                        if ext:
                            phone = ext
                if not phone:
                    continue
                prefixed_id = f"{fb_tag}|{cid}"
                result[prefixed_id] = {
                    "name": _get_device_name(info, cid),
                    "phone": phone,
                    "raw": info,
                    "online": is_online,
                    "fb_url": fb_url,
                    "fb_tag": fb_tag,
                    "real_cid": cid,
                }
            except Exception:
                continue
        return result
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def fetch_counts_from_one(fb_url: str, fb_tag: str, prefetched=None) -> Tuple[int, int]:
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT))
    online = 0
    offline = 0
    try:
        clients, _ = prefetched or await _try_fetch_clients(session, fb_url)
        if not clients:
            return 0, 0
        for cid, info in clients.items():
            try:
                if not isinstance(info, dict):
                    continue
                is_online = info.get("status") is True
                if is_online:
                    online += 1
                else:
                    offline += 1
            except Exception:
                continue
        return online, offline
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def fetch_counts_all_firebases(fb_list: List[tuple], prefetched=None) -> Tuple[int, int, Dict[str, dict]]:
    prefetched = prefetched or {}
    tasks = [fetch_counts_from_one(url, tag, prefetched=prefetched.get(url))
             for url, tag in fb_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_online = 0
    total_offline = 0
    per_fb: Dict[str, dict] = {}
    for (url, tag), r in zip(fb_list, results):
        if isinstance(r, Exception) or not r:
            per_fb[tag] = {"online": 0, "offline": 0}
            continue
        per_fb[tag] = {"online": r[0], "offline": r[1]}
        total_online += r[0]
        total_offline += r[1]
    return total_online, total_offline, per_fb


async def fetch_devices_all_firebases(fb_list: List[tuple],
                                       only_online: bool = True,
                                       max_per_fb: int = 100,
                                       prefetched=None) -> Dict[str, dict]:
    prefetched = prefetched or {}
    tasks = [fetch_devices_from_one(url, tag, only_online,
                                     prefetched=prefetched.get(url))
             for url, tag in fb_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged = {}
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        for k, v in list(r.items())[:max_per_fb]:
            merged[k] = v
    return merged


async def refresh_global_device_cache():
    global global_device_cache, _last_refresh_time
    if not global_fb_list:
        global_device_cache = {
            "devices": {}, "online_count": 0, "offline_count": 0,
            "per_fb": {},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_global_device_cache(global_device_cache)
        _last_refresh_time = time.monotonic()
        return global_device_cache
    try:
        devices_task = fetch_devices_all_firebases(
            global_fb_list, only_online=True, max_per_fb=150)
        counts_task = fetch_counts_all_firebases(global_fb_list)
        devices, (online_count, offline_count, per_fb) = await asyncio.gather(
            devices_task, counts_task)
        global_device_cache = {
            "devices": devices or {},
            "online_count": len(devices or {}),
            "raw_online_count": online_count,
            "offline_count": offline_count,
            "per_fb": per_fb or {},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_global_device_cache(global_device_cache)
        _last_refresh_time = time.monotonic()
    except Exception as exc:
        logger.error("global device cache refresh failed: %s", exc)
    return global_device_cache


async def _global_device_refresh_loop():
    global _last_refresh_time
    while True:
        try:
            await refresh_global_device_cache()
            _last_refresh_time = time.monotonic()
            await asyncio.sleep(ADMIN_DEVICE_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("global device refresh loop: %s", exc)
            await asyncio.sleep(ADMIN_DEVICE_REFRESH_INTERVAL)


async def fetch_last_sms(fb_url: str, device_id: str, limit: int = FB_MESSAGES_LIMIT):
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT))
    try:
        url = build_fb_endpoint(fb_url, f"messages/{device_id}",
                                query=f'orderBy="$key"&limitToLast={limit}')
        data, status, _ = await fb_get_json(session, url)
        if status == "SUCCESS" and isinstance(data, (dict, list)):
            return _newest_with_keys(data, limit=limit)
        url = build_fb_endpoint(fb_url, f"messages/{device_id}")
        data, status, _ = await fb_get_json(session, url)
        if status != "SUCCESS" or not isinstance(data, (dict, list)):
            return []
        return _newest_with_keys(data, limit=limit)
    finally:
        try:
            await session.close()
        except Exception:
            pass


# ============================================================
# SESSION STORE
# ============================================================
user_sessions: Dict[int, dict] = {}
sms_monitor_tasks: Dict[int, asyncio.Task] = {}
sms_monitor_state: Dict[int, dict] = {}
admin_panel_live_tasks: Dict[int, asyncio.Task] = {}
bot_instance: Optional[Bot] = None
BOT_USERNAME: str = "Otp_random_bot"


# ============================================================
# FORCE JOIN HELPERS
# ============================================================
def _is_member_status(status: str) -> bool:
    return status in {"member", "administrator", "creator"}


async def get_unjoined_channels(bot, user_id: int) -> List[dict]:
    unjoined = []
    if user_id in ADMIN_IDS:
        return unjoined
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if not _is_member_status(member.status):
                unjoined.append(channel)
        except Exception:
            unjoined.append(channel)
    return unjoined


async def check_force_join(bot, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if not REQUIRED_CHANNELS:
        return True
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if not _is_member_status(member.status):
                return False
        except Exception:
            return False
    return True


async def _check_required_channels(bot, uid: int):
    missing = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=uid)
            if not _is_member_status(member.status):
                missing.append(channel["label"])
        except Exception as exc:
            logger.warning("force join check failed for %s: %s", channel["id"], exc)
            missing.append(channel["label"])
    return not missing, missing


def build_dynamic_force_join_keyboard(unjoined_channels: List[dict]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    styles = ["danger", "primary", "danger", "primary"]
    for i, c in enumerate(unjoined_channels):
        style = styles[i % len(styles)]
        try:
            btn = InlineKeyboardButton(
                text=f"𝗝𝗢𝗜𝗡 {c.get('label','CHANNEL')[:25]}",
                url=c["url"],
                api_kwargs={"style": style}
            )
        except TypeError:
            btn = InlineKeyboardButton(
                text=f"𝗝𝗢𝗜𝗡 {c.get('label','CHANNEL')[:25]}", url=c["url"])
        rows.append([btn])
    try:
        check_btn = InlineKeyboardButton(
            text="✅ 𝗖𝗛𝗘𝗖𝗞 𝗠𝗘𝗠𝗕𝗘𝗥𝗦𝗛𝗜𝗣",
            callback_data="force_verify",
            api_kwargs={"style": "success"})
    except TypeError:
        check_btn = InlineKeyboardButton(
            text="✅ 𝗖𝗛𝗘𝗖𝗞 𝗠𝗘𝗠𝗕𝗘𝗥𝗦𝗛𝗜𝗣", callback_data="force_verify")
    rows.append([check_btn])
    return InlineKeyboardMarkup(rows)


def _force_join_kb():
    rows = []
    for channel in REQUIRED_CHANNELS:
        if channel.get("url"):
            try:
                rows.append([InlineKeyboardButton(
                    f"🔗 𝗝𝗢𝗜𝗡 {channel['label']}",
                    url=channel["url"],
                    api_kwargs={"style": "primary"})])
            except TypeError:
                rows.append([InlineKeyboardButton(
                    f"🔗 𝗝𝗢𝗜𝗡 {channel['label']}", url=channel["url"])])
    rows.append([styled_button("✅ 𝗩𝗘𝗥𝗜𝗙𝗬 𝗝𝗢𝗜𝗡", "force_verify", "success")])
    return InlineKeyboardMarkup(rows)


def build_forcejoin_caption(first_name: str = "User") -> str:
    return (
        "🔗 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 𝗝𝗢𝗜𝗡 𝗥𝗘𝗤𝗨𝗜𝗥𝗘𝗗\n\n"
        f"👋 Hi {first_name}!\n\n"
        "Join all channels below, then tap\n"
        "✅ 𝗖𝗛𝗘𝗖𝗞 𝗠𝗘𝗠𝗕𝗘𝗥𝗦𝗛𝗜𝗣"
    )


def build_welcome_caption_joined(first_name: str, user_id: int) -> str:
    safe_name = (first_name or "User").strip()
    refer_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    if user_id in ADMIN_IDS:
        remaining = "Unlimited ♾️"
    else:
        remaining = format_remaining_time(user_id)

    rec = _ensure_user_record(user_id)
    ref_count = rec.get("referral_count", 0)

    return (
        f"🌸 𝗛𝗶𝗶 {safe_name} ⚡\n\n"
        "🚀 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗢𝗧𝗣 𝗕𝗼𝘁 ✴️\n\n"
        f"⏳ 𝗔𝗖𝗖𝗘𝗦𝗦 : {remaining}\n\n"
        f"🔗 𝗬𝗢𝗨𝗥 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗟𝗜𝗡𝗞 :\n<code>{refer_link}</code>\n\n"
        f"📊 𝗧𝗢𝗧𝗔𝗟 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟𝗦 : {ref_count}\n\n"
        "🎁 𝟭 𝗥𝗘𝗙𝗘𝗥 = 𝟯 𝗛𝗢𝗨𝗥𝗦 𝗔𝗖𝗖𝗘𝗦𝗦\n\n"
        "👇 𝗧𝗮𝗽 𝗯𝗲𝗹𝗼𝘄 𝘁𝗼 𝘀𝘁𝗮𝗿𝘁"
    )


# ============================================================
# CAPTCHA
# ============================================================
def _new_math_captcha():
    a = random.randint(2, 20)
    b = random.randint(2, 20)
    op = random.choice(("+", "-", "×"))
    answer = a + b if op == "+" else a - b if op == "-" else a * b
    return f"{a} {op} {b}", answer


# ============================================================
# SEND WELCOME
# ============================================================
async def send_welcome_photo(chat_id: int, first_name: str, *,
                              user_id: int = 0,
                              show_force_join: bool,
                              reply_markup=None, bot=None):
    try:
        if show_force_join:
            caption = build_forcejoin_caption(first_name)
        else:
            caption = build_welcome_caption_joined(first_name, user_id)
        payload = {
            "chat_id": chat_id,
            "photo": WELCOME_IMAGE_URL,
            "caption": _bold_blockquote(caption),
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            if hasattr(reply_markup, "to_dict"):
                payload["reply_markup"] = reply_markup.to_dict()
            else:
                payload["reply_markup"] = reply_markup
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return
                if bot:
                    await bot.send_message(
                        chat_id=chat_id, text=_bold_blockquote(caption),
                        parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"[WELCOME] error: {e}")


# ============================================================
# ACCESS MIDDLEWARE
# ============================================================
async def _require_access(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          *, edit_target=None):
    user = update.effective_user
    uid = user.id

    # 1) Maintenance
    if maintenance_mode and uid not in ADMIN_IDS:
        maintenance_text = _bold_blockquote(
            "🛠️ 𝗕𝗢𝗧 𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘 𝗠𝗢𝗗𝗘 𝗢𝗡 ⚙️")
        if edit_target is not None and update.callback_query:
            try:
                cq = update.callback_query
                if getattr(cq.message, "photo", None):
                    await cq.edit_message_caption(
                        caption=maintenance_text, parse_mode="HTML")
                else:
                    await cq.edit_message_text(
                        maintenance_text, parse_mode="HTML")
            except Exception:
                pass
        elif update.effective_message:
            try:
                await update.effective_message.reply_text(
                    maintenance_text, parse_mode="HTML")
            except Exception:
                pass
        return False

    # 2) Force Join
    if REQUIRED_CHANNELS and uid not in ADMIN_IDS:
        joined = await check_force_join(context.bot, uid)
        user_access_state[uid] = joined
        if not joined:
            verified_access_users.discard(uid)
            context.user_data.pop("force_join_captcha", None)
            context.user_data.pop("force_join_verified", None)
            unjoined = await get_unjoined_channels(context.bot, uid)
            kb = build_dynamic_force_join_keyboard(unjoined)
            caption = build_forcejoin_caption(user.first_name or "User")
            if edit_target is not None and update.callback_query:
                try:
                    await update.callback_query.edit_message_caption(
                        caption=_bold_blockquote(caption),
                        parse_mode="HTML", reply_markup=kb)
                    return False
                except Exception:
                    pass
            chat_id = (update.effective_chat.id if update.effective_chat
                       else update.callback_query.message.chat_id)
            await send_welcome_photo(
                chat_id=chat_id, first_name=user.first_name or "User",
                user_id=uid, show_force_join=True,
                reply_markup=kb, bot=context.bot)
            return False

        # 3) Captcha
        if (captcha_enabled and uid not in ADMIN_IDS
                and uid not in verified_access_users
                and not context.user_data.get("force_join_verified")):
            question, answer = _new_math_captcha()
            context.user_data["force_join_captcha"] = answer
            captcha_text = _bold_blockquote(
                f"🔐 𝗩𝗘𝗥𝗜𝗙𝗬 𝗖𝗔𝗣𝗧𝗖𝗛𝗔\n\n"
                f"🧮 Solve: {question} = ?")
            if edit_target is not None and update.callback_query:
                try:
                    cq = update.callback_query
                    if getattr(cq.message, "photo", None):
                        await cq.edit_message_caption(
                            caption=captcha_text, parse_mode="HTML")
                    else:
                        await cq.edit_message_text(
                            captcha_text, parse_mode="HTML")
                    return False
                except Exception:
                    pass
            chat_id = (update.effective_chat.id if update.effective_chat
                       else update.callback_query.message.chat_id)
            await context.bot.send_message(
                chat_id=chat_id, text=captcha_text, parse_mode="HTML")
            return False

    # 4) Access Time Check
    if uid not in ADMIN_IDS and not has_access(uid):
        access_msg = _bold_blockquote(
            "⚠️ 𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗦𝗧𝗥𝗜𝗖𝗧𝗘𝗗\n\n"
            "🔒 𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗲𝘅𝗽𝗶𝗿𝗲𝗱.\n\n"
            "🎁 𝟭 𝗥𝗘𝗙𝗘𝗥 = 𝟯 𝗛𝗢𝗨𝗥𝗦 𝗔𝗖𝗖𝗘𝗦𝗦\n\n"
            "🔗 𝗬𝗢𝗨𝗥 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗟𝗜𝗡𝗞 :\n"
            f"<code>https://t.me/{BOT_USERNAME}?start=ref_{uid}</code>\n\n"
            "📌 𝗦𝗵𝗮𝗿𝗲 𝘁𝗵𝗶𝘀 𝗹𝗶𝗻𝗸 𝘄𝗶𝘁𝗵 𝗳𝗿𝗶𝗲𝗻𝗱𝘀 𝘁𝗼 𝗴𝗲𝘁 𝗮𝗰𝗰𝗲𝘀𝘀!")
        if edit_target is not None and update.callback_query:
            try:
                cq = update.callback_query
                if getattr(cq.message, "photo", None):
                    await cq.edit_message_caption(
                        caption=access_msg, parse_mode="HTML")
                else:
                    await cq.edit_message_text(
                        access_msg, parse_mode="HTML")
                return False
            except Exception:
                pass
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    access_msg, parse_mode="HTML")
            except Exception:
                pass
        return False

    return True


# ============================================================
# FORCE VERIFY CALLBACK
# ============================================================
async def force_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    first_name = q.from_user.first_name or "User"

    if maintenance_mode and uid not in ADMIN_IDS:
        try:
            await q.edit_message_caption(
                caption=_bold_blockquote("🛠️ 𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘 𝗠𝗢𝗗𝗘 𝗢𝗡"),
                parse_mode="HTML")
        except Exception:
            pass
        return

    joined = await check_force_join(context.bot, uid)
    user_access_state[uid] = joined

    if joined:
        verified_access_users.discard(uid)
        context.user_data.pop("force_join_captcha", None)
        context.user_data.pop("force_join_verified", None)
        try:
            await q.answer("✅ All channels joined!")
        except Exception:
            pass
        try:
            await q.message.delete()
        except Exception:
            pass
        if captcha_enabled and uid not in ADMIN_IDS:
            question, answer = _new_math_captcha()
            context.user_data["force_join_captcha"] = answer
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=_bold_blockquote(
                    f"🔐 𝗩𝗘𝗥𝗜𝗙𝗬 𝗖𝗔𝗣𝗧𝗖𝗛𝗔\n\n"
                    f"🧮 Solve: {question} = ?"),
                parse_mode="HTML")
        else:
            await _send_main_menu(context, q.message.chat_id, first_name, uid)
    else:
        verified_access_users.discard(uid)
        context.user_data.pop("force_join_captcha", None)
        unjoined = await get_unjoined_channels(context.bot, uid)
        try:
            await q.answer("❌ Join all channels first!", show_alert=True)
        except Exception:
            pass
        kb = build_dynamic_force_join_keyboard(unjoined)
        try:
            await q.message.delete()
        except Exception:
            pass
        await send_welcome_photo(
            chat_id=q.message.chat_id, first_name=first_name,
            user_id=uid, show_force_join=True,
            reply_markup=kb, bot=context.bot)


# ============================================================
# MAIN MENU
# ============================================================
def connect_inline_kb():
    return InlineKeyboardMarkup([
        [styled_button("🎲 𝗚𝗘𝗡𝗘𝗥𝗔𝗧𝗘 𝗡𝗨𝗠𝗕𝗘𝗥", "generate_number", "success")],
    ])


async def _send_main_menu(context, chat_id: int, first_name: str, uid: int):
    kb = connect_inline_kb()
    caption = _bold_blockquote(build_welcome_caption_joined(first_name, uid))

    # Try to fetch user's own profile photo
    user_photo_id = None
    try:
        photos = await context.bot.get_user_profile_photos(user_id=uid, limit=1)
        if photos and photos.total_count > 0:
            user_photo_id = photos.photos[0][-1].file_id
    except Exception as exc:
        logger.warning("Could not fetch user profile photo for uid=%s: %s", uid, exc)

    photo_to_send = user_photo_id if user_photo_id else WELCOME_IMAGE_URL

    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_to_send,
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as exc:
        logger.warning("welcome image failed, text fallback: %s", exc)
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=kb,
        )


# ============================================================
# STYLING HELPERS
# ============================================================
def make_blockquote(text: str) -> str:
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("<blockquote>") or stripped.startswith(">"):
        return text
    return f"<blockquote>{text}</blockquote>"


def _bold_blockquote(text: str) -> str:
    if str(text).lstrip().startswith("<blockquote>"):
        return str(text)
    raw = str(text)

    def convert_plain(value: str) -> str:
        converted = []
        for char in value.replace("*", ""):
            code = ord(char)
            if "A" <= char <= "Z":
                converted.append(chr(0x1D5D4 + (code - ord("A"))))
            elif "a" <= char <= "z":
                converted.append(chr(0x1D5EE + (code - ord("a"))))
            elif "0" <= char <= "9":
                converted.append(chr(0x1D7EC + (code - ord("0"))))
            else:
                converted.append(char)
        return html_escape("".join(converted), quote=False)

    pieces = []
    cursor = 0
    pattern = re.compile(
        r"(?P<html_code><code>(?P<html_inner>.*?)</code>)|(?P<bt>`(?P<bt_inner>[^`]+)`)",
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(raw):
        pieces.append(convert_plain(raw[cursor:match.start()]))
        if match.group("html_code"):
            inner = match.group("html_inner")
            pieces.append(f"<code>{html_escape(inner, quote=False)}</code>")
        else:
            pieces.append(f"<code>{html_escape(match.group('bt_inner'), quote=False)}</code>")
        cursor = match.end()
    pieces.append(convert_plain(raw[cursor:]))
    return f"<blockquote>{''.join(pieces)}</blockquote>"


def styled_button(text: str, callback_data: str, style: str = None,
                  icon_custom_emoji_id: str = None):
    kwargs = {"callback_data": callback_data}
    if style:
        kwargs["api_kwargs"] = {"style": style}
        if icon_custom_emoji_id:
            kwargs["api_kwargs"]["icon_custom_emoji_id"] = icon_custom_emoji_id
    try:
        return InlineKeyboardButton(text, **kwargs)
    except TypeError:
        return InlineKeyboardButton(text, callback_data=callback_data)


# ============================================================
# TIME FORMATTER
# ============================================================
def _format_time(ts_key) -> str:
    if not ts_key:
        return "Unknown"
    try:
        s = str(ts_key).strip()
        if re.fullmatch(r'\d+', s):
            n = int(s)
            if n > 10**12:
                n = n // 1000
            elif n < 10**9:
                return s
            dt = datetime.fromtimestamp(n)
            return dt.strftime("%d-%m-%Y | %I:%M:%S %p")
        return s
    except Exception:
        return str(ts_key)


# ============================================================
# ADMIN KEYBOARDS
# ============================================================
def admin_panel_kb():
    maintenance_label = "🟢 TURN BOT ON" if maintenance_mode else "🔴 TURN BOT OFF"
    captcha_label = "🔐 CAPTCHA ON" if captcha_enabled else "🔓 CAPTCHA OFF"
    return InlineKeyboardMarkup([
        [styled_button("➕ ADD FIREBASE", "admin_add_firebase", "success")],
        [styled_button("📋 MANAGE FIREBASES", "admin_manage_fb", "primary")],
        [styled_button("📊 BOT STATISTICS", "admin_stats", "primary")],
        [styled_button("🎁 GIFT ACCESS", "admin_gift_access", "success")],
        [styled_button("📢 BROADCAST", "admin_broadcast", "success")],
        [styled_button(maintenance_label, "admin_toggle_maintenance", "danger")],
        [styled_button(captcha_label, "admin_toggle_captcha", "primary")],
        [styled_button("➕ ADD FORCE JOIN CHANNEL", "admin_add_channel", "success")],
        [styled_button("📋 FORCE JOIN CHANNELS", "admin_channels", "primary")],
    ])


def admin_gift_kb():
    return InlineKeyboardMarkup([
        [styled_button("👤 GIFT SINGLE USER", "admin_gift_single", "primary")],
        [styled_button("👥 GIFT ALL USERS", "admin_gift_all", "success")],
        [styled_button("🔙 ADMIN PANEL", "admin_back", "danger")],
    ])


def admin_firebases_kb():
    rows = []
    if not global_fb_list:
        rows.append([styled_button("➕ ADD FIREBASE", "admin_add_firebase", "success")])
    else:
        for i, (url, tag) in enumerate(global_fb_list):
            rows.append([styled_button(f"🔥 {tag}", f"admin_fb_info:{i}", "primary")])
            rows.append([
                styled_button("🔄 REFRESH", f"admin_fb_refresh:{i}", "success"),
                styled_button("🗑 DELETE", f"admin_fb_delete:{i}", "danger"),
            ])
        if len(global_fb_list) < MAX_FIREBASES:
            rows.append([styled_button("➕ ADD FIREBASE", "admin_add_firebase", "success")])
    rows.append([styled_button("🔙 ADMIN PANEL", "admin_back", "danger")])
    return InlineKeyboardMarkup(rows)


def admin_back_kb():
    return InlineKeyboardMarkup([
        [styled_button("🔙 ADMIN PANEL", "admin_back", "danger")]])


def admin_channels_kb():
    rows = []
    for index, channel in enumerate(REQUIRED_CHANNELS):
        rows.append([styled_button(
            f"🗑 REMOVE {channel.get('label', channel.get('id', '?'))[:35]}",
            f"admin_remove_channel:{index}", "danger")])
    rows.append([styled_button("➕ ADD CHANNEL", "admin_add_channel", "success")])
    rows.append([styled_button("🔙 ADMIN PANEL", "admin_back", "primary")])
    return InlineKeyboardMarkup(rows)


DEVICES_PER_PAGE = 6


def device_list_kb(devices: Dict[str, dict], mode: str = "online", page: int = 0):
    items = list(devices.items())[:80]
    total_pages = max(1, (len(items) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DEVICES_PER_PAGE
    page_items = items[start:start + DEVICES_PER_PAGE]
    rows = []
    for cid, info in page_items:
        phone = str(info.get("phone") or "").strip()
        label = f"🟢 {cid}"
        if phone and phone not in ("—", "N/A"):
            label += f"  |  {phone}"
        rows.append([styled_button(label[:60], f"dev:{cid}", "success")])
    nav = []
    if page > 0:
        nav.append(styled_button("◀️ PREV", f"devpage:{page-1}", "primary"))
    nav.append(styled_button(f"📄 {page+1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav.append(styled_button("NEXT ▶️", f"devpage:{page+1}", "primary"))
    rows.append(nav)
    rows.append([styled_button("🔄 REFRESH", "scan_active", "primary")])
    rows.append([styled_button("🔙 BACK", "menu_back", "danger")])
    return InlineKeyboardMarkup(rows)


def device_actions_kb(device_id: str, mode: str = "online"):
    return InlineKeyboardMarkup([
        [styled_button("📩 LAST 5 SMS", f"sms:{device_id}", "primary")],
        [styled_button("🎲 GENERATE AGAIN", "generate_number", "success")],
        [styled_button("🔙 BACK TO DEVICE LIST", "device_list", "danger")],
    ])


def sms_view_kb(device_id: str, mode: str = "online"):
    return InlineKeyboardMarkup([
        [styled_button("🔄 REFRESH", f"sms_refresh:{device_id}", "primary")],
        [styled_button("🎲 GENERATE AGAIN", "generate_number", "success")],
        [styled_button("🔙 BACK TO DEVICE", f"dev:{device_id}", "danger")],
    ])


def sms_monitor_kb(device_id: str):
    return InlineKeyboardMarkup([
        [styled_button("⛔ STOP SMS MONITOR", f"smsmon_stop:{device_id}", "danger")],
        [styled_button("🔙 BACK TO DEVICE", f"dev:{device_id}", "primary")],
    ])


def manage_fb_kb(uid: int):
    sess = user_sessions.get(uid, {})
    fb_list = sess.get("fb_list", [])
    rows = []
    for i, (url, tag) in enumerate(fb_list):
        short = fb_host_short(url)
        rows.append([styled_button(f"{tag} — {short}", f"fbnoop:{i}", "primary")])
        rows.append([
            styled_button("👁 VIEW", f"scan_fb:{i}", "success"),
            styled_button("🗑 DELETE", f"delete_fb:{i}", "danger"),
        ])
    if len(fb_list) < MAX_FIREBASES:
        rows.append([styled_button("➕ ADD FIREBASE", "add_fb", "success")])
    return InlineKeyboardMarkup(rows)


def firebase_connected_kb(uid: int):
    return InlineKeyboardMarkup([
        [styled_button("🔎 SCAN ACTIVE", "scan_active", "success")],
        [styled_button("🔗 MANAGE FIREBASE", "manage_firebase", "primary")],
    ])


# ============================================================
# ADMIN PANEL LIVE
# ============================================================
def _seconds_until_next_refresh() -> int:
    elapsed = time.monotonic() - _last_refresh_time
    remaining = ADMIN_DEVICE_REFRESH_INTERVAL - elapsed
    return max(0, int(remaining))


def _build_admin_fb_text() -> str:
    if not global_fb_list:
        return ("📋 *Manage Firebases*\n\n"
                "Abhi koi Firebase add nahi hai.\n"
                "➕ ADD FIREBASE se URL add karo.")
    lines = ["📋 *Manage Firebases*\n"]
    per_fb = global_device_cache.get("per_fb", {}) or {}
    for url, tag in global_fb_list:
        counts = per_fb.get(tag) or {}
        online = int(counts.get("online", 0))
        offline = int(counts.get("offline", 0))
        lines.append(f"*{tag}*\n   🟢 {online}  |  🔴 {offline}  |  📊 {online + offline}")
    lines.append(f"\nTotal panels: {len(global_fb_list)}/{MAX_FIREBASES}")
    lines.append(f"Cache: `{global_device_cache.get('updated_at') or 'not loaded'}`")
    remaining = _seconds_until_next_refresh()
    mins, secs = divmod(remaining, 60)
    lines.append(f"\n⏳ Next auto-refresh in: *{mins}:{secs:02d}*")
    lines.append("🔄 REFRESH se abhi update karo.")
    return "\n".join(lines)


async def _admin_panel_live_loop(bot, uid: int, chat_id: int, message_id: int):
    last_rendered = ""
    try:
        while True:
            await asyncio.sleep(ADMIN_PANEL_EDIT_INTERVAL)
            if uid not in admin_panel_live_tasks:
                break
            text = _build_admin_fb_text()
            if text == last_rendered:
                continue
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text,
                    parse_mode="Markdown", reply_markup=admin_firebases_kb())
                last_rendered = text
            except Exception as exc:
                msg = str(exc).lower()
                if "message is not modified" in msg:
                    last_rendered = text
                    continue
                if "message to edit not found" in msg or "message can't be edited" in msg:
                    break
                logger.info("admin panel live edit failed: %s", exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("admin panel live loop crashed: %s", exc)
    finally:
        admin_panel_live_tasks.pop(uid, None)


def _stop_admin_panel_live_task(uid: int):
    task = admin_panel_live_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()


async def _start_admin_panel_live_task(bot, uid: int, chat_id: int, message_id: int):
    _stop_admin_panel_live_task(uid)
    task = asyncio.create_task(_admin_panel_live_loop(bot, uid, chat_id, message_id))
    admin_panel_live_tasks[uid] = task


# ============================================================
# SESSION
# ============================================================
async def _ensure_session(uid: int):
    sess = user_sessions.get(uid)
    if not sess:
        sess = {"fb_list": [], "active_fb_idx": 0, "devices": {},
                "current_device": "", "mode": "online",
                "awaiting_fb_add": False, "device_page": 0}
        user_sessions[uid] = sess
    return sess


async def _show_cached_device_list(q, sess):
    devices = sess.get("devices", {})
    page = int(sess.get("device_page", 0) or 0)
    items = list(devices.items())[:80]
    total_pages = max(1, (len(items) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    sess["device_page"] = page
    online_count = sess.get("online_count", 0)
    offline_count = sess.get("offline_count", 0)
    total_count = sess.get("total_count", online_count + offline_count)
    await q.edit_message_text(
        f"📞 *DEVICES INFO*\n\n"
        f"🟢 ONLINE : {online_count}\n"
        f"🔴 OFFLINE : {offline_count}\n"
        f"📊 TOTAL : {total_count}\n"
        f"🔗 Firebase: {len(sess.get('fb_list', []))}\n\n"
        "Tap a device below.",
        parse_mode="Markdown",
        reply_markup=device_list_kb(devices, mode="online", page=page))


# ============================================================
# DEVICE VIEW BUILDER (reusable)
# ============================================================
def _build_device_view(device_id: str, info: dict):
    """Return (text, reply_markup, mode) for a device info screen."""
    tag = info.get("fb_tag", "?")
    real_cid = info.get("real_cid", device_id)
    raw = info.get("raw") or {}
    phone = info.get("phone") or "N/A"
    online = bool(info.get("online"))
    network = raw.get("network") or raw.get("operator") or "—"
    android = raw.get("android") or raw.get("androidVersion") or raw.get("os") or "—"
    battery = raw.get("battery") or raw.get("batteryLevel") or "—"
    if isinstance(battery, (int, float)):
        battery = f"{int(battery)}%"
    text = (f"📱 *{real_cid}*\n\n"
            f"🌐 Firebase: `{tag}`\n"
            f"🆔 ID: `{real_cid}`\n"
            f"📡 Status: {'🟢 ONLINE' if online else '🔴 OFFLINE'}\n"
            f"📞 Phone: `{phone}`\n"
            f"📶 Network: {network}\n"
            f"🤖 Android: {android}\n"
            f"🔋 Battery: {battery}")
    mode = "online" if online else "offline"
    return text, device_actions_kb(device_id, mode), mode


async def _show_device_view(q, sess, device_id: str):
    """Show device info screen, with fallback rebuild if info is missing."""
    info = sess.get("devices", {}).get(device_id)
    if not info:
        parsed_tag, parsed_cid = _parse_prefixed(device_id)
        fb_url = _find_fb_url_by_tag(q.from_user.id, parsed_tag)
        if fb_url:
            info = {
                "fb_url": fb_url,
                "fb_tag": parsed_tag,
                "real_cid": parsed_cid,
                "phone": "",
                "online": True,
                "raw": {},
            }
            sess.setdefault("devices", {})[device_id] = info
        else:
            await _safe_edit_callback_message(
                q,
                "❌ *Device not found.*\n\nPlease tap REFRESH.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 REFRESH", callback_data="scan_active")],
                    [InlineKeyboardButton("🔙 BACK", callback_data="menu_back")],
                ]))
            return False
    text, markup, mode = _build_device_view(device_id, info)
    sess["current_device"] = device_id
    sess["mode"] = mode
    await _safe_edit_callback_message(q, text, parse_mode="Markdown",
                                       reply_markup=markup)
    return True


# ============================================================
# /start
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    first_name = update.effective_user.first_name or "User"
    username = update.effective_user.username or str(uid)

    if uid not in known_users:
        known_users.add(uid)
        _save_user_ids()

    _ensure_user_record(uid)
    context.user_data.pop("force_join_captcha", None)
    context.user_data.pop("force_join_verified", None)

    # --- Referral ---
    if update.message and update.message.text and " " in update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][4:])
                result = await process_referral(referrer_id, uid)
                if result["success"]:
                    try:
                        ref_msg = _bold_blockquote(
                            "🎉 𝗥𝗘𝗙𝗘𝗥𝗥𝗔𝗟 𝗦𝗨𝗖𝗖𝗘𝗦𝗦\n\n"
                            f"👤 @{username} 𝗝𝗢𝗜𝗡𝗘𝗗 𝗨𝗦𝗜𝗡𝗚 𝗬𝗢𝗨𝗥 𝗟𝗜𝗡𝗞\n\n"
                            "⏳ +𝟯 𝗛𝗢𝗨𝗥𝗦 𝗔𝗖𝗖𝗘𝗦𝗦 𝗔𝗗𝗗𝗘𝗗\n"
                            f"📊 𝗔𝗖𝗖𝗘𝗦𝗦 : {result.get('referrer_remaining', 'N/A')}")
                        await context.bot.send_message(
                            chat_id=referrer_id, text=ref_msg, parse_mode="HTML")
                    except Exception as exc:
                        logger.exception(
                            f"[REFERRAL] notification failed: referrer={referrer_id}, err={exc}")
            except (ValueError, TypeError):
                pass

    # --- Access check (force join + captcha + time) ---
    if not await _require_access(update, context):
        return

    stop_sms_monitor(uid)
    _stop_admin_panel_live_task(uid)
    sess = await _ensure_session(uid)
    sess["devices"] = {}
    sess["current_device"] = ""
    sess["mode"] = "online"
    sess["awaiting_fb_add"] = False
    context.user_data.pop("awaiting_url", None)
    context.user_data.pop("awaiting_fb_add", None)

    await _send_main_menu(context, update.effective_chat.id, first_name, uid)


# ============================================================
# GENERATE NUMBER
# ============================================================
async def _safe_edit_callback_message(q, text: str, *, parse_mode=None, reply_markup=None):
    """Edit callback message whether it is a photo (caption) or a text message."""
    msg = q.message
    is_photo = bool(getattr(msg, "photo", None))
    try:
        if is_photo:
            await q.edit_message_caption(
                caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await q.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except Exception as e1:
        err = str(e1).lower()
        if "message is not modified" in err:
            return True
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await q.bot.send_message(
                chat_id=msg.chat_id, text=text,
                parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception as e2:
            logger.error("safe edit failed: %s | fallback: %s", e1, e2)
            return False


async def generate_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _require_access(update, context, edit_target=q.message):
        return
    uid = q.from_user.id
    chat_id = q.message.chat_id
    sess = await _ensure_session(uid)

    if not global_fb_list:
        await _safe_edit_callback_message(
            q,
            "⚠️ *No numbers available right now.*\n\n"
            "Admin ne abhi koi Firebase add nahi kiya.\n"
            "Thodi der baad try karo.",
            parse_mode="Markdown", reply_markup=connect_inline_kb())
        return

    await _safe_edit_callback_message(q, "⚡ Generating from ready devices...")
    devices = dict(global_device_cache.get("devices") or {})
    sess["fb_list"] = list(global_fb_list)

    if not devices:
        await _safe_edit_callback_message(
            q,
            "⚠️ *Abhi ready online number nahi hai.*\n\n"
            "Admin device snapshot background me refresh ho raha hai.",
            parse_mode="Markdown", reply_markup=connect_inline_kb())
        return

    device_id, info = random.choice(list(devices.items()))
    sess["devices"] = {device_id: info}
    sess["current_device"] = device_id
    sess["mode"] = "online"

    tag = info.get("fb_tag", "?")
    real_cid = info.get("real_cid", device_id)
    phone = info.get("phone") or "N/A"
    raw = info.get("raw") or {}
    network = raw.get("network") or raw.get("operator") or "—"
    android = raw.get("android") or raw.get("androidVersion") or raw.get("os") or "—"
    battery = raw.get("battery") or raw.get("batteryLevel") or "—"
    if isinstance(battery, (int, float)):
        battery = f"{int(battery)}%"

    stop_sms_monitor(uid)

    # Delete previous SMS messages
    current_state = sms_monitor_state.get(uid)
    if current_state:
        message_ids = current_state.get("sent_message_ids", [])
        if message_ids:
            try:
                for msg_id in message_ids:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    await asyncio.sleep(0.1)  # Small delay between deletes
            except Exception as e:
                logger.warning(f"could not delete old SMS messages: {e}")

    baseline_fingerprints = set()
    try:
        baseline_pairs = await fetch_last_sms(info.get("fb_url", ""), real_cid, limit=50)
        baseline_fingerprints = {
            _msg_fingerprint(key, message) for key, message in baseline_pairs
        }
        logger.info("[SMS MONITOR] baseline uid=%s device=%s count=%d",
                    uid, device_id, len(baseline_fingerprints))
    except Exception as exc:
        logger.warning("SMS baseline unavailable: %s", exc)

    start_sms_monitor(
        context.bot, uid, chat_id, device_id, unlimited=True,
        baseline_fingerprints=baseline_fingerprints,
        fb_url=info.get("fb_url", ""))

    text_msg = (
        f"🎲 *NUMBER GENERATED*\n\n"
        f"📱 Device: `{real_cid}`\n"
        f"📞 Phone: `{phone}`\n"
        f"🌐 Panel: `{tag}`\n"
        f"📡 Status: 🟢 ONLINE\n"
        f"📶 Network: {network}\n"
        f"🤖 Android: {android}\n"
        f"🔋 Battery: {battery}\n\n"
        f"✅ *OTP Monitor ON*\n"
        f"Naya OTP is chat me aayega.\n"
        f"Dubara GENERATE pe purana monitor band + messages delete.")
    await _safe_edit_callback_message(
        q, text_msg, parse_mode="Markdown",
        reply_markup=device_actions_kb(device_id, "online"))


# ============================================================
# ADMIN COMMAND
# ============================================================
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    _stop_admin_panel_live_task(uid)
    await update.message.reply_text(
        "🛠 *Admin Panel*\n\nSelect an action:",
        parse_mode="Markdown", reply_markup=admin_panel_kb())


# ============================================================
# CAPTCHA INPUT
# ============================================================
async def captcha_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "force_join_captcha" not in context.user_data:
        return
    raw = (update.message.text or "").strip()
    try:
        answer = int(re.sub(r"\D", "", raw))
    except ValueError:
        await update.message.reply_text(
            _bold_blockquote("❌ 𝗪𝗥𝗢𝗡𝗚 𝗔𝗡𝗦𝗪𝗘𝗥\n\n🧮 Sirf number bhejein."),
            parse_mode="HTML")
        return
    if answer != context.user_data.get("force_join_captcha"):
        question, new_answer = _new_math_captcha()
        context.user_data["force_join_captcha"] = new_answer
        await update.message.reply_text(
            _bold_blockquote(f"❌ 𝗪𝗥𝗢𝗡𝗚 𝗔𝗡𝗦𝗪𝗘𝗥\n\n🧮 Try: {question} = ?"),
            parse_mode="HTML")
        return
    context.user_data.pop("force_join_captcha", None)
    context.user_data["force_join_verified"] = True
    verified_access_users.add(update.effective_user.id)

    await update.message.reply_text(
        _bold_blockquote("✅ 𝗖𝗔𝗣𝗧𝗖𝗛𝗔 𝗩𝗘𝗥𝗜𝗙𝗜𝗘𝗗\n\n✅ 𝗔𝗖𝗖𝗘𝗦𝗦 𝗚𝗥𝗔𝗡𝗧𝗘𝗗!"),
        parse_mode="HTML")

    await _send_main_menu(context, update.effective_chat.id,
                          update.effective_user.first_name or "User",
                          update.effective_user.id)


# ============================================================
# ADMIN TEXT INPUT
# ============================================================
async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_fb_list
    uid = update.effective_user.id
    action = context.user_data.get("admin_action")
    if uid not in ADMIN_IDS or not action:
        return
    text = (update.message.text or "").strip()

    # ---------- GIFT SINGLE: expects "<user_id> <hours>" ----------
    if action == "gift_single":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text(
                "❌ Format: `<user_id> <hours>`\n"
                "Example: `123456789 5`",
                parse_mode="Markdown", reply_markup=admin_gift_kb())
            return
        try:
            target_uid = int(parts[0].strip())
            hours = float(parts[1].strip())
        except (ValueError, TypeError):
            await update.message.reply_text(
                "❌ Invalid user ID or hours. Example: `123456789 5`",
                parse_mode="Markdown", reply_markup=admin_gift_kb())
            return
        if hours <= 0 or hours > GIFT_ACCESS_MAX_HOURS:
            await update.message.reply_text(
                f"❌ Hours 1 se {GIFT_ACCESS_MAX_HOURS} ke beech hone chahiye.",
                reply_markup=admin_gift_kb())
            return
        seconds = int(hours * 3600)
        _ensure_user_record(target_uid)
        grant_access(target_uid, seconds)
        context.user_data.pop("admin_action", None)
        remaining = format_remaining_time(target_uid)
        # Notify user
        notified = False
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=_bold_blockquote(
                    "🎁 𝗔𝗖𝗖𝗘𝗦𝗦 𝗚𝗜𝗙𝗧𝗘𝗗\n\n"
                    f"⏳ 𝗚𝗶𝗳𝘁𝗲𝗱 𝗧𝗶𝗺𝗲 : {hours:g} 𝗛𝗼𝘂𝗿𝘀\n"
                    f"📊 𝗡𝗲𝘄 𝗔𝗰𝗰𝗲𝘀𝘀 : {remaining}\n\n"
                    "✅ 𝗬𝗼𝘂 𝗰𝗮𝗻 𝗻𝗼𝘄 𝘂𝘀𝗲 𝘁𝗵𝗲 𝗯𝗼𝘁."),
                parse_mode="HTML")
            notified = True
        except Exception as exc:
            logger.info("gift notify failed for %s: %s", target_uid, exc)
        await update.message.reply_text(
            f"✅ *Access Gifted*\n\n"
            f"👤 User: `{target_uid}`\n"
            f"⏳ Hours: `{hours:g}`\n"
            f"📊 New Access: `{remaining}`\n"
            f"📨 Notified: `{'Yes' if notified else 'No'}`",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    # ---------- GIFT ALL: expects "<hours>" ----------
    if action == "gift_all":
        try:
            hours = float(text.strip())
        except (ValueError, TypeError):
            await update.message.reply_text(
                "❌ Invalid hours. Example: `5`",
                parse_mode="Markdown", reply_markup=admin_gift_kb())
            return
        if hours <= 0 or hours > GIFT_ACCESS_MAX_HOURS:
            await update.message.reply_text(
                f"❌ Hours 1 se {GIFT_ACCESS_MAX_HOURS} ke beech hone chahiye.",
                reply_markup=admin_gift_kb())
            return
        seconds = int(hours * 3600)
        context.user_data.pop("admin_action", None)
        sent = failed = 0
        for target_uid in list(known_users):
            _ensure_user_record(target_uid)
            grant_access(target_uid, seconds)
            remaining = format_remaining_time(target_uid)
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=_bold_blockquote(
                        "🎁 𝗔𝗖𝗖𝗘𝗦𝗦 𝗚𝗜𝗙𝗧𝗘𝗗\n\n"
                        f"⏳ 𝗚𝗶𝗳𝘁𝗲𝗱 𝗧𝗶𝗺𝗲 : {hours:g} 𝗛𝗼𝘂𝗿𝘀\n"
                        f"📊 𝗡𝗲𝘄 𝗔𝗰𝗰𝗲𝘀𝘀 : {remaining}\n\n"
                        "✅ 𝗬𝗼𝘂 𝗰𝗮𝗻 𝗻𝗼𝘄 𝘂𝘀𝗲 𝘁𝗵𝗲 𝗯𝗼𝘁."),
                    parse_mode="HTML")
                sent += 1
            except Exception as exc:
                failed += 1
                logger.info("gift-all notify failed for %s: %s", target_uid, exc)
        await update.message.reply_text(
            f"🎁 *Gift All Complete*\n\n"
            f"⏳ Hours: `{hours:g}`\n"
            f"👥 Total Users: `{len(known_users)}`\n"
            f"✅ Notified: `{sent}`\n"
            f"❌ Failed: `{failed}`",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    # ---------- ADD FIREBASE ----------
    if action == "add_firebase":
        url = normalize_fb_url(text)
        if not url:
            await update.message.reply_text("❌ Invalid Firebase URL.")
            return
        if any(u == url for u, _ in global_fb_list):
            await update.message.reply_text("⚠️ Already added.", reply_markup=admin_panel_kb())
            context.user_data.pop("admin_action", None)
            return
        if len(global_fb_list) >= MAX_FIREBASES:
            await update.message.reply_text(f"❌ Max {MAX_FIREBASES}.",
                                            reply_markup=admin_panel_kb())
            context.user_data.pop("admin_action", None)
            return
        msg = await update.message.reply_text("⏳ Validating Firebase...")
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT))
        try:
            clients, _ = await _try_fetch_clients(session, url)
            ok = bool(clients)
        finally:
            await session.close()
        if not ok:
            await msg.edit_text("❌ FIREBASE DEAD / empty.", reply_markup=admin_back_kb())
            return
        new_tag = f"FB{len(global_fb_list) + 1}"
        global_fb_list.append((url, new_tag))
        _save_global_firebases()
        context.user_data.pop("admin_action", None)
        await refresh_global_device_cache()
        online = offline = 0
        try:
            online, offline = await fetch_counts_from_one(url, new_tag)
        except Exception:
            pass
        await msg.edit_text(
            f"✅ *Firebase Added*\n\n🔗 `{new_tag}`\n"
            f"🟢 Online: `{online}`\n🔴 Offline: `{offline}`\n"
            f"📊 Panels: {len(global_fb_list)}/{MAX_FIREBASES}",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    # ---------- ADD CHANNEL ----------
    if action == "add_channel":
        chat_id_input = text.strip()
        
        # If it's a link
        if chat_id_input.startswith("https://t.me/"):
            if "/+" in chat_id_input or "joinchat" in chat_id_input:
                await update.message.reply_text("❌ Private link se directly check nahi hota. Please bot ko channel me admin banayein aur numeric Chat ID (e.g. -100...) bhejein.")
                return
            else:
                chat_id_input = "@" + chat_id_input.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        
        if chat_id_input.lstrip("-").isdigit():
            chat_id = int(chat_id_input)
        else:
            if not chat_id_input.startswith("@"):
                chat_id_input = "@" + chat_id_input
            if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", chat_id_input):
                await update.message.reply_text(
                    "❌ Valid public channel username ya numeric Chat ID (-100...) bhejein.")
                return
            chat_id = chat_id_input

        if any(str(c.get("id")).lower() == str(chat_id).lower() for c in REQUIRED_CHANNELS):
            await update.message.reply_text("⚠️ Yeh channel already added hai.",
                                            reply_markup=admin_panel_kb())
            context.user_data.pop("admin_action", None)
            return
            
        try:
            chat = await context.bot.get_chat(chat_id)
            title = chat.title or str(chat_id)
            if chat.username:
                url = f"https://t.me/{chat.username}"
            else:
                url = chat.invite_link
                if not url:
                    url = await context.bot.export_chat_invite_link(chat_id)
        except Exception as exc:
            logger.warning("admin channel validation failed: %s", exc)
            await update.message.reply_text(
                "❌ Channel/Group nahi mila ya bot ko wahan admin access nahi hai. Bot ko admin banayein aur try karein.")
            return
            
        REQUIRED_CHANNELS.append({
            "id": chat_id,
            "label": title,
            "url": url,
        })
        _save_required_channels()
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"✅ Force-join channel added: *{title}*",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    # ---------- BROADCAST ----------
    if action == "broadcast":
        context.user_data.pop("admin_action", None)
        sent = failed = 0
        for target_uid in list(known_users):
            try:
                await context.bot.send_message(chat_id=target_uid, text=text)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.info("broadcast failed for %s: %s", target_uid, exc)
        await update.message.reply_text(
            f"📢 Broadcast complete.\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
            reply_markup=admin_panel_kb())


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("admin_action"):
        await admin_text_input(update, context)
    elif "force_join_captcha" in context.user_data:
        await captcha_input(update, context)
    else:
        pass


# ============================================================
# SMS MONITOR
# ============================================================
async def _delete_monitor_messages(bot, uid: int, chat_id: int, message_ids=None):
    state = sms_monitor_state.get(uid, {})
    ids = list(message_ids if message_ids is not None
               else state.get("sent_message_ids", []))
    if not ids:
        return
    for message_id in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.info("could not delete SMS %s: %s", message_id, exc)
    if message_ids is None:
        state["sent_message_ids"] = []


async def _delete_monitor_status_message(bot, uid: int, chat_id: int):
    state = sms_monitor_state.get(uid, {})
    message_id = state.get("monitor_message_id")
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.info("could not delete monitor status %s: %s", message_id, exc)
    state.pop("monitor_message_id", None)


def touch_sms_monitor(uid: int):
    """Reset idle timer whenever user taps a button."""
    state = sms_monitor_state.get(uid)
    if state:
        state["last_activity"] = time.monotonic()


def stop_sms_monitor(uid: int):
    task = sms_monitor_tasks.pop(uid, None)
    state = sms_monitor_state.get(uid)
    if state:
        bot = state.get("bot")
        chat_id = state.get("chat_id")
        if bot and chat_id:
            message_ids = list(state.get("sent_message_ids", []))
            if message_ids:
                asyncio.create_task(_delete_monitor_messages(bot, uid, chat_id, message_ids))
            asyncio.create_task(_delete_monitor_status_message(bot, uid, chat_id))
    if task and not task.done():
        task.cancel()
    sms_monitor_state.pop(uid, None)


def _msg_fingerprint(ts_key: str, m: dict) -> str:
    sender = _extract_msg_sender(m)
    body = _extract_msg_body(m)
    tval = ""
    if isinstance(m, dict):
        for k in ("time", "timestamp", "receivedTime", "sentTime",
                  "dateTime", "createdAt"):
            v = m.get(k)
            if v is not None and str(v).strip():
                tval = str(v).strip()
                break
    return f"{ts_key}|{sender}|{body}|{tval}"


async def _sms_monitor_loop(bot, uid: int, chat_id: int, device_id: str,
                             unlimited: bool = False,
                             baseline_fingerprints=None,
                             fb_url: str = "", task_token=None, state: dict = None):
    started = time.monotonic()
    seen_fingerprints = set(baseline_fingerprints or ())
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FB_REQUEST_TIMEOUT))
    real_cid = device_id
    resolved_fb_url = fb_url
    stop_reason = None
    try:
        tag, real_cid = _parse_prefixed(device_id)
        if not resolved_fb_url:
            resolved_fb_url = (
                (state or {}).get("fb_url")
                or sms_monitor_state.get(uid, {}).get("fb_url")
                or _find_fb_url_by_tag(uid, tag))
        if not resolved_fb_url:
            return
        url_with_query = build_fb_endpoint(
            resolved_fb_url, f"messages/{real_cid}",
            query='orderBy="$key"&limitToLast=50')
        url_plain = build_fb_endpoint(resolved_fb_url, f"messages/{real_cid}")
        while True:
            if not unlimited and (time.monotonic() - started >= SMS_MONITOR_DURATION):
                break
            current_state = sms_monitor_state.get(uid)
            if current_state is None or current_state is not state:
                break
            if unlimited and uid not in ADMIN_IDS:
                last_activity = current_state.get("last_activity", started)
                if (time.monotonic() - last_activity) >= SMS_MONITOR_IDLE_TIMEOUT:
                    stop_reason = "idle"
                    break
            if uid not in ADMIN_IDS and not has_access(uid):
                stop_reason = "revoked"
                break
            data, status, err = await fb_get_json(session, url_with_query,
                                                   retries=0, timeout=6)
            if status != "SUCCESS" or not isinstance(data, (dict, list)) or not data:
                data, status, err = await fb_get_json(session, url_plain,
                                                       retries=0, timeout=6)
            if status != "SUCCESS" or not isinstance(data, (dict, list)) or not data:
                await asyncio.sleep(SMS_MONITOR_INTERVAL)
                continue
            pairs = _newest_with_keys(data, limit=50)
            current_fps = [(k, m, _msg_fingerprint(k, m)) for k, m in pairs]
            new_msgs = [(k, m, fp) for k, m, fp in current_fps
                        if fp not in seen_fingerprints]
            new_msgs.sort(key=lambda x: str(x[0]))
            for ts_key, m, fp in new_msgs:
                sender = _extract_msg_sender(m)
                body = _extract_msg_body(m)
                if not body:
                    body = "(no message body)"
                when = _extract_msg_time(m, ts_key).replace(" | ", " • ")
                otp = _extract_otp(body)
                alert_lines = [
                    "💬 ʟɪᴠᴇ ꜱᴍꜱ ʀᴇᴄᴇɪᴠᴇᴅ!",
                    "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️",
                    "",
                    f"📱 ꜰrom: {sender}",
                    f"⏱️ ᴛime: {when}",
                    "",
                    f"{body}",
                ]
                if otp:
                    alert_lines.extend(["", f"🔑 ᴏᴛᴘ ᴅᴇᴛᴇᴄᴛᴇᴅ: `{otp}`"])
                alert_text = "\n".join(alert_lines)
                try:
                    sent_message = await bot.send_message(
                        chat_id=chat_id,
                        text=_bold_blockquote(alert_text),
                        parse_mode="HTML")
                    seen_fingerprints.add(fp)
                    state = sms_monitor_state.get(uid)
                    if state is not None and state is current_state:
                        state.setdefault("sent_message_ids", []).append(
                            sent_message.message_id)
                except Exception as e:
                    logger.error("[SMS MONITOR] notify error uid=%s err=%r",
                                 uid, e, exc_info=True)
            if len(seen_fingerprints) > 500:
                seen_fingerprints = set(fp for _, _, fp in current_fps)
            await asyncio.sleep(SMS_MONITOR_INTERVAL)

        if not unlimited:
            await _delete_monitor_messages(bot, uid, chat_id)
            await _delete_monitor_status_message(bot, uid, chat_id)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=("⏱️ *SMS Monitor Auto-Stopped*\n\n"
                          "✅ Time poora ho gaya.\n"
                          "▶️ Dobara GENERATE NUMBER dabao."),
                    parse_mode="Markdown", reply_markup=connect_inline_kb())
            except Exception as e:
                logger.error(f"auto-stop notify: {e}")
        elif stop_reason == "idle":
            await _delete_monitor_messages(bot, uid, chat_id)
            await _delete_monitor_status_message(bot, uid, chat_id)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=_bold_blockquote(
                        "⏱️ 𝗠𝗢𝗡𝗜𝗧𝗢𝗥 𝗔𝗨𝗧𝗢-𝗦𝗧𝗢𝗣𝗣𝗘𝗗\n\n"
                        "⚠️ 𝟭𝟬 𝗺𝗶𝗻𝘂𝘁𝗲𝘀 𝘀𝗲 𝗸𝗼𝗶 𝗯𝘂𝘁𝘁𝗼𝗻 𝗻𝗮𝗵𝗶 𝗱𝗮𝗯𝗮𝘆𝗮\n\n"
                        "🔄 𝗔𝗰𝗰𝗲𝘀𝘀 𝗮𝗴𝗮𝗶𝗻 𝗸𝗲 𝗹𝗶𝘆𝗲 /start 𝗱𝗮𝗯𝗮𝗼 𝗮𝘂𝗿 𝗻𝗮𝘆𝗮 𝗺𝗼𝗻𝗶𝘁𝗼𝗿 𝘀𝘁𝗮𝗿𝘁 𝗸𝗮𝗿𝗼."),
                    parse_mode="HTML",
                    reply_markup=connect_inline_kb())
            except Exception as e:
                logger.error(f"idle-stop notify: {e}")
        elif stop_reason == "revoked":
            await _delete_monitor_messages(bot, uid, chat_id)
            await _delete_monitor_status_message(bot, uid, chat_id)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=_bold_blockquote(
                        "⛔ 𝗠𝗢𝗡𝗜𝗧𝗢𝗥 𝗦𝗧𝗢𝗣𝗣𝗘𝗗\n\n"
                        "⚠️ 𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗿𝗲𝘃𝗼𝗸𝗲𝗱.\n\n"
                        "🔗 𝗥𝗲𝗷𝗼𝗶𝗻 𝗮𝗹𝗹 𝗰𝗵𝗮𝗻𝗻𝗲𝗹𝘀 𝗮𝗻𝗱 𝘀𝗲𝗻𝗱 /start"),
                    parse_mode="HTML",
                    reply_markup=connect_inline_kb())
            except Exception as e:
                logger.error(f"revoke-stop notify: {e}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("[SMS MONITOR] crashed uid=%s err=%r", uid, e, exc_info=True)
    finally:
        try:
            await session.close()
        except Exception:
            pass
        current_state = sms_monitor_state.get(uid)
        if current_state is state:
            sms_monitor_state.pop(uid, None)
            sms_monitor_tasks.pop(uid, None)


def start_sms_monitor(bot, uid: int, chat_id: int, device_id: str,
                       unlimited: bool = False, baseline_fingerprints=None,
                       fb_url: str = ""):
    stop_sms_monitor(uid)
    task_token = object()
    now = time.monotonic()
    state = {
        "device_id": device_id, "chat_id": chat_id,
        "sent_message_ids": [], "bot": bot, "unlimited": unlimited,
        "baseline_fingerprints": set(baseline_fingerprints or ()),
        "fb_url": fb_url, "task_token": task_token,
        "last_activity": now,
    }
    sms_monitor_state[uid] = state
    task = asyncio.create_task(_sms_monitor_loop(
        bot, uid, chat_id, device_id, unlimited=unlimited,
        baseline_fingerprints=baseline_fingerprints,
        fb_url=fb_url, task_token=task_token, state=state))
    sms_monitor_tasks[uid] = task
    logger.info("[SMS MONITOR] started uid=%s device=%s fb_url=%s baseline_count=%d",
                uid, device_id, fb_url, len(baseline_fingerprints or ()))


def _parse_prefixed(device_id: str):
    if "|" in device_id:
        parts = device_id.split("|", 1)
        return parts[0], parts[1]
    return "", device_id


def _find_fb_url_by_tag(uid: int, tag: str) -> Optional[str]:
    for url, t in global_fb_list:
        if t == tag:
            return url
    sess = user_sessions.get(uid)
    if sess:
        for url, t in sess.get("fb_list", []):
            if t == tag:
                return url
    return None


# ============================================================
# ADMIN CALLBACKS
# ============================================================
def _admin_only(uid: int) -> bool:
    return uid in ADMIN_IDS


async def _admin_show_firebases(q):
    text = _build_admin_fb_text()
    await q.edit_message_text(text, parse_mode="Markdown",
                              reply_markup=admin_firebases_kb())
    await _start_admin_panel_live_task(
        q.bot, q.from_user.id, q.message.chat_id, q.message.message_id)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_fb_list, maintenance_mode, captcha_enabled, _last_refresh_time
    q = update.callback_query
    uid = q.from_user.id
    if not _admin_only(uid):
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer()
    data = q.data

    if not (data == "admin_manage_fb"
            or data.startswith("admin_fb_refresh:")
            or data.startswith("admin_fb_info:")
            or data.startswith("admin_fb_delete:")):
        _stop_admin_panel_live_task(uid)

    # ---------- GIFT ACCESS MENU ----------
    if data == "admin_gift_access":
        await q.edit_message_text(
            "🎁 *Gift Access*\n\n"
            "Kaise gift karna hai?\n\n"
            "👤 *Single User* — ek user ko hours ke hisaab se access do\n"
            "👥 *All Users* — sabhi users ko same hours do\n\n"
            "Neeche se option chuno:",
            parse_mode="Markdown", reply_markup=admin_gift_kb())
        return

    if data == "admin_gift_single":
        context.user_data["admin_action"] = "gift_single"
        await q.edit_message_text(
            "👤 *Gift Single User*\n\n"
            "Format bhejein:\n"
            "`<user_id> <hours>`\n\n"
            "Example:\n"
            "`123456789 5`\n\n"
            "Iska matlab: user 123456789 ko 5 ghante ka access.\n\n"
            "📌 *Hours float bhi ho sakte hain* (e.g. `0.5` = 30 min)",
            parse_mode="Markdown", reply_markup=admin_gift_kb())
        return

    if data == "admin_gift_all":
        context.user_data["admin_action"] = "gift_all"
        await q.edit_message_text(
            "👥 *Gift All Users*\n\n"
            f"Sirf hours likh kar bhejein.\n"
            f"Total users: `{len(known_users)}`\n\n"
            "Example:\n"
            "`5`\n\n"
            "Iska matlab: sabhi users ko 5 ghante ka access.\n\n"
            "📌 *Hours float bhi ho sakte hain* (e.g. `0.5` = 30 min)",
            parse_mode="Markdown", reply_markup=admin_gift_kb())
        return

    if data == "admin_toggle_maintenance":
        maintenance_mode = not maintenance_mode
        _save_maintenance_mode()
        notice = (_bold_blockquote("🛠️ 𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘 𝗠𝗢𝗗𝗘 𝗢𝗡 ⚙️")
                  if maintenance_mode
                  else _bold_blockquote("✅ 𝗕𝗢𝗧 𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘 𝗠𝗢𝗗𝗘 𝗢𝗙𝗙"))
        sent = failed = 0
        for target_uid in list(known_users):
            try:
                await context.bot.send_message(chat_id=target_uid,
                                                text=notice, parse_mode="HTML")
                sent += 1
            except Exception as exc:
                failed += 1
                logger.info("maintenance notice failed for %s: %s", target_uid, exc)
        await q.edit_message_text(
            f"{notice}\n\n📨 Sent: {sent}\n❌ Failed: {failed}",
            parse_mode="HTML", reply_markup=admin_panel_kb())
        return

    if data == "admin_toggle_captcha":
        captcha_enabled = not captcha_enabled
        _save_captcha_enabled()
        await q.edit_message_text(
            f"🔐 Captcha: {'ON' if captcha_enabled else 'OFF'}",
            reply_markup=admin_panel_kb())
        return

    if data == "admin_back":
        context.user_data.pop("admin_action", None)
        await q.edit_message_text(
            "🛠 *Admin Panel*\n\nSelect an action:",
            parse_mode="Markdown", reply_markup=admin_panel_kb())
        return

    if data == "admin_stats":
        await q.edit_message_text(
            "📊 *Bot Statistics*\n\n"
            f"👥 Total users: `{len(known_users)}`\n"
            f"🔗 Firebases: `{len(global_fb_list)}`\n"
            f"🔗 Force-join channels: `{len(REQUIRED_CHANNELS)}`\n"
            f"💾 Active sessions: `{len(user_sessions)}`\n"
            f"📨 Active SMS monitors: `{len(sms_monitor_tasks)}`\n"
            f"🔐 Captcha: `{'ON' if captcha_enabled else 'OFF'}`",
            parse_mode="Markdown", reply_markup=admin_back_kb())
        return

    if data == "admin_add_firebase":
        if len(global_fb_list) >= MAX_FIREBASES:
            await q.answer(f"Max {MAX_FIREBASES} Firebase allowed.", show_alert=True)
            return
        context.user_data["admin_action"] = "add_firebase"
        await q.edit_message_text(
            "➕ *Add Firebase*\n\n"
            "Firebase Realtime Database URL bhejein.\n"
            "Example: `https://xxx-default-rtdb.firebaseio.com`",
            parse_mode="Markdown", reply_markup=admin_back_kb())
        return

    if data == "admin_manage_fb":
        await _admin_show_firebases(q)
        return

    if data.startswith("admin_fb_refresh:"):
        try:
            idx = int(data.split(":", 1)[1])
            if idx < 0 or idx >= len(global_fb_list):
                await q.answer("Invalid Firebase.", show_alert=True)
                return
            url, tag = global_fb_list[idx]
            await q.edit_message_text(f"⏳ Refreshing `{tag}`...")
            await refresh_global_device_cache()
            _last_refresh_time = time.monotonic()
            text = _build_admin_fb_text()
            await q.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=admin_firebases_kb())
            await _start_admin_panel_live_task(
                context.bot, uid, q.message.chat_id, q.message.message_id)
        except Exception as e:
            logger.error("admin_fb_refresh: %s", e)
            await q.edit_message_text("❌ Refresh failed.",
                                      reply_markup=admin_firebases_kb())
        return

    if data.startswith("admin_fb_delete:"):
        try:
            idx = int(data.split(":", 1)[1])
            if idx < 0 or idx >= len(global_fb_list):
                await q.answer("Invalid Firebase.", show_alert=True)
                return
            removed = global_fb_list.pop(idx)
            _retag_global_firebases()
            _save_global_firebases()
            await q.edit_message_text(
                f"🗑 *Deleted*\n\nRemoved: `{removed[1]}`\n"
                f"Remaining: {len(global_fb_list)}/{MAX_FIREBASES}",
                parse_mode="Markdown", reply_markup=admin_firebases_kb())
        except Exception as e:
            logger.error("admin_fb_delete: %s", e)
            await q.answer("Delete failed.", show_alert=True)
        return

    if data.startswith("admin_fb_info:"):
        try:
            idx = int(data.split(":", 1)[1])
            if idx < 0 or idx >= len(global_fb_list):
                await q.answer("Invalid Firebase.", show_alert=True)
                return
            url, tag = global_fb_list[idx]
            per_fb = global_device_cache.get("per_fb", {}) or {}
            counts = per_fb.get(tag) or {}
            online = int(counts.get("online", 0))
            offline = int(counts.get("offline", 0))
            await q.edit_message_text(
                f"📋 *{tag}*\n\n"
                f"🟢 Online: `{online}`\n🔴 Offline: `{offline}`\n"
                f"📊 Total: `{online + offline}`\n"
                f"🕒 `{global_device_cache.get('updated_at', '')}`",
                parse_mode="Markdown", reply_markup=admin_firebases_kb())
        except Exception as e:
            logger.error("admin_fb_info: %s", e)
            await q.answer("Load failed.", show_alert=True)
        return

    if data == "admin_channels":
        if REQUIRED_CHANNELS:
            lines = ["📋 *Force-Join Channels*\n"]
            for i, channel in enumerate(REQUIRED_CHANNELS, 1):
                lines.append(f"{i}. `{channel.get('label', channel.get('id'))}`")
            text = "\n".join(lines)
        else:
            text = "📋 *Force-Join Channels*\n\nNo channels configured."
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=admin_channels_kb())
        return

    if data == "admin_add_channel":
        context.user_data["admin_action"] = "add_channel"
        await q.edit_message_text(
            "➕ *Add Force-Join Channel*\n\n"
            "Channel username bhejein, example: `@mychannel`\n"
            "Bot ko us channel ka administrator hona chahiye.",
            parse_mode="Markdown", reply_markup=admin_back_kb())
        return

    if data == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await q.edit_message_text(
            "📢 *Broadcast*\n\n"
            "Broadcast message bhejein. Sabhi users ko send hoga.",
            parse_mode="Markdown", reply_markup=admin_back_kb())
        return

    if data.startswith("admin_remove_channel:"):
        try:
            index = int(data.split(":", 1)[1])
            removed = REQUIRED_CHANNELS.pop(index)
            _save_required_channels()
            await q.edit_message_text(
                f"✅ Removed: `{removed.get('label', removed.get('id'))}`",
                parse_mode="Markdown", reply_markup=admin_channels_kb())
        except (ValueError, IndexError):
            await q.answer("Invalid channel.", show_alert=True)
        return


# ============================================================
# SMS VIEW
# ============================================================
async def _show_sms_safe(q, info: dict, device_id: str, updated_at: Optional[str] = None):
    """Fetch and display last N SMS for a device."""
    fb_url = info.get("fb_url", "")
    real_cid = info.get("real_cid", device_id)
    tag = info.get("fb_tag", "?")

    if not fb_url:
        parsed_tag, parsed_cid = _parse_prefixed(device_id)
        fb_url = _find_fb_url_by_tag(q.from_user.id, parsed_tag)
        if not fb_url:
            await _safe_edit_callback_message(
                q,
                "❌ *Device info missing.*\n\nPlease tap REFRESH.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 REFRESH", callback_data="scan_active")],
                    [InlineKeyboardButton("🎲 GENERATE AGAIN", callback_data="generate_number")],
                ]))
            return
        real_cid = parsed_cid
        tag = parsed_tag

    try:
        await _safe_edit_callback_message(q, "⏳ Fetching SMS...")
    except Exception:
        pass

    pairs = await fetch_last_sms(fb_url, real_cid, limit=FB_MESSAGES_LIMIT)

    if not pairs:
        await _safe_edit_callback_message(
            q,
            f"📭 *No SMS records found.*\n\n"
            f"🌐 Firebase: `{tag}`\n"
            f"📱 Device: `{real_cid}`",
            parse_mode="Markdown",
            reply_markup=sms_view_kb(device_id))
        return

    lines = [f"📩 LAST {len(pairs)} SMS", "",
             "━━━━━━━━━━━━━━━━━━━━━━━",
             f"🌐 Firebase: {tag}",
             f"📱 Device: {real_cid}",
             "━━━━━━━━━━━━━━━━━━━━━━━"]
    circled_numbers = ("①", "②", "③", "④", "⑤")
    for i, (ts_key, m) in enumerate(pairs, 1):
        sender = _extract_msg_sender(m)
        body = _extract_msg_body(m)
        when = _extract_msg_time(m, ts_key)
        otp = _extract_otp(body)
        number = _extract_number(sender)
        if not body:
            body = "(no body)"
        number_label = circled_numbers[i - 1] if i <= len(circled_numbers) else f"{i}."
        
        # Add separator between SMS
        if i > 1:
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
        
        lines.extend(["",
                      f"{number_label} ᴅᴇᴠɪᴄᴇ ɴᴀᴍᴇ: {sender}",
                      f"⏱️ ᴛɪᴍᴇ: {when}",
                      "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️",
                      f"{_otp_short_message(sender, body)}"])
        if otp:
            lines.append(f"🔑 ᴏᴛᴘ: `{otp}`")
    if updated_at:
        lines.extend(["",
                      "━━━━━━━━━━━━━━━━━━━━━━━",
                      "𝗨𝗽𝗱𝗮𝘁𝗲𝗱",
                      "━━━━━━━━━━━━━━━━━━━━━━━"])
    text = "\n".join(lines)
    
    # Send all SMS in single message (no separate chats)
    if len(text) > 3800:
        # If too long, split but keep in one message with ... continuation
        text = text[:3800] + "\n\n... (truncated - too many SMS)"
    
    await _safe_edit_callback_message(
        q, text, parse_mode="Markdown",
        reply_markup=sms_view_kb(device_id))


# ============================================================
# MENU CALLBACK
# ============================================================
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "force_verify":
        await force_verify_callback(update, context)
        return

    if not await _require_access(update, context, edit_target=q.message):
        uid_check = q.from_user.id
        if uid_check in sms_monitor_state and uid_check not in ADMIN_IDS:
            stop_sms_monitor(uid_check)
        return

    uid = q.from_user.id
    touch_sms_monitor(uid)

    data = q.data
    sess = user_sessions.get(uid)

    if not sess:
        await q.edit_message_text(
            "⚠️ Session expired.\n\nSend /start again.",
            reply_markup=connect_inline_kb())
        return

    if data == "manage_firebase":
        context.user_data["awaiting_url"] = False
        sess["awaiting_fb_add"] = False
        await q.edit_message_text(
            f"🔗 *Manage Firebase*\n\n"
            f"Connected: {len(sess.get('fb_list', []))}/{MAX_FIREBASES}\n"
            f"Choose an action:",
            parse_mode="Markdown", reply_markup=manage_fb_kb(uid))
        return

    if data == "menu_back":
        stop_sms_monitor(uid)
        context.user_data["awaiting_url"] = False
        sess["awaiting_fb_add"] = False
        if not sess.get("fb_list"):
            await q.edit_message_text("Hii 👋\n\nWelcome to Firebase Connector",
                                      reply_markup=connect_inline_kb())
            return
        await q.edit_message_text(
            f"🔗 *Connected Firebase*\n\n"
            f"📊 Total: {len(sess['fb_list'])}/{MAX_FIREBASES}\n\n"
            "Tap a Firebase to fetch online devices.",
            parse_mode="Markdown", reply_markup=manage_fb_kb(uid))
        return

    if data == "scan_active":
        stop_sms_monitor(uid)
        await q.edit_message_text("⏳ Scanning online devices...")
        await scan_and_show(update, context, edit_target=q.message)
        return

    if data == "device_list":
        # Don't stop monitor - show current device info
        uid = q.from_user.id
        touch_sms_monitor(uid)  # Keep monitor active
        
        current_device = sess.get("current_device")
        if current_device:
            # Show current device info
            await _show_device_view(q, sess, current_device)
        else:
            # No current device, show device list
            if not sess.get("devices"):
                await q.answer("No cached scan. Refreshing...", show_alert=False)
                await scan_and_show(update, context, edit_target=q.message)
                return
            if len(sess.get("devices", {})) <= 1:
                await q.edit_message_text("⏳ Scanning online devices...")
                await scan_and_show(update, context, edit_target=q.message)
                return
            await _show_cached_device_list(q, sess)
        return

    if data.startswith("scan_fb:"):
        stop_sms_monitor(uid)
        try:
            idx = int(data.split(":", 1)[1])
            if idx < 0 or idx >= len(sess.get("fb_list", [])):
                await q.answer("Invalid Firebase.", show_alert=True)
                return
            await q.edit_message_text("⏳ Scanning this Firebase...")
            await scan_and_show(update, context, edit_target=q.message, fb_idx=idx)
        except Exception as e:
            logger.error(f"scan firebase: {e}")
            await q.edit_message_text("❌ Scan failed. Please try again.")
        return

    if data.startswith("delete_fb:"):
        try:
            idx = int(data.split(":", 1)[1])
            fb_list = sess.get("fb_list", [])
            if idx < 0 or idx >= len(fb_list):
                await q.answer("Invalid Firebase.", show_alert=True)
                return
            removed = fb_list.pop(idx)
            fb_list = [(url, f"FB{i+1}") for i, (url, _) in enumerate(fb_list)]
            sess["fb_list"] = fb_list
            sess["active_fb_idx"] = min(sess.get("active_fb_idx", 0),
                                          max(0, len(fb_list) - 1))
            sess["devices"] = {}
            stop_sms_monitor(uid)
            await q.edit_message_text(
                f"🗑️ *Firebase Deleted*\n\n"
                f"Removed: `{removed[1]}`\n"
                f"Remaining: {len(fb_list)}/{MAX_FIREBASES}",
                parse_mode="Markdown",
                reply_markup=manage_fb_kb(uid) if fb_list else connect_inline_kb())
        except Exception as e:
            logger.error(f"delete firebase: {e}")
            await q.edit_message_text("❌ Delete failed.")
        return

    if data.startswith("fbnoop:"):
        try:
            idx = int(data.split(":", 1)[1])
            fb_list = sess.get("fb_list", [])
            if 0 <= idx < len(fb_list):
                sess["active_fb_idx"] = idx
                await q.answer(f"Opening {fb_list[idx][1]}...")
                await q.edit_message_text(
                    f"⏳ Scanning {fb_list[idx][1]} for online devices...",
                    parse_mode="Markdown")
                await scan_and_show(update, context, edit_target=q.message, fb_idx=idx)
            else:
                await q.answer("Invalid Firebase.", show_alert=True)
        except Exception as e:
            logger.error(f"open firebase: {e}")
            await q.answer("Firebase scan failed.", show_alert=True)
        return

    if data == "noop":
        await q.answer(f"Page {sess.get('device_page', 0)+1}")
        return

    if data.startswith("devpage:"):
        try:
            page = int(data.split(":", 1)[1])
            devices = sess.get("devices", {})
            items = list(devices.items())[:80]
            total_pages = max(1, (len(items) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
            if page < 0 or page >= total_pages:
                await q.answer("Invalid page.", show_alert=True)
                return
            sess["device_page"] = page
            await _show_cached_device_list(q, sess)
        except Exception as e:
            logger.error(f"device page: {e}")
            await q.answer("Page change failed.", show_alert=True)
        return

    if data.startswith("dev:"):
        device_id = data.split(":", 1)[1]
        await _show_device_view(q, sess, device_id)
        return

    if data.startswith("sms_refresh:") or data.startswith("sms:"):
        device_id = data.split(":", 1)[1]
        info = sess.get("devices", {}).get(device_id, {})
        if not info:
            parsed_tag, parsed_cid = _parse_prefixed(device_id)
            fb_url = _find_fb_url_by_tag(uid, parsed_tag)
            if fb_url:
                info = {
                    "fb_url": fb_url,
                    "fb_tag": parsed_tag,
                    "real_cid": parsed_cid,
                    "phone": "",
                    "online": True,
                    "raw": {},
                }
                sess.setdefault("devices", {})[device_id] = info
            else:
                await q.answer("Device not found. Please REFRESH.", show_alert=True)
                return
        if data.startswith("sms_refresh:"):
            sess.setdefault("sms_last_updated", {})[device_id] = datetime.now().strftime("%I:%M %p")
        updated_at = sess.get("sms_last_updated", {}).get(device_id)
        await _show_sms_safe(q, info, device_id, updated_at=updated_at)
        return

    if data.startswith("smsmon_stop:"):
        stop_sms_monitor(uid)
        device_id = data.split(':', 1)[1]
        await _show_device_view(q, sess, device_id)
        return

    if data.startswith("smsmon:"):
        device_id = data.split(":", 1)[1]
        info = sess.get("devices", {}).get(device_id, {})
        if not info:
            parsed_tag, parsed_cid = _parse_prefixed(device_id)
            fb_url = _find_fb_url_by_tag(uid, parsed_tag)
            if fb_url:
                info = {
                    "fb_url": fb_url,
                    "fb_tag": parsed_tag,
                    "real_cid": parsed_cid,
                    "phone": "",
                    "online": True,
                    "raw": {},
                }
                sess.setdefault("devices", {})[device_id] = info
            else:
                await q.answer("Device not found.", show_alert=True)
                return
        phone = info.get("phone") or "N/A"
        stop_sms_monitor(uid)
        baseline_fingerprints = set()
        try:
            baseline_pairs = await fetch_last_sms(
                info.get("fb_url", ""), info.get("real_cid", device_id), limit=50)
            baseline_fingerprints = {
                _msg_fingerprint(key, message) for key, message in baseline_pairs}
        except Exception as exc:
            logger.warning("manual SMS baseline unavailable: %s", exc)
        start_sms_monitor(
            context.bot, uid, q.message.chat_id, device_id,
            baseline_fingerprints=baseline_fingerprints,
            fb_url=info.get("fb_url", ""))
        await q.edit_message_text(
            "📨 *SMS Monitor Started*\n\n"
            f"📱 *Device:* `{info.get('real_cid', device_id)}`\n"
            f"📞 *Number:* `{phone}`\n\n"
            "⏱️ *Duration:* 5 minutes\n"
            "⚡ *Speed:* 1 sec polling\n\n"
            "🔔 Har naya SMS turant yahan forward hoga.",
            parse_mode="Markdown",
            reply_markup=sms_monitor_kb(device_id))
        state = sms_monitor_state.get(uid)
        if state is not None:
            state["monitor_message_id"] = q.message.message_id
        return


# ============================================================
# SCAN HELPER
# ============================================================
async def scan_and_show(update, context, edit_target=None, fb_idx=None):
    uid = update.effective_user.id
    sess = user_sessions.get(uid)
    if not sess or not sess.get("fb_list"):
        if edit_target:
            await edit_target.edit_text("⚠️ No Firebase connected. Send /start.")
        return
    if fb_idx is not None:
        sess["active_fb_idx"] = fb_idx
    prefetched = sess.pop("prefetched", {})
    devices_task = fetch_devices_all_firebases(
        sess["fb_list"], only_online=True, prefetched=prefetched)
    counts_task = fetch_counts_all_firebases(
        sess["fb_list"], prefetched=prefetched)
    devices, (online_count, offline_count, per_fb) = await asyncio.gather(
        devices_task, counts_task)
    online_count = len(devices)
    total_count = online_count + offline_count
    sess["devices"] = devices
    sess["mode"] = "online"
    sess["device_page"] = 0
    sess["online_count"] = online_count
    sess["offline_count"] = offline_count
    sess["total_count"] = total_count
    text = (f"📞 *DEVICES INFO*\n\n"
            f"🟢 ONLINE : {online_count}\n"
            f"🔴 OFFLINE : {offline_count}\n"
            f"📊 TOTAL : {total_count}\n"
            f"🔗 Firebase: {len(sess['fb_list'])}\n\n"
            "Tap a device below.")
    markup = (device_list_kb(devices, mode="online", page=0)
              if devices else firebase_connected_kb(uid))
    if edit_target:
        await edit_target.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=markup)


# ============================================================
# MAINTENANCE LOOP
# ============================================================
async def _maintenance_loop(bot):
    loop_count = 0
    while True:
        try:
            await asyncio.sleep(ACCESS_CHECK_INTERVAL)
            loop_count += 1
            if loop_count % max(1, CLEANUP_INTERVAL // ACCESS_CHECK_INTERVAL) == 0:
                gc.collect()
            for uid, task in list(sms_monitor_tasks.items()):
                if task.done():
                    sms_monitor_tasks.pop(uid, None)
            for uid, task in list(admin_panel_live_tasks.items()):
                if task.done():
                    admin_panel_live_tasks.pop(uid, None)

            now = time.time()
            for uid in list(sms_monitor_state.keys()):
                if uid in ADMIN_IDS:
                    continue
                if not has_access(uid):
                    logger.info("[MONITOR] auto-stop uid=%s reason=access_expired", uid)
                    stop_sms_monitor(uid)
                    try:
                        await bot.send_message(
                            chat_id=uid,
                            text=_bold_blockquote(
                                "⛔ 𝗠𝗢𝗡𝗜𝗧𝗢𝗥 𝗦𝗧𝗢𝗣𝗣𝗘𝗗\n\n"
                                "⚠️ 𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗲𝘅𝗽𝗶𝗿𝗲𝗱.\n\n"
                                "🔗 𝗥𝗲𝗳𝗲𝗿 𝗳𝗿𝗶𝗲𝗻𝗱𝘀 𝘁𝗼 𝗴𝗲𝘁 𝗺𝗼𝗿𝗲 𝗮𝗰𝗰𝗲𝘀𝘀.\n"
                                "📌 𝗦𝗲𝗻𝗱 /start 𝘁𝗼 𝗿𝗲𝘀𝘁𝗮𝗿𝘁."),
                            parse_mode="HTML",
                            reply_markup=connect_inline_kb())
                    except Exception as exc:
                        logger.info("could not notify revoked monitor %s: %s", uid, exc)

            if REQUIRED_CHANNELS:
                for uid in list(known_users):
                    joined, missing = await _check_required_channels(bot, uid)
                    previous = user_access_state.get(uid)
                    user_access_state[uid] = joined
                    if previous is True and not joined:
                        verified_access_users.discard(uid)
                        if uid in sms_monitor_state and uid not in ADMIN_IDS:
                            logger.info("[MONITOR] auto-stop uid=%s reason=channel_leave", uid)
                            stop_sms_monitor(uid)
                        referrer_id = await check_referred_user_left(uid)
                        if referrer_id:
                            try:
                                await bot.send_message(
                                    chat_id=referrer_id,
                                    text=_bold_blockquote(
                                        "⚠️ 𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗩𝗢𝗞𝗘𝗗\n\n"
                                        "👤 𝗔 𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝘂𝘀𝗲𝗿 𝗹𝗲𝗳𝘁 𝘁𝗵𝗲 𝗰𝗵𝗮𝗻𝗻𝗲𝗹\n\n"
                                        "📌 𝗥𝗲𝗮𝘀𝗼𝗻 : Channel Leave\n\n"
                                        "🎁 𝗥𝗘𝗙𝗘𝗥 𝗔𝗚𝗔𝗜𝗡 𝗧𝗢 𝗚𝗘𝗧 𝗔𝗖𝗖𝗘𝗦𝗦"),
                                    parse_mode="HTML")
                            except Exception as exc:
                                logger.info(
                                    "could not notify referrer %s: %s", referrer_id, exc)
                            if referrer_id in sms_monitor_state and referrer_id not in ADMIN_IDS:
                                logger.info(
                                    "[MONITOR] auto-stop referrer uid=%s reason=referral_revoked",
                                    referrer_id)
                                stop_sms_monitor(referrer_id)
                        try:
                            await bot.send_message(
                                chat_id=uid,
                                text=_bold_blockquote(
                                    "⚠️ 𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗦𝗧𝗥𝗜𝗖𝗧𝗘𝗗\n\n"
                                    "📌 𝗥𝗲𝗮𝘀𝗼𝗻 : Channel Leave\n\n"
                                    "🔗 𝗥𝗲𝗷𝗼𝗶𝗻 𝗮𝗹𝗹 𝗰𝗵𝗮𝗻𝗻𝗲𝗹𝘀 𝘁𝗼 𝗰𝗼𝗻𝘁𝗶𝗻𝘂𝗲."),
                                parse_mode="HTML",
                                reply_markup=_force_join_kb())
                            logger.info("access restricted uid=%s (channel leave)", uid)
                        except Exception as exc:
                            logger.info("could not notify restricted %s: %s", uid, exc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"maintenance: {e}")


# ============================================================
# SHUTDOWN
# ============================================================
async def _post_stop(app):
    for task in list(sms_monitor_tasks.values()):
        if not task.done():
            task.cancel()
    if sms_monitor_tasks:
        await asyncio.gather(*sms_monitor_tasks.values(), return_exceptions=True)
    sms_monitor_tasks.clear()
    sms_monitor_state.clear()
    for task in list(admin_panel_live_tasks.values()):
        if not task.done():
            task.cancel()
    if admin_panel_live_tasks:
        await asyncio.gather(*admin_panel_live_tasks.values(), return_exceptions=True)
    admin_panel_live_tasks.clear()
    logger.info("Monitor loops cancelled.")


async def _telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    logger.error(
        "telegram handler error update=%s: %s",
        type(update).__name__, error,
        exc_info=(type(error), error, error.__traceback__) if error else None)


# ============================================================
# MAIN
# ============================================================
def main():
    global bot_instance
    print("=" * 60)
    print("  Firebase Connector — OTP Bot FINAL")
    print(f"  Max Firebases: {MAX_FIREBASES}")
    print(f"  Global FBs: {len(global_fb_list)}")
    print(f"  Force-join: {len(REQUIRED_CHANNELS)}")
    print(f"  Captcha: {'ON' if captcha_enabled else 'OFF'}")
    print(f"  Referral: 1 refer = {REFERRAL_HOURS} hours")
    print(f"  SMS Monitor idle timeout: {SMS_MONITOR_IDLE_TIMEOUT // 60} minutes")
    print("=" * 60)

    app = Application.builder().token(BOT_TOKEN).build()
    bot_instance = app.bot

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern=r"^admin_(back|stats|channels|add_channel|add_firebase|manage_fb|broadcast|toggle_maintenance|toggle_captcha|remove_channel:\d+|fb_refresh:\d+|fb_delete:\d+|fb_info:\d+|gift_access|gift_single|gift_all)$"))
    app.add_handler(CallbackQueryHandler(generate_number_callback,
                                          pattern="^generate_number$"))
    app.add_handler(CallbackQueryHandler(
        menu_callback,
        pattern=r"^(device_list|menu_back|dev:.+|sms_refresh:.+|sms:.+|smsmon:.+|smsmon_stop:.+|force_verify|scan_active|scan_fb:\d+|delete_fb:\d+|fbnoop:\d+|noop|devpage:\d+|manage_firebase)$"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        text_router))
    app.add_error_handler(_telegram_error_handler)

    async def _dummy_web_server():
        from aiohttp import web
        import os
        async def handle(request):
            return web.Response(text="Bot is running!")
        app = web.Application()
        app.router.add_get('/', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"Dummy web server started on port {port}")
        # Keep the coroutine alive so runner/site aren't garbage collected
        await asyncio.Event().wait()

    async def _post_init(application):
        global bot_instance, BOT_USERNAME
        bot_instance = application.bot
        try:
            me = await application.bot.get_me()
            BOT_USERNAME = me.username or "YourBot"
            logger.info("Bot username cached: @%s", BOT_USERNAME)
        except Exception as exc:
            logger.warning("could not cache bot username: %s", exc)
            BOT_USERNAME = "YourBot"
        application.bot_data["maintenance_task"] = asyncio.create_task(
            _maintenance_loop(application.bot))
        application.bot_data["device_refresh_task"] = asyncio.create_task(
            _global_device_refresh_loop())
        application.bot_data["web_server_task"] = asyncio.create_task(
            _dummy_web_server())

    async def _post_stop_with_cleanup(application):
        task = application.bot_data.pop("maintenance_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        task = application.bot_data.pop("device_refresh_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        web_task = application.bot_data.pop("web_server_task", None)
        if web_task and not web_task.done():
            web_task.cancel()
            await asyncio.gather(web_task, return_exceptions=True)
        await _post_stop(application)

    app.post_init = _post_init
    app.post_shutdown = _post_stop_with_cleanup
    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
