
import base64
import datetime
from dataclasses import dataclass
from typing import Optional

import requests
from django.conf import settings

DARAJA_BASE_URLS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}


@dataclass
class StkPushResult:
    checkout_request_id: str
    merchant_request_id: str
    response_code: str
    response_description: str


class MpesaClient:
    def __init__(self):
        self.base_url = DARAJA_BASE_URLS[settings.MPESA_ENV]

    def _get_access_token(self) -> str:
        resp = requests.get(
            f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
            auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _password(self, timestamp: str) -> str:
        raw = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    def _callback_url(self) -> str:

        url = settings.MPESA_CALLBACK_URL
        if settings.MPESA_CALLBACK_SECRET:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}token={settings.MPESA_CALLBACK_SECRET}"
        return url

    def stk_push(self, phone_number: str, amount: int, account_reference: str, description: str) -> StkPushResult:

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        token = self._get_access_token()
        payload = {
            "BusinessShortCode": settings.MPESA_SHORTCODE,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone_number,
            "PartyB": settings.MPESA_SHORTCODE,
            "PhoneNumber": phone_number,
            "CallBackURL": self._callback_url(),
            "AccountReference": account_reference,
            "TransactionDesc": description,
        }
        resp = requests.post(
            f"{self.base_url}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return StkPushResult(
            checkout_request_id=data["CheckoutRequestID"],
            merchant_request_id=data["MerchantRequestID"],
            response_code=data["ResponseCode"],
            response_description=data["ResponseDescription"],
        )


def verify_callback_token(request) -> bool:

    token = request.GET.get("token", "")
    return bool(settings.MPESA_CALLBACK_SECRET) and token == settings.MPESA_CALLBACK_SECRET


def parse_stk_callback(payload: dict) -> Optional[dict]:
    try:
        stk = payload["Body"]["stkCallback"]
        result = {
            "checkout_request_id": stk["CheckoutRequestID"],
            "merchant_request_id": stk["MerchantRequestID"],
            "result_code": stk["ResultCode"],
            "result_desc": stk["ResultDesc"],
            "receipt_number": None,
            "amount": None,
            "phone_number": None,
        }
        items = stk.get("CallbackMetadata", {}).get("Item", [])
        for item in items:
            name = item.get("Name")
            value = item.get("Value")
            if name == "MpesaReceiptNumber":
                result["receipt_number"] = value
            elif name == "Amount":
                result["amount"] = value
            elif name == "PhoneNumber":
                result["phone_number"] = value
        return result
    except (KeyError, TypeError):
        return None