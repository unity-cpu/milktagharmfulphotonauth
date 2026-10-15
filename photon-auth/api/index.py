import json
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

# Discord webhooks
SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"
FailedAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"

# Upstash Redis
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# Ban settings
BAN_ON_INVALID_TOKEN = True
BAN_ON_MISSING_PARAMS = False
BAN_BY_IP = False
BAN_BY_USERNAME = True
BAN_TTL_SECONDS = 0

# Webhook spam control
WEBHOOK_COOLDOWN = 0
NOTIFY_ON_BAN_HIT = False

# ---------------------------------------------------------------------------
# UPSTASH REDIS HELPERS
# ---------------------------------------------------------------------------
def _redis(cmd, *args):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        print("[redis] env vars missing — bans will NOT persist")
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
    except Exception as e:
        print(f"Failed to send Discord webhook: {e}")

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
# AUTH HANDLER
# ---------------------------------------------------------------------------
def photonauth():
    client_ip = get_client_ip()
    body = request.get_json(silent=True) or {}

    print(f"Received {request.method} {request.path} from {client_ip}")
    print(f"Query args: {dict(request.args)}")

    username = (
        request.args.get("username") or body.get("username")
        or request.args.get("user") or request.args.get("UserId")
        or body.get("UserId") or body.get("user")
    )
    token = (
        request.args.get("token") or body.get("token")
        or request.args.get("Ticket") or request.args.get("sessionTicket")
        or body.get("Ticket") or body.get("sessionTicket")
    )

    print(f"username={username}, token={token}")

    banned, ban_type = is_banned(username, client_ip)
    if banned:
        print(f"Blocked banned {ban_type}: username={username} ip={client_ip}")
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

    playfab_auth_url = f"https://{settings.TitleId}.playfabapi.com/photon/authenticate"
    try:
        auth_req = requests.get(
            url=playfab_auth_url,
            params={"username": username, "token": token},
            headers=settings.GetAuthHeaders(),
            timeout=10
        )
        print(f"PlayFab status: {auth_req.status_code}")
        print(auth_req.text)
    except Exception as e:
        print(f"PlayFab request failed: {e}")
        send_discord_log(
            f"playfab-error:{client_ip}:{username}:{token}",
            FailedAuthy, "Photon Auth Failed - PlayFab Error",
            f"```IP: {client_ip}\nusername: {username}\ntoken: {token}\nError: {str(e)}```",
            0x8B0000
        )
        return jsonify({"ResultCode": 2, "Message": "Authentication service error"}), 200

    try:
        result = auth_req.json()
    except Exception:
        result = {}

    # PlayFab wraps data under "data" sometimes; unwrap if present
    if "data" in result and isinstance(result["data"], dict):
        result = result["data"]

    result_code = result.get("ResultCode", result.get("resultCode"))
    user_id = result.get("UserId") or result.get("userId") or username

    # ---------- SUCCESS ----------
    if auth_req.status_code == 200 and result_code == 1:
        # THIS IS THE LINE YOU WERE MISSING
        photon_token = (
            result.get("PhotonToken")
            or result.get("photonToken")
            or result.get("Token")
            or result.get("token")
        )

        print(f"Auth success for {user_id}, PhotonToken={'yes' if photon_token else 'NO'}")

        send_discord_log(
            f"success:{user_id}",
            SuccessfulAuthy, "Photon Auth Success",
            f"```PlayFabId: {user_id}\nusername: {username}\nIP: {client_ip}```",
            0x3498DB
        )

        return jsonify({
            "ResultCode": 1,
            "UserId": user_id,
            "PhotonToken": photon_token,
            "Message": "Authentication successful"
        }), 200

    # ---------- FAILURE ----------
    reason = result.get("message") or result.get("Message") or "Invalid token"
    if BAN_ON_INVALID_TOKEN:
        add_ban(username, client_ip, f"failed auth: {reason}")

    print(f"Auth FAILED -> banned username={username} ip={client_ip}")
    send_discord_log(
        f"fail:{client_ip}:{(username or '').lower()}:{token}",
        FailedAuthy, "Photon Auth Failed - Banned",
        f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken: {token}\nPlayFab status: {auth_req.status_code}\nReason: {reason}\nResponse: {auth_req.text}```",
        0x8B0000
    )

    return jsonify({"ResultCode": 2, "Message": reason}), 200

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
# CATCH-ALL (required for Vercel rewrites)
# ---------------------------------------------------------------------------
@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def catch_all(path):
    full_path = "/" + path if path else "/"

    print(f"[catch_all] reconstructed path: {full_path}")

    if "admin/unban" in full_path or "admin/unban" in request.path:
        return admin_unban()
    elif "admin/bans" in full_path or "admin/bans" in request.path:
        return admin_bans()
    else:
        return photonauth()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)