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

def get_user_oculus_id(playfab_id: str) -> str | None:
    req = requests.post(
        url=f"https://{settings.TitleId}.playfabapi.com/Server/GetUserReadOnlyData",
        json={
            "PlayFabId": playfab_id,
            "Keys": ["OculusId"]
        },
        headers=settings.GetAuthHeaders(),
        timeout=10
    )
    if req.status_code == 200:
        data = req.json().get("data", {}).get("Data", {})
        return data.get("OculusId", {}).get("Value")
    return None

@app.route("/api/photon", methods=["POST", "GET"])
def photonauth():
    print(f"Received {request.method} request at /api/photon")
    AuthTicketUrl = f"https://{settings.TitleId}.playfabapi.com/Server/AuthenticateSessionTicket"
    VALID_APPS = [f"{settings.TitleId}"]

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()

    if request.method == "GET":
        return jsonify({
            "resultCode": 3,
            "message": "GET method not allowed",
            "userId": None,
            "nickname": None
        }), 405

    elif request.method == "POST":
        newData = request.get_json()
        AppId = newData.get("AppId")
        AppVersion = newData.get("AppVersion")
        Ticket = newData.get("Ticket")
        Token = newData.get("Token")
        Nonce = newData.get("Nonce")
        Platform = newData.get("Platform")
        OculusId = newData.get("OculusId")

        print(json.dumps(newData, indent=2))

        if not all([AppId, AppVersion, Ticket, Nonce, Platform, OculusId]):
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Missing Parameters",
                f"```IP: {client_ip}\nAppId: {AppId}\nOculusId: {OculusId}\nPlatform: {Platform}\nFailure: Missing required parameters```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 3,
                "Message": "Missing required parameters",
                "Error": "BadRequestMissingParams"
            }), 400

        if AppId not in VALID_APPS:
            print(f"Invalid AppId: {AppId}")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Invalid AppId",
                f"```IP: {client_ip}\nAppId: {AppId}\nOculusId: {OculusId}\nExpected: {settings.TitleId}```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 2,
                "Message": "Invalid AppId parameter",
                "Error": "BadRequestWrongAppId"
            }), 403

        if Platform != "Quest":
            print("Users Platform Is Not Quest")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Invalid Platform",
                f"```IP: {client_ip}\nOculusId: {OculusId}\nPlatform: {Platform}\nFailure: Only Quest is allowed```",
                0x8B0000
            )
            return jsonify({
                "Error": "Bad request",
                "ResultCode": 3,
                "Message": "Platform Must Be Quest"
            }), 403

        if not OculusId.isdigit():
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Invalid OculusId",
                f"```IP: {client_ip}\nOculusId: {OculusId}\nFailure: OculusId is not numeric```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 3,
                "Message": "Invalid OculusId",
                "Error": "BadRequestInvalidOculusId"
            }), 403

        AuthSessionTicketReq = requests.post(
            url=AuthTicketUrl,
            json={"SessionTicket": Ticket},
            headers=settings.GetAuthHeaders(),
            timeout=10
        )
        print(f"Session ticket auth status: {AuthSessionTicketReq.status_code}")

        if AuthSessionTicketReq.status_code != 200:
            print(f"SessionTicket: {Ticket} Is Invalid")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Invalid SessionTicket",
                f"```IP: {client_ip}\nOculusId: {OculusId}\nTicket: {Ticket[:40]}...\nFailure: SessionTicket rejected by PlayFab```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 2,
                "Message": "Invalid SessionTicket parameter",
                "Error": "BadRequestBadSessionTicket"
            }), 403

        getdata = AuthSessionTicketReq.json().get("data").get("UserInfo", {})
        UserId = getdata.get("PlayFabId")

        stored_oculus_id = get_user_oculus_id(UserId)
        if stored_oculus_id is None:
            print(f"No stored OculusId for user {UserId}")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - No Stored OculusId",
                f"```IP: {client_ip}\nPlayFabId: {UserId}\nOculusId: {OculusId}\nFailure: User has no OculusId stored```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 3,
                "Message": "User not properly set up",
                "Error": "BadRequestNoStoredOculusId"
            }), 403

        if stored_oculus_id != OculusId:
            print(f"OculusId mismatch: provided {OculusId} vs stored {stored_oculus_id}")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - OculusId Mismatch",
                f"```IP: {client_ip}\nPlayFabId: {UserId}\nProvided: {OculusId}\nStored: {stored_oculus_id}```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 3,
                "Message": "OculusId does not match account",
                "Error": "BadRequestOculusIdMismatch"
            }), 403

        VerifyNonceReq = requests.post(
            url="https://graph.oculus.com/user_nonce_validate",
            json={
                "access_token": settings.AppCreds,
                "nonce": Nonce,
                "user_id": OculusId
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        print(f"Nonce validation response: {VerifyNonceReq.status_code}")
        print(json.dumps(VerifyNonceReq.json(), indent=2))

        nonce_json_data = VerifyNonceReq.json()
        if VerifyNonceReq.status_code != 200 or not nonce_json_data.get("is_valid", False):
            print(f"User: {UserId} Has Failed The Nonce Verification. Nonce: {Nonce}")
            send_discord_log(
                FailedAuthy,
                "Photon Auth Failed - Nonce Verification",
                f"```IP: {client_ip}\nPlayFabId: {UserId}\nOculusId: {OculusId}\nNonce: {Nonce}\nFailure: Nonce validation failed```",
                0x8B0000
            )
            return jsonify({
                "ResultCode": 1,
                "Message": "Failed Nonce Verification",
                "Error": "BadRequestInvalidNonce"
            }), 403

        print(f"{UserId} Was Authed Succesfully.")
        send_discord_log(
            SuccessfulAuthy,
            "Photon Auth Success",
            f"```PlayFabId: {UserId}\nOculusId: {OculusId}\nAppId: {AppId}\nAppVersion: {AppVersion}\nIP: {client_ip}```",
            0x3498DB
        )
        return jsonify({
            "ResultCode": 1,
            "Message": "Authentication successful",
            "AppId": AppId,
            "AppVersion": AppVersion,
            "Nonce": Nonce,
            "OculusId": OculusId,
            "Ticket": Ticket,
            "Token": Token,
            "UserId": UserId
        }), 200

# Required for Vercel
app = app
