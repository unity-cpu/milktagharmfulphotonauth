import os
import threading
import time
import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
class GameInfo:
    def __init__(self) -> None:
        # Must match Unity's TITLE_ID
        self.TitleId: str = "116C19"
        self.SecretKey: str = "FN87SC9HGNFQD93THWQ9YARI7DF4CFPU6XUKJ51JHI4GSSGBEO"
        self.AppCreds: str = "OC|1301286683067668|71b4414bb45875826af351f631d6b342"

        # Must match Unity's APP_ID_REALTIME
        self.PhotonAppId: str = "d367d3f3-d294-4eef-8b35-3d0722fab130"

        # Must match Unity's APP_VERSION. Empty to disable.
        self.ExpectedAppVersion: str = "live11143"

        # Timestamp tolerance (seconds)
        self.MaxTimestampSkew: int = 60

        # Nonce TTL (seconds)
        self.NonceTtl: int = 300

        # Per-IP rate limit
        self.RateLimitMax: int = 15
        self.RateLimitWindow: int = 60

        # Auto-ban on hardening failures (nonce replay is NOT included)
        self.BanOnHardeningFailure: bool = True

    def GetAuthHeaders(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "X-SecretKey": self.SecretKey
        }

settings = GameInfo()
app = Flask(__name__)

SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"
FailedAuthy     = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

BAN_ON_INVALID_TOKEN   = True
BAN_ON_MISSING_PARAMS  = False
BAN_BY_IP              = False
BAN_BY_USERNAME        = True
BAN_TTL_SECONDS        = 0

WEBHOOK_COOLDOWN  = 0
NOTIFY_ON_BAN_HIT = False

# ---------------------------------------------------------------------------
# UPSTASH REDIS HELPERS
# ---------------------------------------------------------------------------
def _redis(cmd, *args):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        r = requests.post(
            UPSTASH_URL,
            headers={
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
                "Content-Type": "application/json",
            },
            json=[cmd, *args],
            timeout=5,
        )
        return r.json().get("result")
    except Exception:
        return None

def _ban_key_username(username: str) -> str:
    return f"ban:u:{username.lower()}"

def _ban_key_ip(ip: str) -> str:
    return f"ban:ip:{ip}"

def is_banned(username, ip):
    if username and _redis("EXISTS", _ban_key_username(username)):
        return True, "username"
    if BAN_BY_IP and ip and _redis("EXISTS", _ban_key_ip(ip)):
        return True, "ip"
    return False, ""

def add_ban(username, ip, reason: str) -> None:
    if BAN_BY_USERNAME and username:
        key = _ban_key_username(username)
        if BAN_TTL_SECONDS > 0:
            _redis("SET", key, reason, "EX", str(BAN_TTL_SECONDS))
        else:
            _redis("SET", key, reason)
    if BAN_BY_IP and ip:
        key = _ban_key_ip(ip)
        if BAN_TTL_SECONDS > 0:
            _redis("SET", key, reason, "EX", str(BAN_TTL_SECONDS))
        else:
            _redis("SET", key, reason)

def remove_ban(username, ip) -> None:
    if username:
        _redis("DEL", _ban_key_username(username))
    if ip:
        _redis("DEL", _ban_key_ip(ip))

# ---------------------------------------------------------------------------
# HARDENING HELPERS
# ---------------------------------------------------------------------------
def _rate_limit_check(ip: str) -> bool:
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return True
    bucket = int(time.time() // settings.RateLimitWindow)
    key = f"rl:{ip}:{bucket}"
    count = _redis("INCR", key)
    if count == 1:
        _redis("EXPIRE", key, str(settings.RateLimitWindow))
    try:
        return int(count) <= settings.RateLimitMax
    except (TypeError, ValueError):
        return True

def _consume_nonce(nonce: str, username: str, token: str) -> bool:
    """
    Returns True if the nonce is fresh OR if it's a retry of the exact
    same request. Returns False only when the same nonce is reused with
    a DIFFERENT (username, ticket) pair — that's the actual replay case.
    """
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return True

    key = f"nonce:{nonce}"
    fingerprint = f"{username}|{token}"

    result = _redis("SET", key, fingerprint, "EX", str(settings.NonceTtl), "NX")

    if result == "OK":
        return True  # brand new nonce

    existing = _redis("GET", key)
    if existing == fingerprint:
        return True  # same request retried — allow

    return False  # same nonce, different request — replay

def _timestamp_ok(ts) -> bool:
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - t) <= settings.MaxTimestampSkew

def _version_ok(v) -> bool:
    if not settings.ExpectedAppVersion:
        return True
    return v == settings.ExpectedAppVersion

# ---------------------------------------------------------------------------
# WEBHOOKS
# ---------------------------------------------------------------------------
_wh_lock = threading.Lock()
_wh_history: dict[str, float] = {}

def _should_send(key: str) -> bool:
    now = time.time()
    with _wh_lock:
        last = _wh_history.get(key)
        if last is None:
            _wh_history[key] = now
            return True
        if WEBHOOK_COOLDOWN > 0 and (now - last) >= WEBHOOK_COOLDOWN:
            _wh_history[key] = now
            return True
        return False

def send_discord_log(key: str, webhook_url: str, title: str, description: str, color: int):
    if not webhook_url:
        return
    if not _should_send(key):
        return
    try:
        requests.post(
            url=webhook_url,
            json={"content": None, "embeds": [{
                "title": title,
                "description": description[:4000],
                "color": color
            }]},
            timeout=5
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# CLIENT IP (Vercel-aware)
# ---------------------------------------------------------------------------
def get_client_ip():
    ip = request.headers.get("x-vercel-forwarded-for")
    if ip:
        return ip.split(",")[0].strip()
    ip = request.headers.get("x-forwarded-for")
    if ip:
        return ip.split(",")[0].strip()
    ip = request.headers.get("x-real-ip")
    if ip:
        return ip
    return request.remote_addr or "unknown"

# ---------------------------------------------------------------------------
# PLAYFAB HELPERS
# ---------------------------------------------------------------------------
def playfab_authenticate_session_ticket(session_ticket: str):
    url = f"https://{settings.TitleId}.playfabapi.com/Server/AuthenticateSessionTicket"
    try:
        r = requests.post(
            url=url,
            json={"SessionTicket": session_ticket},
            headers={
                "content-type": "application/json",
                "X-SecretKey": settings.SecretKey,
            },
            timeout=10,
        )
    except Exception as e:
        return False, None, {"error": str(e)}

    try:
        data = r.json()
    except Exception:
        data = {}

    if data.get("code") == 200:
        user_info = (data.get("data") or {}).get("UserInfo") or {}
        return True, user_info.get("PlayFabId"), data
    return False, None, data

def playfab_get_photon_token(session_ticket: str):
    url = f"https://{settings.TitleId}.playfabapi.com/Client/GetPhotonAuthenticationToken"
    try:
        r = requests.post(
            url=url,
            json={"PhotonApplicationId": settings.PhotonAppId},
            headers={
                "content-type": "application/json",
                "X-Authorization": session_ticket,
            },
            timeout=10,
        )
    except Exception as e:
        return False, None, {"error": str(e)}

    try:
        data = r.json()
    except Exception:
        data = {}

    if data.get("code") == 200:
        token = (data.get("data") or {}).get("PhotonCustomAuthenticationToken")
        return bool(token), token, data
    return False, None, data

# ---------------------------------------------------------------------------
# AUTH HANDLER
# ---------------------------------------------------------------------------
def _getp(body, *names):
    for n in names:
        v = request.args.get(n)
        if v:
            return v
        v = body.get(n)
        if v:
            return v
    return None

def photonauth():
    client_ip = get_client_ip()
    body = request.get_json(silent=True) or {}

    username = _getp(body, "username", "user", "UserId")
    token    = _getp(body, "token", "Ticket", "sessionTicket")
    ts       = _getp(body, "ts", "timestamp")
    nonce    = _getp(body, "nonce")
    app_ver  = _getp(body, "appVersion", "version", "AppVersion")

    # HARDENING LAYER 1: rate limit
    if not _rate_limit_check(client_ip):
        return jsonify({"ResultCode": 4, "Message": "Rate limit exceeded"}), 429

    banned, ban_type = is_banned(username, client_ip)
    if banned:
        if NOTIFY_ON_BAN_HIT:
            send_discord_log(
                f"banned-hit:{ban_type}:{(username or '').lower()}:{client_ip}",
                FailedAuthy, "Photon Auth Blocked - Banned",
                f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nBanned by: {ban_type}```",
                0x8B0000
            )
        return jsonify({"ResultCode": 2, "Message": "You are banned"}), 200

    if not username or not token:
        if BAN_ON_MISSING_PARAMS:
            add_ban(username, client_ip, "missing username/token")
        send_discord_log(
            f"missing:{client_ip}:{(username or '').lower()}:{token}",
            FailedAuthy, "Photon Auth Failed - Missing Params",
            f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}```",
            0x8B0000
        )
        return jsonify({"ResultCode": 3, "Message": "Missing username or token"}), 200

    # HARDENING LAYER 2: version
    if not _version_ok(app_ver):
        if settings.BanOnHardeningFailure:
            add_ban(username, client_ip, f"bad app version: {app_ver}")
        send_discord_log(
            f"hard-version:{client_ip}:{username}",
            FailedAuthy, "Photon Auth Blocked - Version",
            f"```IP: {client_ip}\nusername: {username}\nappVersion: {app_ver}\nexpected: {settings.ExpectedAppVersion}```",
            0x8B0000
        )
        return jsonify({"ResultCode": 5, "Message": "Outdated client"}), 200

    # HARDENING LAYER 3: timestamp
    if not _timestamp_ok(ts):
        if settings.BanOnHardeningFailure:
            add_ban(username, client_ip, f"bad timestamp: {ts}")
        send_discord_log(
            f"hard-ts:{client_ip}:{username}",
            FailedAuthy, "Photon Auth Blocked - Timestamp",
            f"```IP: {client_ip}\nusername: {username}\nts: {ts}\nskew limit: {settings.MaxTimestampSkew}s```",
            0x8B0000
        )
        return jsonify({"ResultCode": 5, "Message": "Timestamp out of range"}), 200

    # HARDENING LAYER 4: nonce replay
    # NOTE: no add_ban here on purpose — Photon retries legitimate requests,
    # and Redis races can produce false positives. Reject only.
    if not nonce or not _consume_nonce(nonce, username, token):
        send_discord_log(
            f"hard-nonce:{client_ip}:{username}",
            FailedAuthy, "Photon Auth Blocked - Replay",
            f"```IP: {client_ip}\nusername: {username}\nnonce: {nonce}```",
            0x8B0000
        )
        return jsonify({"ResultCode": 5, "Message": "Replay detected"}), 200

    # STEP 1 — validate session ticket
    ok, playfab_id, auth_data = playfab_authenticate_session_ticket(token)

    if not ok or not playfab_id:
        reason = (
            auth_data.get("errorMessage")
            or auth_data.get("error")
            or auth_data.get("status")
            or "Invalid session ticket"
        )
        if BAN_ON_INVALID_TOKEN:
            add_ban(username, client_ip, f"failed auth: {reason}")

        send_discord_log(
            f"fail:{client_ip}:{(username or '').lower()}:{token}",
            FailedAuthy, "Photon Auth Failed - Banned",
            f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nReason: {reason}\nRaw: {auth_data}```",
            0x8B0000
        )
        return jsonify({"ResultCode": 2, "Message": reason}), 200

    # STEP 2 — fetch full Photon token
    photon_ok, photon_token, photon_data = playfab_get_photon_token(token)

    if not photon_ok or not photon_token:
        send_discord_log(
            f"success-no-photon:{playfab_id}",
            SuccessfulAuthy, "Photon Auth OK (no token)",
            f"```PlayFabId: {playfab_id}\nusername: {username}\nIP: {client_ip}\n"
            f"Reason: GetPhotonAuthenticationToken did not return a token\n"
            f"Raw: {photon_data}```",
            0xF1C40F
        )
        return jsonify({
            "ResultCode": 1,
            "UserId": playfab_id,
            "PhotonToken": None,
            "SessionTicket": token,
            "Message": "Authenticated but no PhotonToken returned"
        }), 200

    # FULL SUCCESS — full token, no truncation
    send_discord_log(
        f"success:{playfab_id}",
        SuccessfulAuthy, "Photon Auth Success",
        f"```PlayFabId: {playfab_id}\n"
        f"username: {username}\n"
        f"IP: {client_ip}\n"
        f"SessionTicket: {token}\n"
        f"PhotonToken: {photon_token}```",
        0x3498DB
    )

    return jsonify({
        "ResultCode": 1,
        "UserId": playfab_id,
        "PhotonToken": photon_token,
        "SessionTicket": token,
        "Message": "Authentication successful"
    }), 200

# ---------------------------------------------------------------------------
# ADMIN HANDLERS
# ---------------------------------------------------------------------------
def admin_unban():
    key = request.headers.get("X-SecretKey") or request.args.get("secret")
    if not settings.SecretKey or key != settings.SecretKey:
        return jsonify({"error": "unauthorized"}), 403

    username = request.args.get("username")
    ip = request.args.get("ip")
    if not username and not ip:
        return jsonify({"error": "provide username and/or ip"}), 400

    remove_ban(username, ip)
    return jsonify({"ok": True, "unbanned": {"username": username, "ip": ip}}), 200

def admin_bans():
    key = request.headers.get("X-SecretKey") or request.args.get("secret")
    if not settings.SecretKey or key != settings.SecretKey:
        return jsonify({"error": "unauthorized"}), 403

    usernames = _redis("KEYS", "ban:u:*") or []
    ips = _redis("KEYS", "ban:ip:*") or []
    return jsonify({
        "usernames": [k.replace("ban:u:", "") for k in usernames],
        "ips": [k.replace("ban:ip:", "") for k in ips],
    }), 200

# ---------------------------------------------------------------------------
# CATCH-ALL (Vercel rewrites)
# ---------------------------------------------------------------------------
@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def catch_all(path):
    full_path = "/" + path if path else "/"

    if "admin/unban" in full_path or "admin/unban" in request.path:
        return admin_unban()
    elif "admin/bans" in full_path or "admin/bans" in request.path:
        return admin_bans()
    else:
        return photonauth()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)