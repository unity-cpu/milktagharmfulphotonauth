import json
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Discord Webhooks (leave empty to disable)
SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"  # Webhook URL for successful auths
FailedAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"      # Webhook URL for failed auths
# Simple in-memory sets so we only log each user once
logged_success = set()
logged_failed = set()

def send_discord_log(webhook_url: str, title: str, description: str, color: int):
    if not webhook_url:
        return
    try:
        embed = {
            "content": None,
            "embeds": [{
                "title": title,
                "description": description,
                "color": color
            }]
        }
        requests.post(url=webhook_url, json=embed, timeout=5)
    except Exception as e:
        print(f"Failed to send Discord webhook: {e}")

def get_client_ip():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    return client_ip or "unknown"

@app.route("/", methods=["GET", "POST"])
@app.route("/api/index", methods=["GET", "POST"])
@app.route("/api/photon", methods=["GET", "POST"])
def photonauth():
    client_ip = get_client_ip()
    print(f"Received {request.method} {request.path} from {client_ip}")
    print(f"Query args: {dict(request.args)}")

    username = request.args.get("username") or (request.get_json(silent=True) or {}).get("username")
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token")

    if not username:
        username = request.args.get("user") or request.args.get("UserId")

    print(f"username={username}, token={token}")

    # ===== REJECT people with no username =====
    if not username:
        key = f"fail:{client_ip}"
        if key not in logged_failed:
            logged_failed.add(key)
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - No Username",
                f"```IP: {client_ip}\nToken: {token}\nFailure: Missing username - REJECTED```",
                0x8B0000
            )
        return jsonify({
            "ResultCode": 3,
            "Message": "Missing username"
        }), 200

    # ===== ACCEPT and log only once =====
    if username not in logged_success:
        logged_success.add(username)
        send_discord_log(
            SuccessfulAuthy,
            "Photon Auth Success",
            f"```UserId: {username}\nIP: {client_ip}\nToken: {token}```",
            0x3498DB
        )

    print(f"Auth success for {username}")
    return jsonify({
        "ResultCode": 1,
        "UserId": username,
        "Message": "Authentication successful"
    }), 200

app = app