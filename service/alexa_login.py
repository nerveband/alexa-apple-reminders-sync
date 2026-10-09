"""One-time Alexa sign-in. Saves a long-lived token to /data/alexa_auth.json.

Run:  docker compose run --rm reminders-sync python alexa_login.py
"""
import asyncio
import json
import os
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from alexapy import AlexaLogin

DATA = os.environ.get("DATA_DIR", "/data")


async def main():
    email = os.environ.get("ALEXA_EMAIL") or input("Amazon email: ").strip()
    uuid = uuid4().hex.upper()
    out = lambda n: os.path.join(DATA, n)
    first = AlexaLogin("amazon.com", email, "", out, False, uuid=uuid)
    verifier, challenge = first.code_verifier, first.code_challenge
    print("\n1. Open this link in your normal web browser and sign in to Amazon:\n")
    print(str(first.start_url))
    print("\n2. After you sign in, Amazon shows a page that says it can't find the page. That is expected.")
    print("3. Copy the full web address from the address bar and paste it here.\n")
    await first.close()
    pasted = input("Paste the address: ").strip()
    code = parse_qs(urlparse(pasted).query).get("openid.oa2.authorization_code", [None])[0]
    if not code:
        raise SystemExit("That address has no authorization code. Start again and copy the whole address.")
    login = AlexaLogin("amazon.com", email, "", out, False, uuid=uuid,
                       oauth={"code_verifier": verifier, "code_challenge": challenge, "authorization_code": code})
    if not await login.test_loggedin(rebuild_session=True) or not login.refresh_token:
        await login.close()
        raise SystemExit("Amazon did not accept the sign-in. Start again.")
    path = os.path.join(DATA, "alexa_auth.json")
    with open(path, "w") as f:
        json.dump({"uuid": uuid, "email": email, "oauth": {
            "refresh_token": login.refresh_token, "mac_dms": login.mac_dms,
            "code_verifier": verifier, "code_challenge": challenge}}, f)
    os.chmod(path, 0o600)
    await login.close()
    print(f"\nDone. Saved {path}. Start the service with: docker compose up -d")


asyncio.run(main())
