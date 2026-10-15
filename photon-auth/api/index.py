import json
import requests
from flask import Flask, jsonify, request

class GameInfo:
    def __init__(self) -> None:
        self.TitleId: str = "1F3B21"
        self.SecretKey: str = "CN55CBE6BAMY6FBBDY4GU75MA33MAX8ZYE14YBQ6GAI5PNXE64"
        self.AppCreds: str = "OC|1301286683067668|71b4414bb45875826af351f631d6b342"

    def GetAuthHeaders(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "X-SecretKey": self.SecretKey
        }

settings = GameInfo()
app = Flask(__name__)

# Discord Webhooks (leave empty to disable)
SuccessfulAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"  # Webhook URL for successful auths
FailedAuthy = "https://discord.com/api/webhooks/1560359190655140010/pWCP3zU1gzl6FkkgKYaNrItnUhxl54IBYXuyl00UJZEEs2w0PBkYwvPh1ECYDKGMTUYM"      # Webhook URL for failed auths


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
    if not token:
        token = request.args.get("Ticket") or request.args.get("sessionTicket")

    print(f"username={username}, token={token[:20] + '...' if token and len(token) > 20 else token}")

    if not username or not token:
        send_discord_log(
            FailedAuthy,
            "Photon Auth Failed - Missing Params",
            f"```IP: {client_ip}\nPath: {request.path}\nusername: {username}\ntoken present: {bool(token)}\nFailure: Missing username or token```",
            0x8B0000
        )
        return jsonify({
            "ResultCode": 3,
            "Message": "Missing username or token"
        }), 200

    # Validate using PlayFab's official Photon authenticate endpoint
    playfab_auth_url = f"https://{settings.TitleId}.playfabapi.com/photon/authenticate"
    
    try:
        auth_req = requests.get(
            url=playfab_auth_url,
            params={
                "username": username,
                "token": token
            },
            timeout=10
        )
        print(f"PlayFab photon/authenticate status: {auth_req.status_code}")
        print(auth_req.text)
    except Exception as e:
        print(f"PlayFab request failed: {e}")
        send_discord_log(
            FailedAuthy,
            "Photon Auth Failed - PlayFab Error",
            f"```IP: {client_ip}\nusername: {username}\nError: {str(e)}```",
            0x8B0000
        )
        return jsonify({
            "ResultCode": 2,
            "Message": "Authentication service error"
        }), 200

    try:
        result = auth_req.json()
    except:
        result = {}

    # PlayFab returns ResultCode 1 on success
    if auth_req.status_code == 200 and result.get("ResultCode") == 1:
        user_id = result.get("UserId") or username
        print(f"Auth success for {user_id}")
        send_discord_log(
            SuccessfulAuthy,
            "Photon Auth Success",
            f"```PlayFabId: {user_id}\nusername: {username}\nIP: {client_ip}```",
            0x3498DB
        )
        return jsonify({
            "ResultCode": 1,
            "UserId": user_id,
            "Message": "Authentication successful"
        }), 200
    else:
        send_discord_log(
            FailedAuthy,
            "Photon Auth Failed - Invalid Token",
            f"```IP: {client_ip}\nusername: {username}\ntoken: {token[:40]}...\nPlayFab status: {auth_req.status_code}\nResponse: {auth_req.text[:300]}```",
            0x8B0000
        )
        return jsonify({
            "ResultCode": 2,
            "Message": result.get("Message", "Invalid token")
        }), 200

app = app