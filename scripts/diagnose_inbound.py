"""Why is no message arriving? Check the whole inbound path, end to end.

Inbound WhatsApp has four things that must all be true, and when a message
goes missing it is rarely obvious which one broke:

  1. Twilio is pointed at this deployment's webhook URL.
  2. The URL is reachable and answers.
  3. The destination number maps to an organization (`channel_configs`).
  4. The signature validates against that tenant's auth token.

This reports on each in order, so the answer is a line in the output rather
than a guess.

    python scripts/diagnose_inbound.py

Reads credentials from the local .env. Writes nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import pathlib
import sys

import httpx

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"
PUBLIC_URL = os.environ.get("PINGPULSE_URL", "https://pingpulse.duckdns.org")
WEBHOOK_PATH = "/api/v1/whatsapp/webhook"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def ok(text: str) -> None:
    print(f"  \033[1;32mok  \033[0m {text}")


def bad(text: str) -> None:
    print(f"  \033[1;31mFAIL\033[0m {text}")


def warn(text: str) -> None:
    print(f"  \033[1;33m??  \033[0m {text}")


def sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def main() -> int:
    env = load_env()
    sid = env.get("TWILIO_ACCOUNT_SID", "")
    token = env.get("TWILIO_AUTH_TOKEN", "")
    number = env.get("TWILIO_WHATSAPP_NUMBER", "")

    print(f"\n\033[1mdeployment\033[0m {PUBLIC_URL}\n")

    # ---------------------------------------------------------- 1. reachable
    print("\033[1m1. Is the webhook reachable?\033[0m")
    try:
        response = httpx.get(f"{PUBLIC_URL}{WEBHOOK_PATH}", timeout=20)
        if response.status_code == 200:
            ok(f"GET {WEBHOOK_PATH} -> 200")
        else:
            bad(f"GET {WEBHOOK_PATH} -> {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        bad(f"cannot reach {PUBLIC_URL}: {exc}")
        return 1

    # ------------------------------------------------- 2. signature enforced
    print("\n\033[1m2. Is the webhook rejecting forgeries?\033[0m")
    form = {
        "MessageSid": "SM_diag",
        "From": "whatsapp:+10000000000",
        "To": number or "whatsapp:+14155238886",
        "Body": "diagnostic",
        "NumMedia": "0",
    }
    unsigned = httpx.post(f"{PUBLIC_URL}{WEBHOOK_PATH}", data=form, timeout=30)
    if unsigned.status_code == 403:
        ok("unsigned request rejected (signature validation is on)")
    else:
        warn(f"unsigned request returned {unsigned.status_code} — validation is OFF")

    # --------------------------------------------- 3. does a real one land?
    print("\n\033[1m3. Does a correctly signed message get through?\033[0m")
    if not token:
        warn("no TWILIO_AUTH_TOKEN in .env, cannot sign a test message")
    else:
        signature = sign(token, f"{PUBLIC_URL}{WEBHOOK_PATH}", form)
        signed = httpx.post(
            f"{PUBLIC_URL}{WEBHOOK_PATH}",
            data=form,
            headers={"X-Twilio-Signature": signature},
            timeout=120,
        )
        if signed.status_code == 200:
            ok("signed request accepted (200)")
        elif signed.status_code == 403:
            bad(
                "signed request REJECTED. The platform auth token does not match "
                "what the server expects for this number — check the channel's "
                "own credentials, or PUBLIC_BASE_URL not matching the real URL."
            )
        else:
            bad(f"signed request -> {signed.status_code}")

    # --------------------------------------------- 4. what Twilio points at
    print("\n\033[1m4. Where is Twilio actually sending messages?\033[0m")
    if not (sid and token):
        warn("no Twilio credentials in .env, cannot ask Twilio")
        return 0

    try:
        auth = (sid, token)
        numbers = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
            auth=auth,
            timeout=30,
        ).json()

        found = False
        for entry in numbers.get("incoming_phone_numbers", []):
            found = True
            configured = entry.get("sms_url") or "(not set)"
            label = f"{entry.get('phone_number')}: {configured}"
            if PUBLIC_URL in str(configured):
                ok(label)
            else:
                bad(label + "   <-- not pointed at this deployment")

        if not found:
            warn(
                "no purchased numbers on this account — you are on the WhatsApp "
                "sandbox, whose webhook is set in the Console under\n"
                "        Messaging > Try it out > Send a WhatsApp message > Sandbox settings\n"
                "        and cannot be read back over the API."
            )
    except Exception as exc:  # noqa: BLE001
        warn(f"could not query Twilio: {exc}")

    print(f"\n\033[1mSet the webhook to\033[0m  {PUBLIC_URL}{WEBHOOK_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
