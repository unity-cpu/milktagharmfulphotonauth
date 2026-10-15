import json
import os
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
SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"  # Webhook URL for successful auths
FailedAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"      # Webhook URL for failed auths

# ---- Ban settings ---------------------------------------------------------
BAN_FILE = "bans.json"          # where bans are stored
BAN_ON_INVALID_TOKEN = True     # ban when the token is rejected
BAN_ON_MISSING_PARAMS = False   # ban when username/token is missing
BAN_BY_IP = True                # also ban the IP (turn off if players share IPs)
BAN_BY_USERNAME = True

# ---- Webhook spam control -------------------------------------------------
# 0  = send ONE webhook per unique event, ever (until restart)
# 60 = send at most one webhook per unique event every 60 seconds
WEBHOOK_COOLDOWN = 0

# ---------------------------------------------------------------------------
# BAN STORAGE
# ---------------------------------------------------------------------------
_ban_lock = threading.Lock()

def _load_bans() -> dict:
    if not os.path.exists(BAN_FILE):
        return {"usernames": {}, "ips": {}}
    try:
        with open(BAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[bans] failed to load: {e}")
        return {"usernames": {}, "ips": {}}
    data.setdefault("usernames", {})
    data.setdefault("ips", {})
    return data

def _save_bans(data: dict) -> None:
    try:
        with open(BAN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[bans] failed to save: {e}")

bans = _load_bans()

def is_banned(username: str | None, ip: str | None) -> tuple[bool, str]:
    with _ban_lock:
        if username and username.lower() in bans["usernames"]:
            return True, "username"
        if ip and ip in bans["ips"]:
            return True, "ip"
    return False, ""

def add_ban(username: str | None, ip: str | None, reason: str) -> None:
    with _ban_lock:
        now = time.time()
        if BAN_BY_USERNAME and username:
            bans["usernames"][username.lower()] = {"reason": reason, "banned_at": now}
        if BAN_BY_IP and ip:
            bans["ips"][ip] = {"reason": reason, "banned_at": now}
        _save_bans(bans)

def remove_ban(username: str | None, ip: str | None) -> None:
    with _ban_lock:
        if username:
            bans["usernames"].pop(username.lower(), None)
        if ip:
            bans["ips"].pop(ip, None)
        _save_bans(bans)

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
    """key = unique identifier for this event. Same key is only sent once
    (or once per WEBHOOK_COOLDOWN seconds)."""
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

def get_client_ip():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    return client_ip or "unknown"

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

    # --- pull username / token from anywhere the client puts them ---
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

    # FULL token, no truncation
    print(f"username={username}, token={token}")

    # ---------- 1. banned? ----------
    banned, ban_type = is_banned(username, client_ip)
    if banned:
        print(f"Blocked banned {ban_type}: username={username} ip={client_ip}")
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

    # PlayFab replies with lowercase "resultCode" -> check both casings
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

    # ---------- 5. failure -> BAN + full token in webhook ----------
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
# ADMIN: unban (POST /admin/unban?username=X&ip=Y  with X-SecretKey header)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)