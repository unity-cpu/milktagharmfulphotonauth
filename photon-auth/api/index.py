import json
import threading
import time
import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# CONFIG  ->  fill these in
# ---------------------------------------------------------------------------
class GameInfo:
    def __init__(self) -> None:
        self.TitleId: str = "1F3B21"
        self.SecretKey: str = "FN87SC9HGNFQD93THWQ9YARI7DF4CFPU6XUKJ51JHI4GSSGBEO"
        self.AppCreds: str = "OC|1301286683067668|71b4414bb45875826af351f631d6b342"

    def GetAuthHeaders(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "X-SecretKey": self.SecretKey
        }

settings = GameInfo()
app = Flask(__name__)

# Discord webhooks (leave empty to disable)
SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"
FailedAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"

# ---- Upstash Redis (bans persist across redeploys) ------------------------
# Set these as Environment Variables in Vercel:
#   UPSTASH_REDIS_REST_URL
#   UPSTASH_REDIS_REST_TOKEN
# Or paste them here for local testing.
import os
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# ---- Ban settings ---------------------------------------------------------
BAN_ON_INVALID_TOKEN = True     # ban when the token is rejected
BAN_ON_MISSING_PARAMS = False   # ban when username/token is missing
BAN_BY_IP = False               # keep OFF on Vercel (shared/infra IPs)
BAN_BY_USERNAME = True
BAN_TTL_SECONDS = 0             # 0 = never expire; e.g. 604800 = 7 days

# ---- Webhook spam control -------------------------------------------------
WEBHOOK_COOLDOWN = 0            # 0 = one webhook per unique event, ever
NOTIFY_ON_BAN_HIT = False       # send a webhook when a banned user retries?

# ---------------------------------------------------------------------------
# UPSTASH REDIS HELPERS (REST API — no extra pip package needed)
# ---------------------------------------------------------------------------
def _redis(cmd, *args):
    """Run a Redis command via Upstash REST. Returns the 'result' field."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        print("[redis] UPSTASH_URL / UPSTASH_TOKEN not set — bans will NOT persist")
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
        data = r.json()
        return data.get("result")
    except Exception as e:
        print(f"[redis] {cmd} failed: {e}")
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
# WEBHOOK (deduped so it can't spam)
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
        payload = {
            "content": None,
            "embeds": [{
                "title": title,
                "description": description[:4000],
                "color": color
            }]
        }
        requests.post(url=webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send Discord webhook: {e}")

# ---------------------------------------------------------------------------
# CLIENT IP — Vercel-aware
# ---------------------------------------------------------------------------
def get_client_ip():
    # Vercel's own header with the real client IP
    ip = request.headers.get("x-vercel-forwarded-for")
    if ip:
        return ip.split(",")[0].strip()

    # Standard proxy header
    ip = request.headers.get("x-forwarded-for")
    if ip:
        return ip.split(",")[0].strip()

    # Vercel also sets this
    ip = request.headers.get("x-real-ip")
    if ip:
        return ip

    return request.remote_addr or "unknown"

# ---------------------------------------------------------------------------
# AUTH ROUTE
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
@app.route("/api/index", methods=["GET", "POST"])
@app.route("/api/photon", methods=["GET", "POST"])
def photonauth():
    client_ip = get_client_ip()
    body = request.get_json(silent=True) or {}

    print(f"Received {request.method} {request.path} from {client_ip}")
    print(f"Query args: {dict(request.args)}")

    username = (
        request.args.get("username")
        or body.get("username")
        or request.args.get("user")
        or request.args.get("UserId")
        or body.get("UserId")
        or body.get("user")
    )
    token = (
        request.args.get("token")
        or body.get("token")
        or request.args.get("Ticket")
        or request.args.get("sessionTicket")
        or body.get("Ticket")
        or body.get("sessionTicket")
    )

    print(f"username={username}, token={token}")

    # ---------- 1. banned? ----------
    banned, ban_type = is_banned(username, client_ip)
    if banned:
        print(f"Blocked banned {ban_type}: username={username} ip={client_ip}")
        if NOTIFY_ON_BAN_HIT:
            send_discord_log(
                f"banned-hit:{ban_type}:{(username or '').lower()}:{client_ip}",
                FailedAuthy,
                "Photon Auth Blocked - Banned",
                f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nBanned by: {ban_type}```",
                0x8B0000
            )
        return jsonify({
            "ResultCode": 2,
            "Message": "You are banned"
        }), 200

    # ---------- 2. missing params? ----------
    if not username or not token:
        if BAN_ON_MISSING_PARAMS:
            add_ban(username, client_ip, "missing username/token")
        send_discord_log(
            f"missing:{client_ip}:{(username or '').lower()}:{token}",
            FailedAuthy,
            "Photon Auth Failed - Missing Params",
            f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nFailure: Missing username or token```",
            0x8B0000
        )
        return jsonify({
            "ResultCode": 3,
            "Message": "Missing username or token"
        }), 200

    # ---------- 3. validate with PlayFab ----------
    playfab_auth_url = f"https://{settings.TitleId}.playfabapi.com/photon/authenticate"

    try:
        auth_req = requests.get(
            url=playfab_auth_url,
            params={"username": username, "token": token},
            headers=settings.GetAuthHeaders(),
            timeout=10
        )
        print(f"PlayFab photon/authenticate status: {auth_req.status_code}")
        print(auth_req.text)
    except Exception as e:
        print(f"PlayFab request failed: {e}")
        send_discord_log(
            f"playfab-error:{client_ip}:{username}:{token}",
            FailedAuthy,
            "Photon Auth Failed - PlayFab Error",
            f"```IP: {client_ip}\nusername: {username}\ntoken: {token}\nError: {str(e)}```",
            0x8B0000
        )
        return jsonify({
            "ResultCode": 2,
            "Message": "Authentication service error"
        }), 200

    try:
        result = auth_req.json()
    except Exception:
        result = {}

    result_code = result.get("ResultCode", result.get("resultCode"))
    user_id = result.get("UserId") or result.get("userId") or username

    # ---------- 4. success ----------
    if auth_req.status_code == 200 and result_code == 1:
        print(f"Auth success for {user_id}")
        send_discord_log(
            f"success:{user_id}",
            SuccessfulAuthy,
            "Photon Auth Success",
            f"```PlayFabId: {user_id}\nusername: {username}\ntoken: {token}\nIP: {client_ip}```",
            0x3498DB
        )
        return jsonify({
            "ResultCode": 1,
            "UserId": user_id,
            "Message": "Authentication successful"
        }), 200

    # ---------- 5. failure -> BAN ----------
    reason = result.get("message") or result.get("Message") or "Invalid token"

    if BAN_ON_INVALID_TOKEN:
        add_ban(username, client_ip, f"failed auth: {reason}")

    print(f"Auth FAILED -> banned username={username} ip={client_ip}")

    send_discord_log(
        f"fail:{client_ip}:{(username or '').lower()}:{token}",
        FailedAuthy,
        "Photon Auth Failed - Banned",
        f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nPlayFab status: {auth_req.status_code}\nReason: {reason}\nResponse: {auth_req.text}```",
        0x8B0000
    )

    return jsonify({
        "ResultCode": 2,
        "Message": reason
    }), 200

# ---------------------------------------------------------------------------
# ADMIN: unban
#   GET/POST /admin/unban?username=X&ip=Y   (with X-SecretKey header)
#   GET/POST /admin/bans                    (list all bans)
# ---------------------------------------------------------------------------
@app.route("/admin/unban", methods=["POST", "GET"])
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

@app.route("/admin/bans", methods=["GET"])
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
# VERCEL ROUTING — required so /admin/* reaches Flask instead of 404-ing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)