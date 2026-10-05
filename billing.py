#!/usr/bin/env python3
"""Report Salad portal credit balances per organization.

The public API (api.salad.com/api/public) has no billing operation, so this
logs into the web portal's backend (portal-api.salad.com/api/portal) with the
account credentials stored in the encrypted vault and reads each org's
"Current Credit Amount":

    POST /users/login                          {"email", "password"} -> 204 + scid cookie
    GET  /organizations/{org}/billing-profile/credits-balance         -> {"amount": N} (cents)

Output is a per-org table in USD plus an EUR column converted at the live ECB
reference rate (frankfurter.app); if the FX lookup fails, only USD is shown.

Usage:
  python3 billing.py                 # passphrase from $PORTAL_VAULT_PASS, or prompt
  PORTAL_VAULT_PASS='...' python3 billing.py

The vault (portal_vault.gpg, gitignored) is GPG symmetric AES256 and holds the
login plus one billing URL per org; every org in it gets a balance line. If no
vault exists yet, first run prompts for the portal email/password and the org
slugs, verifies the login against the portal, then encrypts and saves them.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API_BASE = "https://portal-api.salad.com/api/portal"


def load_vault(vault_path: str, passphrase: str) -> dict:
    """Decrypt the GPG vault and parse its JSON payload."""
    proc = subprocess.run(
        ["gpg", "--batch", "--quiet", "--decrypt", "--passphrase", passphrase, vault_path],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"vault decrypt failed (wrong passphrase?): {proc.stderr.decode().strip()}")
    return json.loads(proc.stdout)


def create_vault(vault_path: str, passphrase: str) -> dict:
    """First-run setup: prompt for portal credentials + orgs, verify login, encrypt."""
    print(f"no vault at {vault_path} — creating one (credentials are verified before saving)")
    email = input("portal email: ").strip()
    password = getpass.getpass("portal password: ")
    orgs = [o.strip() for o in input("org slugs (comma-separated): ").split(",") if o.strip()]
    if not email or not password or not orgs:
        raise SystemExit("email, password and at least one org slug are required")

    # Verify the credentials before encrypting them away.
    login(email, password)

    vault = {
        "user": email,
        "password": password,
        "billing_urls": {org: f"https://portal.salad.com/organizations/{org}/billing" for org in orgs},
        "scrape_target": "Current Credit Amount",
    }
    proc = subprocess.run(
        ["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256",
         "--passphrase", passphrase, "--output", vault_path],
        input=json.dumps(vault).encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"vault encrypt failed: {proc.stderr.decode().strip()}")
    os.chmod(vault_path, 0o600)
    print(f"vault created at {vault_path}")
    return vault


def _request(url: str, data: bytes | None = None, cookie: str | None = None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    # Cloudflare 1010-bans the default Python-urllib user agent; send a browser one.
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def login(email: str, password: str) -> str:
    """Portal login; returns the scid session cookie value."""
    status, headers, body = _request(
        f"{API_BASE}/users/login", data=json.dumps({"email": email, "password": password}).encode()
    )
    if status != 204:
        raise SystemExit(f"login failed: HTTP {status} {body.decode(errors='replace')[:200]}")
    set_cookie = headers.get("Set-Cookie", "")
    cookie = set_cookie.split(";")[0]
    if not cookie.startswith("scid="):
        raise SystemExit(f"login returned no scid cookie: {set_cookie[:120]}")
    return cookie


def usd_to_eur_rate() -> float | None:
    """Live USD->EUR from the ECB reference rate (frankfurter.app); None on failure."""
    try:
        status, _, body = _request("https://api.frankfurter.app/latest?from=USD&to=EUR")
        if status != 200:
            return None
        return float(json.loads(body)["rates"]["EUR"])
    except (ValueError, KeyError):
        return None


def credits_balance(org: str, cookie: str) -> float | None:
    status, _, body = _request(f"{API_BASE}/organizations/{org}/billing-profile/credits-balance", cookie=cookie)
    if status != 200:
        print(f"{'ORG':<24} (HTTP {status})")
        return None
    # API returns cents ({"amount": 188} = $1.88).
    try:
        return float(json.loads(body)["amount"]) / 100
    except (KeyError, ValueError):
        print(f"{'ORG':<24} (unexpected payload: {body.decode()[:80]})")
        return None


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    vault_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_vault.gpg")
    passphrase = os.environ.get("PORTAL_VAULT_PASS") or getpass.getpass("vault passphrase: ")
    if os.path.exists(vault_path):
        vault = load_vault(vault_path, passphrase)
    else:
        vault = create_vault(vault_path, passphrase)

    cookie = login(vault["user"], vault["password"])
    orgs = list((vault.get("billing_urls") or {}).keys())
    if not orgs:
        print("no billing_urls in vault", file=sys.stderr)
        return 2

    rate = usd_to_eur_rate()
    if rate is None:
        print("warning: FX rate unavailable, EUR column omitted", file=sys.stderr)

    rows: list[tuple[str, float]] = []
    total = 0.0
    for org in orgs:
        amount = credits_balance(org, cookie)
        if amount is None:
            continue
        total += amount
        rows.append((org, amount))

    if not rows:
        return 1
    if rate is not None:
        print(f"{'ORG':<24} {'USD':>8} {'EUR':>8}")
        for org, amount in rows:
            print(f"{org:<24} {amount:>8.2f} {amount * rate:>8.2f}")
        print("-" * 44)
        print(f"{'TOTAL':<24} {total:>8.2f} {total * rate:>8.2f}   @ {rate:.4f}")
    else:
        for org, amount in rows:
            print(f"{org:<24} ${amount:.2f}")
        print("-" * 36)
        print(f"{'TOTAL':<24} ${total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
