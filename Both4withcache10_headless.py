#!/usr/bin/env python3
"""
Nifty 50 Options Trader — Heikin-Ashi Strategy
================================================
Single-strategy script. No multi-strategy bloat.

TOKEN MANAGEMENT:
  Smart token fallback (from Both4withcache10_headless.py):
    1. Hardcoded token  (HARDCODED_TOKEN, if USE_HARDCODED_TOKEN=True)
    2. Saved token      (upstox_token.txt)
    3. Refresh token    (upstox_refresh_token.txt)
    4. Headless Selenium OAuth  (fully automated — no manual URL pasting)

STRATEGY RECAP:
  1. No trades before 10:15 AM
  2. Wait for the first 1-hour HA candle (9:15–10:15) to close
  3. Determine trend from that 1H HA candle:
       GREEN → bullish bias → look for CE entries only
       RED   → bearish bias → look for PE entries only
  4. On the 15-minute chart, scan for an EARLY REVERSAL pattern:
       Bullish (CE):  RED RED → First GREEN HA + close above prev HA high + small lower shadow
       Bearish (PE):  GREEN GREEN → First RED HA + close below prev HA low + small upper shadow
  5. Exit at SL / target / time (15:15) or end of day

DATA:
  Upstox does NOT natively serve 15min or 1H candles via its v2 API.
  We build them ourselves:
    • 1-min candles  → fetch from /historical-candle/intraday/NSE_INDEX|Nifty 50/1minute
    • Resample       → pandas resample to 15T and 1h

USAGE:
  python nifty_ha_options.py

REQUIREMENTS:
  pip install requests pandas scipy selenium webdriver-manager
"""

import os
import sys
import time
import csv
import json
import pickle
import re
import imaplib
import email
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

# Selenium imports (needed for headless OAuth fallback)
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException
    _SELENIUM_OK = True
except ImportError:
    _SELENIUM_OK = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  (edit these)
# ─────────────────────────────────────────────────────────────────────────────

# ── CREDENTIALS (for headless OAuth + OTP email retrieval) ───────────────────
EMAIL          = os.environ.get("UPSTOX_EMAIL",    "frzkhn155@gmail.com")
EMAIL_PASSWORD = os.environ.get("UPSTOX_PASSWORD", "vdeahogzvpsmfirv")
MOBILE_NUMBER  = os.environ.get("UPSTOX_MOBILE",   "7397408750")
PASSCODE       = os.environ.get("UPSTOX_PASSCODE", "952495")

# ── Upstox OAuth app credentials ─────────────────────────────────────────────
# Get from https://account.upstox.com/developer/apps → your app
UPSTOX_API_KEY      = os.environ.get("UPSTOX_API_KEY",    "ea9b2ade-6720-4a0b-a8a5-6e1710f55844")
UPSTOX_API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "csxmppf5zd")
UPSTOX_REDIRECT_URI = "http://127.0.0.1:8080/"   # must match your app settings

# ── Headless OAuth local server port ─────────────────────────────────────────
HEADLESS_SERVER_PORT      = 8080
UPSTOX_REFRESH_TOKEN_FILE = "upstox_refresh_token.txt"
UPSTOX_TOKEN_FILE         = "upstox_token.txt"
TOKEN_TIMESTAMP_FILE      = "token_timestamp.json"

# ── Hardcoded token option ────────────────────────────────────────────────────
# Paste today's fresh token here to skip the OAuth flow entirely.
# Set USE_HARDCODED_TOKEN=False to always go through the OAuth ladder.
HARDCODED_TOKEN     = os.environ.get("UPSTOX_TOKEN", "")
USE_HARDCODED_TOKEN = True   # True → try HARDCODED_TOKEN first

# ACCESS_TOKEN is set at startup by get_upstox_token() — do not edit directly.
ACCESS_TOKEN = ""

# Upstox instrument keys
NIFTY_INDEX_KEY  = "NSE_INDEX|Nifty 50"   # used to fetch 1-min candles
NIFTY_OPTION_KEY = "NSE_INDEX|Nifty 50"   # used for option chain lookup

BASE_URL = "https://api.upstox.com/v2"

# Trading parameters
ORDER_QUANTITY     = 1          # lots
ORDER_PRODUCT      = "D"        # NRML (D = delivery/carry for options)
STOPLOSS_PCT       = 15.0       # SL as % of option premium
TARGET_MULTIPLIER  = 2.0        # target = risk × multiplier
NO_NEW_ENTRY_AFTER = "15:15"    # no fresh entries after this time
MARKET_CLOSE_TIME  = "15:30"
SCAN_INTERVAL_SECS = 30         # how often the main loop wakes up

# HA reversal detection parameters
# Lower-shadow filter for bullish reversal:  shadow_pct = lower_shadow / candle_range
SMALL_LOWER_SHADOW_MAX = 0.35   # lower shadow ≤ 35% of range  (bullish reversal)
SMALL_UPPER_SHADOW_MAX = 0.35   # upper shadow ≤ 35% of range  (bearish reversal)

# HA trailing-stop buffer
# CE: trail = prev_ha_low  + TRAIL_BUFFER_PCT × candle_range   (stop sits above raw HA low)
# PE: trail = prev_ha_high − TRAIL_BUFFER_PCT × candle_range   (stop sits below raw HA high)
# Set to 0.0 to trail exactly at the HA boundary (original behaviour).
# 0.10 = 10% of the last HA candle's range added as a cushion.
TRAIL_BUFFER_PCT = 0.10

# Enable automated order placement (set False for signal-only mode)
ENABLE_AUTO_TRADING = True

# Logging
LOG_FILE = "nifty_ha_strategy.txt"
CSV_FILE = "nifty_ha_trades.csv"

# Test / debug
DEBUG_MODE = True

# ─────────────────────────────────────────────────────────────────────────────
# DATA AVAILABILITY LAG (fix for 10:15 issue)
# Upstox intraday 1‑minute candles become available about 60‑120 seconds
# after the minute ends. We wait an extra 90 seconds before considering a
# 1‑hour candle "closed".
# ─────────────────────────────────────────────────────────────────────────────
DATA_LAG_SECONDS = 90

# ─────────────────────────────────────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────────────────────────────────────

BIAS            = None   # "BULLISH" | "BEARISH" | None
BIAS_SET_AT     = None   # datetime when the most recent 1H HA candle was evaluated
ACTIVE_POSITION = {}     # tracks the open option position
DAILY_PNL       = 0.0

BIAS_OVERRIDE_ACTIVE  = False  # track if bias has been overridden for the current hour
PROCESSED_BIAS_CANDLES = set() # 1H candle start times already used for bias
BIAS_OVERRIDE_DONE_FOR = set() # candle_close_time values for which override already ran

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT HTTP SESSION
# ─────────────────────────────────────────────────────────────────────────────

_UPSTOX_SESSION       = None   # requests.Session for Upstox API
_UPSTOX_SESSION_TOKEN = ""     # tracks which Bearer token the session was built for

def _get_upstox_session(access_token: str = None) -> requests.Session:
    """
    Return a persistent requests.Session for all Upstox REST calls.
    Re-creates session only when the access_token changes (daily rotation).
    Eliminates DNS + TLS handshake overhead on every request (~200-400 ms saved).
    """
    global _UPSTOX_SESSION, _UPSTOX_SESSION_TOKEN
    token = access_token or ACCESS_TOKEN or ""
    if _UPSTOX_SESSION is None or _UPSTOX_SESSION_TOKEN != token:
        _UPSTOX_SESSION = requests.Session()
        _UPSTOX_SESSION.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        })
        _UPSTOX_SESSION_TOKEN = token
    return _UPSTOX_SESSION

def _headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

def _order_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN MANAGEMENT  (ported from Both4withcache10_headless.py)
# Ladder: hardcoded → saved file → refresh token → headless Selenium OAuth
# ─────────────────────────────────────────────────────────────────────────────

def verify_token(token, verbose=True):
    """Verify Upstox API token validity via JWT pre-check + profile API call."""
    if verbose:
        print("🔍 Verifying API token...")

    # ── JWT expiry pre-check (no network call) ────────────────────────────────
    try:
        import base64, json as _json
        parts = token.split('.')
        if len(parts) == 3:
            payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
            payload = _json.loads(base64.b64decode(payload_b64).decode('utf-8'))
            exp_ts = payload.get('exp')
            if exp_ts:
                exp_dt = datetime.fromtimestamp(exp_ts)
                now = datetime.now()
                if now > exp_dt:
                    print(f"🚨 TOKEN EXPIRED at {exp_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"— IT IS NOW {now.strftime('%H:%M:%S')}")
                    print("   ➡ Set USE_HARDCODED_TOKEN=False to auto-login, or update HARDCODED_TOKEN")
                elif verbose:
                    mins_left = int((exp_dt - now).total_seconds() / 60)
                    print(f"⏱ Token expires at {exp_dt.strftime('%H:%M:%S')} ({mins_left} min remaining)")
    except Exception:
        pass

    # ── Live API check ────────────────────────────────────────────────────────
    url = f"{BASE_URL}/user/profile"
    try:
        resp = _get_upstox_session(token).get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if verbose:
                print("✅ Token is VALID")
                ud = data.get('data', {})
                print(f"   User: {ud.get('user_name', 'N/A')} (ID: {ud.get('user_id', 'N/A')})\n")
            return {'valid': True, 'data': data.get('data', {}), 'message': 'Token is valid'}
        elif resp.status_code == 401:
            if verbose:
                print("❌ Token is INVALID or EXPIRED")
            return {'valid': False, 'message': 'Token invalid or expired', 'status_code': 401}
        else:
            if verbose:
                print(f"⚠️ Unexpected response: {resp.status_code}")
            return {'valid': False, 'message': f'Unexpected status: {resp.status_code}'}
    except requests.exceptions.Timeout:
        return {'valid': False, 'message': 'Request timeout'}
    except Exception as e:
        return {'valid': False, 'message': f'Error: {e}'}


def _refresh_upstox_token() -> str:
    """
    Try to refresh the Upstox access token using a saved refresh token.
    Returns new access_token string, or None if refresh fails / no token saved.
    """
    if not os.path.exists(UPSTOX_REFRESH_TOKEN_FILE):
        return None
    try:
        with open(UPSTOX_REFRESH_TOKEN_FILE, "r") as f:
            refresh_token = f.read().strip()
        if not refresh_token:
            return None
        print("🔄 Attempting token refresh via saved refresh_token…")
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "client_id":     UPSTOX_API_KEY,
                "client_secret": UPSTOX_API_SECRET,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data         = resp.json()
            access_token = data.get("access_token")
            new_refresh  = data.get("refresh_token", refresh_token)
            if access_token:
                with open(UPSTOX_REFRESH_TOKEN_FILE, "w") as f:
                    f.write(new_refresh)
                with open(UPSTOX_TOKEN_FILE, "w") as f:
                    f.write(access_token)
                print("✅ Token refreshed successfully.")
                return access_token
        print(f"⚠️ Token refresh failed ({resp.status_code}): {resp.text[:100]}")
        return None
    except Exception as exc:
        print(f"⚠️ Token refresh error: {exc}")
        return None


class UpstoxLogin:
    """
    Handles Upstox login and headless OAuth token acquisition.
    Ported from Both4withcache10_headless.py — all methods intact.
    """
    def __init__(self, mobile_number: str, email_address: str, email_password: str,
                 passcode: str = None, cookies_file: str = "upstox_cookies.pkl",
                 login_url: str = "https://login.upstox.com",
                 target_url: str = "https://account.upstox.com/developer/apps",
                 max_retries: int = 3):
        self.mobile_number = mobile_number
        self.email_address = email_address
        self.email_password = email_password
        self.passcode = passcode
        self.login_url = login_url
        self.target_url = target_url
        self.cookies_file = cookies_file
        self.driver = None
        self.max_retries = max_retries

    def setup_driver(self, headless: bool = False):
        if not _SELENIUM_OK:
            raise RuntimeError("selenium / webdriver-manager not installed")
        chrome_options = webdriver.ChromeOptions()
        if headless:
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1280,800")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def close(self):
        if self.driver:
            self.driver.quit()
            print("🚪 Browser closed")

    def delete_all_upstox_emails(self):
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com") as mail:
                mail.login(self.email_address, self.email_password)
                mail.select("inbox")
                deleted_count = 0
                for pattern in ['FROM "donotreply@transactions.upstox.com"', 'FROM "upstox"']:
                    try:
                        status, messages = mail.search(None, pattern)
                        if status == "OK" and messages[0]:
                            for eid in messages[0].split():
                                mail.store(eid, '+FLAGS', '\\Deleted')
                                deleted_count += 1
                            mail.expunge()
                    except Exception:
                        pass
                print(f"✅ Deleted {deleted_count} Upstox emails from inbox")
        except Exception as e:
            print(f"⚠️ Error deleting emails: {e}")

    def get_latest_otp_by_uid(self, max_wait: int = 90,
                               otp_request_time: datetime = None) -> str:
        try:
            if otp_request_time is None:
                otp_request_time = datetime.now()
            print(f"⏳ Waiting for NEW OTP email (max {max_wait}s)…")
            start_time = time.time()
            time.sleep(8)
            while time.time() - start_time < max_wait:
                try:
                    with imaplib.IMAP4_SSL("imap.gmail.com") as mail:
                        mail.login(self.email_address, self.email_password)
                        mail.select("inbox")
                        status, messages = mail.search(
                            None, '(UNSEEN FROM "donotreply@transactions.upstox.com")')
                        if status != "OK" or not messages[0]:
                            status, messages = mail.search(None, '(UNSEEN FROM "upstox")')
                        if status != "OK" or not messages[0]:
                            elapsed = int(time.time() - start_time)
                            print(f"⏳ No unread Upstox emails yet… ({elapsed}s)")
                            time.sleep(3)
                            continue
                        email_ids = messages[0].split()
                        for email_id in reversed(email_ids):
                            try:
                                status, msg_data = mail.fetch(email_id, "(RFC822 INTERNALDATE)")
                                if status != "OK":
                                    continue
                                # Timestamp check — skip emails older than request
                                internaldate_match = re.search(
                                    rb'INTERNALDATE "([^"]+)"', msg_data[0][0])
                                if internaldate_match:
                                    internaldate_str = internaldate_match.group(1).decode()
                                    try:
                                        email_received_time = datetime.strptime(
                                            internaldate_str, "%d-%b-%Y %H:%M:%S %z"
                                        ).replace(tzinfo=None)
                                        time_diff = (email_received_time - otp_request_time
                                                     ).total_seconds()
                                        if time_diff < -5:
                                            continue
                                    except Exception:
                                        continue
                                msg = email.message_from_bytes(msg_data[0][1])
                                body = ""
                                if msg.is_multipart():
                                    for part in msg.walk():
                                        if part.get_content_type() in ("text/plain", "text/html"):
                                            body = part.get_payload(decode=True).decode(
                                                "utf-8", errors="replace")
                                            break
                                else:
                                    body = msg.get_payload(decode=True).decode(
                                        "utf-8", errors="replace")
                                body = body.replace("\r", " ").replace("\n", " ")
                                otp_patterns = [
                                    r'OTP\s*(?:is)?\s*[:=]?\s*(\d{6})',
                                    r'one.?time.?password\s*(?:is)?\s*[:=]?\s*(\d{6})',
                                    r'verification.?code\s*(?:is)?\s*[:=]?\s*(\d{6})',
                                    r'code\s*(?:is)?\s*[:=]?\s*(\d{6})',
                                    r'(?:<b>|>)\s*(\d{6})\s*(?:</b>|<)',
                                    r'\b(\d{6})\b(?!.*\d{7,})',
                                ]
                                found_otp = None
                                for pat in otp_patterns:
                                    m = re.search(pat, body, re.IGNORECASE)
                                    if m:
                                        found_otp = m.group(1)
                                        break
                                if found_otp:
                                    mail.store(email_id, '+FLAGS', '\\Seen')
                                    mail.store(email_id, '+FLAGS', '\\Deleted')
                                    mail.expunge()
                                    print(f"✅ OTP EXTRACTED: {found_otp}")
                                    return found_otp
                            except Exception as e:
                                print(f"⚠️ Error processing email: {e}")
                                continue
                except Exception as e:
                    print(f"⚠️ IMAP check failed: {e}")
                time.sleep(3)
            print(f"❌ No OTP received after {max_wait} seconds")
            return None
        except Exception as e:
            print(f"❌ OTP retrieval error: {e}")
            return None

    def perform_oauth_headless(self) -> str:
        """
        Fully automated headless Selenium OAuth flow.
        Spins up a local HTTP server to capture the redirect code, then
        drives Chrome headlessly through mobile OTP → PIN → consent.
        Returns the access_token string or None on failure.
        """
        import urllib.parse as _urlparse

        # ── Local redirect-capture server ─────────────────────────────────────
        _captured_code = [None]
        _server_ready  = threading.Event()

        class _CodeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                code   = parse_qs(parsed.query).get("code", [None])[0]
                if code:
                    _captured_code[0] = code
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h1>Auth successful - you may close this tab.</h1>")
                    threading.Thread(
                        target=lambda: (time.sleep(1), self.server.shutdown()),
                        daemon=True
                    ).start()
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing code")
            def log_message(self, *_):
                pass

        def _run_server():
            try:
                srv = HTTPServer(("127.0.0.1", HEADLESS_SERVER_PORT), _CodeHandler)
                _server_ready.set()
                print(f"✅ Headless redirect server on http://127.0.0.1:{HEADLESS_SERVER_PORT}")
                srv.serve_forever()
            except OSError as exc:
                print(f"❌ Cannot bind port {HEADLESS_SERVER_PORT}: {exc}")
                _server_ready.set()

        threading.Thread(target=_run_server, daemon=True).start()
        if not _server_ready.wait(timeout=5):
            print("❌ Redirect server failed to start.")
            return None

        # ── Headless Chrome login ─────────────────────────────────────────────
        print("\n🤖 HEADLESS OAUTH LOGIN")
        self.setup_driver(headless=True)
        try:
            auth_url = (
                "https://api.upstox.com/v2/login/authorization/dialog"
                f"?response_type=code"
                f"&client_id={UPSTOX_API_KEY}"
                f"&redirect_uri={_urlparse.quote(UPSTOX_REDIRECT_URI, safe='')}"
            )
            print("🌐 Opening login page (headless)…")
            self.driver.get(auth_url)
            time.sleep(2)
            self.delete_all_upstox_emails()

            # Mobile number
            mobile_input = WebDriverWait(self.driver, 10).until(
                EC.visibility_of_element_located((By.ID, "mobileNum"))
            )
            mobile_input.clear()
            mobile_input.send_keys(self.mobile_number)

            # Click "Get OTP"
            get_otp_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(text(), 'Get OTP')]"))
            )
            otp_request_time = datetime.now()
            get_otp_btn.click()
            print("📨 OTP requested.")

            otp = self.get_latest_otp_by_uid(max_wait=120, otp_request_time=otp_request_time)
            if not otp:
                print("❌ OTP not retrieved.")
                return None

            otp_input = WebDriverWait(self.driver, 10).until(
                EC.visibility_of_element_located((By.ID, "otpNum"))
            )
            for digit in str(otp):
                otp_input.send_keys(digit)
                time.sleep(0.1)

            continue_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//button[contains(text(), 'Continue') or contains(text(), 'Verify')]"))
            )
            continue_btn.click()
            print("✅ OTP submitted.")
            time.sleep(3)

            # PIN (optional)
            if self.passcode:
                try:
                    pin_input = WebDriverWait(self.driver, 8).until(
                        EC.visibility_of_element_located((By.ID, "pinCode"))
                    )
                    pin_input.clear()
                    pin_input.send_keys(self.passcode)
                    try:
                        WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.ID, "pinContinueBtn"))
                        ).click()
                    except Exception:
                        for sel in [(By.XPATH, "//button[@type='submit']"),
                                    (By.XPATH, "//button[contains(text(), 'Continue')]")]:
                            try:
                                WebDriverWait(self.driver, 3).until(
                                    EC.element_to_be_clickable(sel)).click()
                                break
                            except Exception:
                                continue
                    print("✅ PIN submitted.")
                    time.sleep(3)
                except Exception as e:
                    print(f"ℹ️ PIN step skipped: {e}")

            # Consent (optional)
            try:
                allow_btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(text(), 'Allow')]"))
                )
                allow_btn.click()
                print("✅ Consent granted.")
                time.sleep(2)
            except Exception:
                pass

            # Wait for redirect code (browser URL or local server)
            print("⏳ Waiting for OAuth redirect…")
            deadline = time.time() + 30
            while time.time() < deadline:
                cur = self.driver.current_url
                if "127.0.0.1" in cur:
                    code = parse_qs(urlparse(cur).query).get("code", [None])[0]
                    if code:
                        _captured_code[0] = code
                        print("✅ Auth code captured from browser URL.")
                        break
                if _captured_code[0]:
                    break
                time.sleep(1)

            if not _captured_code[0]:
                print("❌ No auth code received after 30 s.")
                return None

        except Exception as exc:
            print(f"❌ Headless login error: {exc}")
            return None
        finally:
            self.close()

        # ── Exchange code for access token ────────────────────────────────────
        print("🔄 Exchanging auth code for access token…")
        try:
            resp = requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                headers={"accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "code":          _captured_code[0],
                    "client_id":     UPSTOX_API_KEY,
                    "client_secret": UPSTOX_API_SECRET,
                    "redirect_uri":  UPSTOX_REDIRECT_URI,
                    "grant_type":    "authorization_code",
                },
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"❌ Token exchange failed ({resp.status_code}): {resp.text[:200]}")
                return None
            data          = resp.json()
            access_token  = data.get("access_token")
            refresh_token = data.get("refresh_token", "")
            if not access_token:
                print(f"❌ No access_token in response: {resp.text[:200]}")
                return None
            try:
                with open(UPSTOX_REFRESH_TOKEN_FILE, "w") as rf:
                    rf.write(refresh_token)
                print(f"💾 Refresh token saved to {UPSTOX_REFRESH_TOKEN_FILE}")
            except Exception:
                pass
            print("✅ Headless OAuth complete!")
            return access_token
        except Exception as exc:
            print(f"❌ Token exchange error: {exc}")
            return None


def get_upstox_token() -> str:
    """
    Get Upstox access token with smart fallback:
      1. Hardcoded token  (HARDCODED_TOKEN, if USE_HARDCODED_TOKEN=True)
      2. Saved token      (upstox_token.txt)
      3. Refresh token    (upstox_refresh_token.txt)
      4. Headless Selenium OAuth  (fully automated — no manual URL pasting)
    """
    print("=" * 60)
    print("UPSTOX TOKEN MANAGEMENT")
    print("=" * 60)
    print(f"Mobile: {MOBILE_NUMBER}")
    print(f"Email:  {EMAIL}")
    print("=" * 60 + "\n")

    # STEP 1: Hardcoded token
    if USE_HARDCODED_TOKEN and HARDCODED_TOKEN:
        print("🔑 Step 1: Checking HARDCODED token…")
        validation = verify_token(HARDCODED_TOKEN, verbose=True)
        if validation['valid']:
            print("✅ HARDCODED token is VALID — using it!")
            try:
                with open(UPSTOX_TOKEN_FILE, 'w') as f:
                    f.write(HARDCODED_TOKEN)
            except Exception:
                pass
            return HARDCODED_TOKEN
        else:
            print(f"❌ HARDCODED token invalid: {validation['message']}")
            print("⚠️ Will try other methods…\n")

    # STEP 2: Saved token
    print("🔑 Step 2: Checking SAVED token from file…")
    if os.path.exists(UPSTOX_TOKEN_FILE):
        try:
            with open(UPSTOX_TOKEN_FILE, 'r') as f:
                saved_token = f.read().strip()
            if saved_token:
                validation = verify_token(saved_token, verbose=True)
                if validation['valid']:
                    print("✅ SAVED token is VALID — using it!")
                    return saved_token
                else:
                    print(f"❌ SAVED token invalid: {validation['message']}\n")
            else:
                print("⚠️ Token file is empty")
        except Exception as e:
            print(f"❌ Error reading token file: {e}")
    else:
        print(f"⚠️ No saved token file at {UPSTOX_TOKEN_FILE}")

    # STEP 3: Refresh token
    print("\n🔑 Step 3: Attempting token refresh (no browser needed)…")
    refreshed = _refresh_upstox_token()
    if refreshed:
        validation = verify_token(refreshed, verbose=True)
        if validation['valid']:
            print("✅ Refreshed token is VALID.")
            return refreshed
        else:
            print("❌ Refreshed token failed validation.")

    # STEP 4: Headless Selenium OAuth
    print("\n" + "=" * 60)
    print("🤖 Step 4: HEADLESS SELENIUM OAUTH (fully automated)")
    print("=" * 60)

    if not _SELENIUM_OK:
        print("❌ Selenium not installed. Run:")
        print("   pip install selenium webdriver-manager")
        return None

    if UPSTOX_API_KEY == "YOUR_UPSTOX_API_KEY":
        print("❌ UPSTOX_API_KEY not set.")
        print("   1. Go to https://account.upstox.com/developer/apps")
        print("   2. Create/open an app → Redirect URL: http://127.0.0.1:8080/")
        print("   3. Paste Client ID into UPSTOX_API_KEY and Secret into UPSTOX_API_SECRET")
        return None

    login = UpstoxLogin(
        mobile_number=MOBILE_NUMBER,
        email_address=EMAIL,
        email_password=EMAIL_PASSWORD,
        passcode=PASSCODE,
    )
    new_token = login.perform_oauth_headless()

    if new_token:
        validation = verify_token(new_token, verbose=True)
        if validation['valid']:
            try:
                with open(UPSTOX_TOKEN_FILE, "w") as f:
                    f.write(new_token)
                print(f"💾 Token saved to {UPSTOX_TOKEN_FILE}")
            except Exception:
                pass
            print("✅ Headless token is VALID — bot will start now.")
            return new_token
        else:
            print("❌ Headless token failed validation.")

    print("\n" + "=" * 60)
    print("❌ ALL TOKEN METHODS FAILED")
    print("=" * 60)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# CANDLE FETCHING  (1-minute intraday → resample)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_1min_intraday() -> pd.DataFrame | None:
    """
    Fetch today's 1-minute intraday candles for Nifty 50 index.
    Upstox /historical-candle/intraday/{key}/1minute returns today's bars.
    """
    url = f"{BASE_URL}/historical-candle/intraday/{NIFTY_INDEX_KEY}/1minute"
    try:
        resp = _get_upstox_session().get(url, timeout=20)
        if resp.status_code == 200:
            candles = resp.json().get("data", {}).get("candles", [])
            if not candles:
                return None
            df = pd.DataFrame(candles,
                              columns=["timestamp", "open", "high", "low",
                                       "close", "volume", "oi"])
            df["datetime"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("datetime").reset_index(drop=True)
            return df
        else:
            if DEBUG_MODE:
                print(f"⚠️  1min fetch HTTP {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️  1min fetch error: {e}")
        return None


def resample_to(df: pd.DataFrame, rule: str, offset: str = None) -> pd.DataFrame | None:
    """Resample 1-minute DataFrame to a higher timeframe."""
    if df is None or df.empty:
        return None
    df_idx = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]
    kwargs = {"offset": offset} if offset else {}
    resampled = df_idx.resample(rule, **kwargs).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    resampled = resampled.reset_index()
    return resampled


def get_candles():
    """
    Returns (df_15m, df_1h) — DataFrames of 15-minute and 1-hour candles
    built by resampling today's 1-minute intraday data.
    """
    df1 = fetch_1min_intraday()
    if df1 is None:
        return None, None
    df_15m = resample_to(df1, "15min")
    df_1h  = resample_to(df1, "1h", offset="15min")  # align to 09:15 NSE open
    return df_15m, df_1h

# ─────────────────────────────────────────────────────────────────────────────
# HEIKIN-ASHI CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_ha(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add ha_open, ha_high, ha_low, ha_close, ha_color columns to a copy of df.
    Requires columns: open, high, low, close.

    ha_open is a recursive average: ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
    This is an IIR filter with coefficient 0.5, computed in O(n) via scipy.signal.lfilter
    instead of a slow Python loop.
    """
    from scipy.signal import lfilter

    df = df.copy().reset_index(drop=True)

    # ── ha_close: vectorised, no loop needed ─────────────────────────────────
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4

    # ── ha_open: IIR recurrence  ha_open[i] = 0.5*ha_open[i-1] + 0.5*ha_close[i-1]
    # Rewrite as:  ha_open[i] = 0.5*(ha_open[i-1] + ha_close[i-1])
    # In lfilter terms (Direct Form II):
    #   b = [0, 0.5],  a = [1, -0.5]
    # Initial condition set so ha_open[0] = (open[0] + close[0]) / 2
    seed = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    # zi represents the filter memory; zi[0] seeds the output to `seed`
    zi = np.array([seed])
    ha_open_arr, _ = lfilter([0, 0.5], [1, -0.5], ha_close.to_numpy(), zi=zi)
    ha_open = pd.Series(ha_open_arr, dtype=float)

    # ── ha_high / ha_low: vectorised max/min ─────────────────────────────────
    ha_high = np.maximum(df["high"].to_numpy(),
               np.maximum(ha_open.to_numpy(), ha_close.to_numpy()))
    ha_low  = np.minimum(df["low"].to_numpy(),
               np.minimum(ha_open.to_numpy(), ha_close.to_numpy()))

    df["ha_open"]  = ha_open.values
    df["ha_high"]  = ha_high
    df["ha_low"]   = ha_low
    df["ha_close"] = ha_close.values
    df["ha_color"] = np.where(df["ha_close"] >= df["ha_open"], "green", "red")
    return df

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: 1-HOUR BIAS DETECTION  (called after 10:15)
# ─────────────────────────────────────────────────────────────────────────────

def determine_1h_bias(df_1h: pd.DataFrame) -> str | None:
    """
    Look at the LATEST fully-closed 1-hour HA candle.
    "Fully closed" now means the candle's end time is at least DATA_LAG_SECONDS
    before the current time – this accounts for Upstox's data delivery latency.

    Updates bias every hour as new 1H candles close.
    Returns "BULLISH", "BEARISH", or None if no new closed candle is available.

    Uses PROCESSED_BIAS_CANDLES to avoid re-evaluating the same candle twice.
    When the most recent closed candle has already been processed, returns the
    current BIAS unchanged (so the caller knows nothing has changed).
    """
    global BIAS, PROCESSED_BIAS_CANDLES

    if df_1h is None or df_1h.empty:
        return None

    ha = compute_ha(df_1h)

    # Walk backwards through candles to find the most recent one that has
    # fully closed AND its data is guaranteed to be available.
    # We require: now >= candle_end + DATA_LAG_SECONDS
    now = datetime.now()
    last_candle = None
    candle_start_found = None
    candle_end_found   = None
    for _, row in ha.iloc[::-1].iterrows():
        candle_start = pd.Timestamp(row["datetime"])
        candle_end   = candle_start + pd.Timedelta(hours=1)
        # --- FIX: add data availability lag ---------------------------------
        if now >= candle_end + timedelta(seconds=DATA_LAG_SECONDS):
            last_candle        = row
            candle_start_found = candle_start
            candle_end_found   = candle_end
            break

    if last_candle is None:
        # No 1H candle has fully closed + lag yet
        if DEBUG_MODE:
            print("   1H HA: No fully-closed (with data lag) 1H candle available yet")
        return None

    # Have we already processed this candle?
    if candle_start_found in PROCESSED_BIAS_CANDLES:
        return BIAS   # keep existing bias, no update needed

    color = last_candle["ha_color"]
    bias  = "BULLISH" if color == "green" else "BEARISH"

    PROCESSED_BIAS_CANDLES.add(candle_start_found)

    print(f"   📊 1H HA candle {candle_start_found.strftime('%H:%M')}–{candle_end_found.strftime('%H:%M')} "
          f"-> {color.upper()} -> new bias: {bias}")
    print(f"      HA O:{last_candle['ha_open']:.1f}  H:{last_candle['ha_high']:.1f}  "
          f"L:{last_candle['ha_low']:.1f}  C:{last_candle['ha_close']:.1f}")

    return bias


def check_bias_override(df_15m):
    """
    At 15 min after each 1H candle closes (10:30, 11:30, 12:30, ...), check
    whether the new hour opened with a gap-down below the previous hour's low
    and has already broken that level.  If so, override BULLISH -> BEARISH.

    Runs in the :30-:35 minute window of any hour (i.e. the first 15m bar of
    the new hour has fully printed).  Uses BIAS_OVERRIDE_DONE_FOR to fire at
    most once per hour boundary, regardless of how many loop iterations land
    in that window.
    """
    global BIAS, BIAS_OVERRIDE_ACTIVE, BIAS_OVERRIDE_DONE_FOR

    if BIAS != "BULLISH":
        return False

    now = datetime.now()
    # Only act in the :30-:35 minute window of any hour
    if now.minute < 30 or now.minute > 35:
        return False

    # The 1H candle that just closed started at :15 of the current hour
    candle_close_time = now.replace(minute=15, second=0, microsecond=0)

    if candle_close_time in BIAS_OVERRIDE_DONE_FOR:
        return False   # already handled this hour boundary

    # Mark this hour boundary as checked (whether or not override fires)
    BIAS_OVERRIDE_DONE_FOR.add(candle_close_time)

    if df_15m is None or len(df_15m) < 2:
        return False

    # Candles straddling the :15 boundary of this hour:
    #   last_before = last 15m bar of the previous hour  (ends at :15)
    #   first_after = first 15m bar of the new hour      (starts at :15)
    before = df_15m[df_15m["datetime"] < candle_close_time]
    after  = df_15m[df_15m["datetime"] >= candle_close_time]

    if len(before) == 0 or len(after) == 0:
        return False

    last_before = before.iloc[-1]
    first_after = after.iloc[0]

    prev_hour_low = last_before["low"]
    new_hour_open = first_after["open"]

    # Condition 1: gap-down - new hour opened below previous hour's low
    if new_hour_open >= prev_hour_low:
        return False

    # Condition 2: confirmation - price actually traded below that low
    current_low = after["low"].min()
    if current_low < prev_hour_low:
        gap_pct = (new_hour_open - prev_hour_low) / prev_hour_low * 100
        print(f"\n⚠️  BIAS OVERRIDE TRIGGERED at {now.strftime('%H:%M:%S')}!")
        print(f"   Previous hour low: {prev_hour_low:.1f}, new hour open: {new_hour_open:.1f} (gap {gap_pct:.2f}%)")
        print(f"   Price broke below previous hour low -> Switching bias to BEARISH")
        BIAS = "BEARISH"
        BIAS_OVERRIDE_ACTIVE = True
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: 15-MINUTE EARLY REVERSAL DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_early_reversal_ce(ha: pd.DataFrame) -> dict | None:
    """
    Bullish reversal → CE entry.

    Pattern:
      • Previous candle:  RED (ha_color == 'red')
      • Current candle:   GREEN (first green after reds)
      • Close above previous HA high  → momentum confirmation
      • Small lower shadow             → buyers stepped in cleanly
    """
    if len(ha) < 2:
        return None

    last = ha.iloc[-1]
    prev = ha.iloc[-2]

    if not (prev["ha_color"] == "red" and last["ha_color"] == "green"):
        return None

    # Close above previous HA high
    if last["ha_close"] <= prev["ha_high"]:
        if DEBUG_MODE:
            print(f"   CE filter: close {last['ha_close']:.1f} ≤ prev HA high {prev['ha_high']:.1f}")
        return None

    # Small lower shadow check
    candle_range = last["ha_high"] - last["ha_low"]
    if candle_range < 0.001:
        return None
    lower_shadow = last["ha_open"] - last["ha_low"]   # HA open > HA low for green candle
    lower_shadow_pct = lower_shadow / candle_range
    if lower_shadow_pct > SMALL_LOWER_SHADOW_MAX:
        if DEBUG_MODE:
            print(f"   CE filter: lower shadow {lower_shadow_pct:.2%} > max {SMALL_LOWER_SHADOW_MAX:.2%}")
        return None

    return {
        "signal":         "CE",
        "candle_time":    last["datetime"],
        "ha_close":       last["ha_close"],
        "prev_ha_high":   prev["ha_high"],
        "lower_shadow_pct": lower_shadow_pct,
    }


def detect_early_reversal_pe(ha: pd.DataFrame) -> dict | None:
    """
    Bearish reversal → PE entry.

    Pattern:
      • Previous candle:  GREEN
      • Current candle:   RED (first red after greens)
      • Close below previous HA low   → momentum confirmation
      • Small upper shadow             → sellers stepped in cleanly
    """
    if len(ha) < 2:
        return None

    last = ha.iloc[-1]
    prev = ha.iloc[-2]

    if not (prev["ha_color"] == "green" and last["ha_color"] == "red"):
        return None

    # Close below previous HA low
    if last["ha_close"] >= prev["ha_low"]:
        if DEBUG_MODE:
            print(f"   PE filter: close {last['ha_close']:.1f} ≥ prev HA low {prev['ha_low']:.1f}")
        return None

    # Small upper shadow check
    candle_range = last["ha_high"] - last["ha_low"]
    if candle_range < 0.001:
        return None
    upper_shadow = last["ha_high"] - last["ha_open"]   # HA high > HA open for red candle
    upper_shadow_pct = upper_shadow / candle_range
    if upper_shadow_pct > SMALL_UPPER_SHADOW_MAX:
        if DEBUG_MODE:
            print(f"   PE filter: upper shadow {upper_shadow_pct:.2%} > max {SMALL_UPPER_SHADOW_MAX:.2%}")
        return None

    return {
        "signal":          "PE",
        "candle_time":     last["datetime"],
        "ha_close":        last["ha_close"],
        "prev_ha_low":     prev["ha_low"],
        "upper_shadow_pct": upper_shadow_pct,
    }


def bullish_structure_ok(df: pd.DataFrame) -> bool:
    """
    Confirm 15m price structure is making higher highs before a CE entry.
    Requires the last 3 swing highs to be rising (or flat → rising).
    Prevents entries during fake HA reversals inside a downtrend.

    Uses the last 4 raw (non-HA) highs:
      highs[-3] → highs[-2] → highs[-1]  must be non-decreasing then rising.
    """
    if len(df) < 4:
        return False
    highs = df["high"].tail(4).values
    ok = highs[-1] > highs[-2] and highs[-2] >= highs[-3]
    if not ok and DEBUG_MODE:
        print(f"   ⛔ CE blocked — lower highs: {highs[-3]:.1f} → {highs[-2]:.1f} → {highs[-1]:.1f}")
    return ok


def bearish_structure_ok(df: pd.DataFrame) -> bool:
    """
    Confirm 15m price structure is making lower lows before a PE entry.
    Requires the last 3 swing lows to be falling (or flat → falling).
    Prevents entries during fake HA reversals inside an uptrend.

    Uses the last 4 raw (non-HA) lows:
      lows[-3] → lows[-2] → lows[-1]  must be non-increasing then falling.
    """
    if len(df) < 4:
        return False
    lows = df["low"].tail(4).values
    ok = lows[-1] < lows[-2] and lows[-2] <= lows[-3]
    if not ok and DEBUG_MODE:
        print(f"   ⛔ PE blocked — higher lows: {lows[-3]:.1f} → {lows[-2]:.1f} → {lows[-1]:.1f}")
    return ok


def scan_15m_for_entry(df_15m: pd.DataFrame) -> dict | None:
    """
    Run HA computation on 15-minute candles and check for the correct
    reversal pattern based on the current BIAS.
    Returns a signal dict or None.
    """
    global BIAS

    if df_15m is None or df_15m.empty or BIAS is None:
        return None

    # Only use candles after 10:15 (when bias was set)
    df_15m = df_15m[df_15m["datetime"] >= pd.Timestamp(BIAS_SET_AT.replace(second=0, microsecond=0))]
    if len(df_15m) < 2:
        return None

    ha = compute_ha(df_15m)

    if BIAS == "BULLISH":
        if not bullish_structure_ok(df_15m):
            return None
        return detect_early_reversal_ce(ha)
    elif BIAS == "BEARISH":
        if not bearish_structure_ok(df_15m):
            return None
        return detect_early_reversal_pe(ha)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# OPTION CHAIN & ORDER PLACEMENT
# ─────────────────────────────────────────────────────────────────────────────

def get_nifty_spot() -> float | None:
    """Fetch Nifty 50 LTP."""
    url = f"{BASE_URL}/market-quote/ltp"
    try:
        resp = _get_upstox_session().get(
            url, params={"instrument_key": NIFTY_INDEX_KEY}, timeout=15)
        if resp.status_code == 200:
            inner = resp.json().get("data", {})
            for k, v in inner.items():
                ltp = v.get("last_price")
                if ltp:
                    return float(ltp)
        if DEBUG_MODE:
            print(f"⚠️  Spot fetch HTTP {resp.status_code}")
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️  Spot fetch error: {e}")
    return None


def get_nifty_option_chain(option_type: str) -> list:
    """
    Fetch Nifty option contracts (CE or PE) sorted by expiry.
    Returns list of contract dicts.
    """
    url = f"{BASE_URL}/option/contract"
    try:
        resp = _get_upstox_session().get(
            url, params={"instrument_key": NIFTY_OPTION_KEY}, timeout=20)
        if resp.status_code != 200:
            if DEBUG_MODE:
                print(f"⚠️  Option chain HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        contracts = resp.json().get("data", [])
        today = datetime.now().date()
        result = []
        for c in contracts:
            if c.get("instrument_type") != option_type:
                continue
            try:
                exp = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
                if exp <= today:  # skip expired (including today for safety)
                    continue
                c["_expiry_date"] = exp
                result.append(c)
            except Exception:
                continue
        result.sort(key=lambda x: x["_expiry_date"])
        return result
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️  Option chain error: {e}")
        return []


def select_atm_contract(contracts: list, spot: float) -> dict | None:
    """Select the nearest-expiry ATM contract."""
    if not contracts:
        return None
    # All contracts are already sorted by expiry; get nearest expiry
    nearest_expiry = contracts[0]["_expiry_date"]
    nearest = [c for c in contracts if c["_expiry_date"] == nearest_expiry]
    # Find ATM
    atm = min(nearest, key=lambda c: abs(c["strike_price"] - spot))
    return atm


def get_ltp(instrument_key: str) -> float | None:
    """Fetch LTP for any instrument."""
    url = f"{BASE_URL}/market-quote/ltp"
    try:
        resp = _get_upstox_session().get(
            url, params={"instrument_key": instrument_key}, timeout=15)
        if resp.status_code == 200:
            inner = resp.json().get("data", {})
            for k, v in inner.items():
                ltp = v.get("last_price")
                if ltp:
                    return float(ltp)
    except Exception as e:
        if DEBUG_MODE:
            print(f"⚠️  LTP error for {instrument_key}: {e}")
    return None


def place_order(instrument_key: str, qty: int, txn_type: str,
                order_type: str, price: float = 0,
                trigger_price: float = 0) -> str | None:
    """Place a buy/sell order. Returns order_id or None."""
    url = f"{BASE_URL}/order/place"
    payload = {
        "quantity":           qty,
        "product":            ORDER_PRODUCT,
        "validity":           "DAY",
        "price":              price,
        "tag":                "HA_BOT",
        "instrument_token":   instrument_key,
        "order_type":         order_type.upper(),
        "transaction_type":   txn_type.upper(),
        "disclosed_quantity": 0,
        "trigger_price":      trigger_price,
        "is_amo":             False,
    }
    try:
        # Orders need Content-Type: application/json — use a dedicated call
        sess = _get_upstox_session()
        resp = sess.post(url, headers={"Content-Type": "application/json"},
                         json=payload, timeout=15)
        print(f"   📤 Order response ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return data.get("data", {}).get("order_id")
    except Exception as e:
        print(f"   ❌ Order error: {e}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def execute_entry(signal: dict):
    """
    Given a confirmed entry signal, select option contract,
    fetch premium, and place BUY + SL orders.
    """
    global ACTIVE_POSITION

    option_type = signal["signal"]  # "CE" or "PE"
    print(f"\n{'='*70}")
    print(f"🎯 ENTRY SIGNAL: {option_type} at {datetime.now().strftime('%H:%M:%S')}")
    print(f"   HA close: {signal['ha_close']:.1f} | Bias: {BIAS}")
    if option_type == "CE":
        print(f"   Prev HA high: {signal['prev_ha_high']:.1f} | "
              f"Lower shadow: {signal['lower_shadow_pct']:.1%}")
    else:
        print(f"   Prev HA low:  {signal['prev_ha_low']:.1f} | "
              f"Upper shadow: {signal['upper_shadow_pct']:.1%}")
    print(f"{'='*70}")

    spot = get_nifty_spot()
    if not spot:
        print("   ❌ Cannot fetch Nifty spot — aborting entry")
        return

    contracts = get_nifty_option_chain(option_type)
    contract  = select_atm_contract(contracts, spot)
    if not contract:
        print(f"   ❌ No {option_type} contracts found — aborting entry")
        return

    premium = get_ltp(contract["instrument_key"])
    if not premium or premium <= 0:
        print(f"   ❌ Cannot fetch premium for {contract.get('trading_symbol')} — aborting")
        return

    lot_size  = contract.get("lot_size", 50)
    total_qty = lot_size * ORDER_QUANTITY
    limit_px  = round(premium * 1.02, 2)   # 2% slippage buffer

    print(f"   Option:  {contract.get('trading_symbol')}")
    print(f"   Strike:  {contract['strike_price']} | Expiry: {contract['expiry']}")
    print(f"   Spot:    {spot:.1f} | Premium LTP: {premium:.2f}")
    print(f"   Lot:     {lot_size} × {ORDER_QUANTITY} = {total_qty} qty")
    print(f"   Limit:   ₹{limit_px:.2f}")

    if not ENABLE_AUTO_TRADING:
        print("   ℹ️  Auto-trading DISABLED — signal logged only")
        _log_signal(signal, contract, premium, spot, order_id="SIGNAL_ONLY")
        return

    order_id = place_order(
        instrument_key=contract["instrument_key"],
        qty=total_qty,
        txn_type="BUY",
        order_type="LIMIT",
        price=limit_px,
    )

    if not order_id:
        print("   ❌ BUY order failed — aborting")
        return

    print(f"   ✅ BUY order placed: {order_id}")

    # Stop-loss order
    sl_trigger = round(premium * (1 - STOPLOSS_PCT / 100), 2)
    sl_limit   = round(sl_trigger * 0.99, 2)
    sl_id = place_order(
        instrument_key=contract["instrument_key"],
        qty=total_qty,
        txn_type="SELL",
        order_type="SL_LIMIT",
        price=sl_limit,
        trigger_price=sl_trigger,
    )
    if sl_id:
        print(f"   🛡️  SL order placed: {sl_id} (trigger ₹{sl_trigger:.2f})")

    # Track position
    ACTIVE_POSITION = {
        "order_id":       order_id,
        "sl_order_id":    sl_id,
        "instrument_key": contract["instrument_key"],
        "trading_symbol": contract.get("trading_symbol"),
        "option_type":    option_type,
        "entry_price":    premium,
        "quantity":       total_qty,
        "sl_trigger":     sl_trigger,
        "target":         round(premium * (1 + (STOPLOSS_PCT / 100) * TARGET_MULTIPLIER), 2),
        "entry_time":     datetime.now(),
        "signal":         signal,
    }

    _log_signal(signal, contract, premium, spot, order_id=order_id)
    print(f"   Target: ₹{ACTIVE_POSITION['target']:.2f} | SL: ₹{sl_trigger:.2f}")
    print(f"{'='*70}\n")

# ─────────────────────────────────────────────────────────────────────────────
# EXIT MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _compute_ha_trail(df_15m: pd.DataFrame) -> float | None:
    """
    Compute the current HA-based trailing stop for the active position.

    CE  →  trail = prev_ha_low  + TRAIL_BUFFER_PCT × ha_range
           (stop trails ABOVE the HA low; tighter than raw HA low)

    PE  →  trail = prev_ha_high − TRAIL_BUFFER_PCT × ha_range
           (stop trails BELOW the HA high; tighter than raw HA high)

    Uses the last completed 15m HA candle (iloc[-2]) so we never react to
    a still-forming bar.  Returns None if data is insufficient.
    """
    if df_15m is None or len(df_15m) < 2:
        return None

    ha = compute_ha(df_15m)
    if len(ha) < 2:
        return None

    prev = ha.iloc[-2]        # last *closed* candle
    ha_range = prev["ha_high"] - prev["ha_low"]
    buffer   = TRAIL_BUFFER_PCT * ha_range
    opt_type = ACTIVE_POSITION.get("option_type")

    if opt_type == "CE":
        trail = prev["ha_low"] + buffer
    elif opt_type == "PE":
        trail = prev["ha_high"] - buffer
    else:
        return None

    return round(trail, 2)


def check_exit():
    """
    Monitor ACTIVE_POSITION for SL / target / EOD exit.

    Trailing stop logic (HA-based, updates every scan):
      CE:  stop = prev_ha_low  + TRAIL_BUFFER_PCT × ha_range  (rises as trend climbs)
      PE:  stop = prev_ha_high − TRAIL_BUFFER_PCT × ha_range  (falls as trend drops)

    The trail only ever moves in the favourable direction (ratchet).
    If the trail is tighter than the initial fixed SL at entry, the tighter
    level wins — protecting profit once the trade moves in favour.
    """
    global ACTIVE_POSITION, DAILY_PNL

    if not ACTIVE_POSITION:
        return

    current_px = get_ltp(ACTIVE_POSITION["instrument_key"])
    if not current_px:
        return

    entry_px  = ACTIVE_POSITION["entry_price"]
    target    = ACTIVE_POSITION["target"]
    qty       = ACTIVE_POSITION["quantity"]
    pnl       = (current_px - entry_px) * qty
    pnl_pct   = (current_px - entry_px) / entry_px * 100
    now_str   = datetime.now().strftime("%H:%M")
    opt_type  = ACTIVE_POSITION["option_type"]
    exit_rsn  = None

    # ── HA trailing stop: update ratchet ──────────────────────────────────────
    df_15m, _ = get_candles()
    ha_trail  = _compute_ha_trail(df_15m)

    if ha_trail is not None:
        prev_sl = ACTIVE_POSITION["sl_trigger"]
        if opt_type == "CE":
            # Trail rises — only update when new level is higher (locks in more profit)
            if ha_trail > prev_sl:
                ACTIVE_POSITION["sl_trigger"] = ha_trail
                if DEBUG_MODE:
                    print(f"   🔼 CE trail raised: ₹{prev_sl:.2f} → ₹{ha_trail:.2f} "
                          f"(buffer {TRAIL_BUFFER_PCT:.0%} of HA range)")
        elif opt_type == "PE":
            # Trail falls — only update when new level is lower (locks in more profit)
            if ha_trail < prev_sl:
                ACTIVE_POSITION["sl_trigger"] = ha_trail
                if DEBUG_MODE:
                    print(f"   🔽 PE trail lowered: ₹{prev_sl:.2f} → ₹{ha_trail:.2f} "
                          f"(buffer {TRAIL_BUFFER_PCT:.0%} of HA range)")

    sl = ACTIVE_POSITION["sl_trigger"]

    # ── Exit conditions ───────────────────────────────────────────────────────
    if opt_type == "CE" and current_px <= sl:
        exit_rsn = "TRAIL_SL_HIT"
    elif opt_type == "PE" and current_px >= sl:
        exit_rsn = "TRAIL_SL_HIT"
    elif current_px >= target and opt_type == "CE":
        exit_rsn = "TARGET_HIT"
    elif current_px <= target and opt_type == "PE":
        exit_rsn = "TARGET_HIT"
    elif now_str >= NO_NEW_ENTRY_AFTER:
        exit_rsn = "TIME_EXIT"

    if not exit_rsn:
        if DEBUG_MODE:
            print(f"   📊 {ACTIVE_POSITION['trading_symbol']} | "
                  f"LTP: ₹{current_px:.2f} | Trail SL: ₹{sl:.2f} | "
                  f"P&L: ₹{pnl:+.0f} ({pnl_pct:+.1f}%)", flush=True)
        return

    # Execute exit
    print(f"\n{'='*70}")
    print(f"🔚 EXIT: {exit_rsn} | {ACTIVE_POSITION['trading_symbol']}")
    print(f"   Entry: ₹{entry_px:.2f} | Exit: ₹{current_px:.2f}")
    print(f"   P&L:   ₹{pnl:+.0f} ({pnl_pct:+.1f}%)")

    if ENABLE_AUTO_TRADING:
        # Cancel SL order first if it exists
        if ACTIVE_POSITION.get("sl_order_id"):
            _cancel_order(ACTIVE_POSITION["sl_order_id"])

        exit_id = place_order(
            instrument_key=ACTIVE_POSITION["instrument_key"],
            qty=ACTIVE_POSITION["quantity"],
            txn_type="SELL",
            order_type="MARKET",
            price=0,
        )
        if exit_id:
            print(f"   ✅ Exit order: {exit_id}")
        else:
            print("   ⚠️  Exit order failed — place manually!")

    DAILY_PNL += pnl
    _log_exit(ACTIVE_POSITION, current_px, exit_rsn, pnl, pnl_pct)
    ACTIVE_POSITION = {}
    print(f"   Daily P&L so far: ₹{DAILY_PNL:+.0f}")
    print(f"{'='*70}\n")


def force_market_exit():
    """
    Unconditional market-order exit used at EOD.
    Unlike check_exit(), this always sells regardless of price vs SL/target.
    """
    global ACTIVE_POSITION, DAILY_PNL

    if not ACTIVE_POSITION:
        return

    current_px = get_ltp(ACTIVE_POSITION["instrument_key"])
    entry_px   = ACTIVE_POSITION["entry_price"]
    qty        = ACTIVE_POSITION["quantity"]
    pnl        = ((current_px - entry_px) * qty) if current_px else 0.0
    pnl_pct    = ((current_px - entry_px) / entry_px * 100) if current_px else 0.0

    print(f"\n{'='*70}")
    print(f"⏰ EOD FORCE-EXIT | {ACTIVE_POSITION['trading_symbol']}")
    if current_px:
        print(f"   Entry: ₹{entry_px:.2f} | LTP: ₹{current_px:.2f}")
        print(f"   P&L:   ₹{pnl:+.0f} ({pnl_pct:+.1f}%)")
    else:
        print(f"   Entry: ₹{entry_px:.2f} | LTP: unavailable — selling at market")

    if ENABLE_AUTO_TRADING:
        # Cancel any pending SL order first
        if ACTIVE_POSITION.get("sl_order_id"):
            _cancel_order(ACTIVE_POSITION["sl_order_id"])

        exit_id = place_order(
            instrument_key=ACTIVE_POSITION["instrument_key"],
            qty=qty,
            txn_type="SELL",
            order_type="MARKET",
            price=0,
        )
        if exit_id:
            print(f"   ✅ Force-exit order: {exit_id}")
        else:
            print("   ⚠️  Force-exit order FAILED — close manually NOW!")
    else:
        print("   ℹ️  Auto-trading DISABLED — log only")

    DAILY_PNL += pnl
    _log_exit(ACTIVE_POSITION, current_px or entry_px, "EOD_FORCE_EXIT", pnl, pnl_pct)
    ACTIVE_POSITION = {}
    print(f"   Daily P&L: ₹{DAILY_PNL:+.0f}")
    print(f"{'='*70}\n")


def _cancel_order(order_id: str):
    url = f"{BASE_URL}/order/cancel"
    try:
        resp = _get_upstox_session().delete(
            url, headers={"Content-Type": "application/json"},
            json={"order_id": order_id}, timeout=10)
        if DEBUG_MODE:
            print(f"   🗑️  Cancel {order_id}: {resp.status_code}")
    except Exception as e:
        if DEBUG_MODE:
            print(f"   ⚠️  Cancel error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

# Single definition of CSV columns — used by both _log_signal and _csv_append.
# If you add a column, update here and nowhere else.
CSV_HEADERS = [
    "timestamp", "signal", "bias", "ha_close",
    "premium", "symbol", "strike", "expiry", "spot", "order_id",
]

def _log_signal(signal, contract, premium, spot, order_id):
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        signal["signal"],
        BIAS,
        signal["ha_close"],
        premium,
        contract.get("trading_symbol"),
        contract["strike_price"],
        contract["expiry"],
        spot,
        order_id,
    ]
    _csv_append(row)
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'='*70}\n")
        f.write(f"ENTRY: {datetime.now()} | {signal['signal']} | Bias: {BIAS}\n")
        f.write(f"  Option: {contract.get('trading_symbol')} | Premium: ₹{premium:.2f}\n")
        f.write(f"  Order ID: {order_id}\n")


def _log_exit(pos, exit_px, reason, pnl, pnl_pct):
    with open(LOG_FILE, "a") as f:
        f.write(f"EXIT: {datetime.now()} | {reason}\n")
        f.write(f"  Entry: ₹{pos['entry_price']:.2f} | Exit: ₹{exit_px:.2f}\n")
        f.write(f"  P&L:   ₹{pnl:+.0f} ({pnl_pct:+.1f}%)\n")
        f.write(f"{'='*70}\n")


def _csv_append(row):
    """Append one data row to the trade CSV. Writes header automatically on first call."""
    write_header = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(CSV_HEADERS)
        w.writerow(row)

# ─────────────────────────────────────────────────────────────────────────────
# MARKET HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return "09:15" <= t < MARKET_CLOSE_TIME


def current_hhmm() -> str:
    return datetime.now().strftime("%H:%M")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def banner():
    print("\n" + "=" * 70)
    print("  NIFTY 50 OPTIONS TRADER — HEIKIN-ASHI STRATEGY")
    print("=" * 70)
    print(f"  Mode:     {'LIVE TRADING' if ENABLE_AUTO_TRADING else 'SIGNAL ONLY'}")
    print(f"  Entry:    After 10:15 AM | No new entries after {NO_NEW_ENTRY_AFTER}")
    print(f"  SL:       {STOPLOSS_PCT}% of premium | Target: {TARGET_MULTIPLIER}× risk")
    print(f"  Pattern:  RED RED → GREEN HA (CE)  /  GREEN GREEN → RED HA (PE)")
    print(f"  Data:     1-min intraday resampled to 15min + 1H")
    print(f"  Data lag: {DATA_LAG_SECONDS}s (to ensure 1H candles are final)")
    print("=" * 70 + "\n")


def main():
    global BIAS, BIAS_SET_AT, ACTIVE_POSITION, BIAS_OVERRIDE_ACTIVE, \
           PROCESSED_BIAS_CANDLES, BIAS_OVERRIDE_DONE_FOR, ACCESS_TOKEN

    # ── STEP 0: Acquire Upstox token ──────────────────────────────────────────
    token = get_upstox_token()
    if not token:
        print("\n❌ CRITICAL: Could not acquire a valid Upstox token. Exiting.")
        print("   Check credentials / OAuth app settings and re-run.")
        sys.exit(1)
    ACCESS_TOKEN = token
    # Re-initialise the persistent session now that we have a real token
    _get_upstox_session(ACCESS_TOKEN)

    banner()

    scan_count = 0

    while True:
        scan_count += 1
        now = datetime.now()
        t   = current_hhmm()

        # -- Market-closed guard (longer sleep, then restart loop) -------------
        if not is_market_open():
            print(f"\U0001f4a4 Market closed ({now.strftime('%H:%M:%S')}) \u2014 waiting...", flush=True)
            time.sleep(60)
            continue

        # -- Fetch candles (always, to keep data fresh) ------------------------
        df_15m, df_1h = get_candles()

        print(f"\U0001f504 Scan #{scan_count} | {now.strftime('%H:%M:%S')} | "
              f"Bias: {BIAS or 'Not set'} | "
              f"15m bars: {len(df_15m) if df_15m is not None else 0} | "
              f"1H bars: {len(df_1h) if df_1h is not None else 0}",
              flush=True)

        # -- STEP 1: No trades before 10:15 ------------------------------------
        if t < "10:15":
            print("   \u23f3 Waiting for 10:15 AM (first 1H candle to close)...")

        # -- STEP 3: EOD cleanup (force-exit, reset state) ---------------------
        elif t >= MARKET_CLOSE_TIME:
            if ACTIVE_POSITION:
                force_market_exit()
            print(f"\n\U0001f4ca Day complete | Daily P&L: \u20b9{DAILY_PNL:+.0f}")
            print("   Restarting state for tomorrow...")
            BIAS, BIAS_SET_AT = None, None
            PROCESSED_BIAS_CANDLES.clear()
            BIAS_OVERRIDE_DONE_FOR.clear()
            ACTIVE_POSITION = {}
            BIAS_OVERRIDE_ACTIVE = False
            time.sleep(300)
            continue

        # -- STEP 2+: Active trading window (10:15 <= t < 15:30) ---------------
        else:
            # 2a. Update bias from the latest fully-closed 1H candle.
            #     determine_1h_bias() is idempotent — safe to call every tick.
            if df_1h is not None and not df_1h.empty:
                new_bias = determine_1h_bias(df_1h)
                if new_bias is not None and new_bias != BIAS:
                    print(f"   \U0001f4ca Bias updated: {BIAS or 'None'} -> {new_bias} at {now.strftime('%H:%M:%S')}")
                    BIAS = new_bias
                    BIAS_SET_AT = now
                    BIAS_OVERRIDE_ACTIVE = False   # reset override flag for new hour
                elif BIAS is None and new_bias is not None:
                    BIAS = new_bias
                    BIAS_SET_AT = now
                    BIAS_OVERRIDE_ACTIVE = False
                    print(f"   \u2705 Bias set: {BIAS} at {now.strftime('%H:%M:%S')}")
                elif new_bias is None and BIAS is None:
                    print("   \u26a0\ufe0f  No fully-closed 1H candle yet \u2014 will retry")
            else:
                if BIAS is None:
                    print("   \u26a0\ufe0f  No 1H data yet \u2014 will retry")

            # 2b. Gap-down override: runs at :30-:35 of each hour after 10:30.
            #     check_bias_override() is self-throttled via BIAS_OVERRIDE_DONE_FOR.
            if current_hhmm() >= "10:30":
                check_bias_override(df_15m)

            # -- STEP 4: Monitor active position exits -------------------------
            if ACTIVE_POSITION:
                check_exit()

            # -- STEP 5: Scan for entry ----------------------------------------
            # Allow fresh entry whenever there is no open position and bias is set,
            # regardless of how many trades have already been taken today.
            if not ACTIVE_POSITION and t < NO_NEW_ENTRY_AFTER and BIAS is not None:
                signal = scan_15m_for_entry(df_15m)
                if signal:
                    print(f"\n   \U0001f514 Reversal pattern detected: {signal['signal']}")
                    execute_entry(signal)
                elif DEBUG_MODE:
                    # Show last 2 HA colours for transparency
                    if df_15m is not None and len(df_15m) >= 2:
                        ha = compute_ha(
                            df_15m[df_15m["datetime"] >= pd.Timestamp(BIAS_SET_AT)]
                        ) if BIAS_SET_AT else compute_ha(df_15m)
                        if len(ha) >= 2:
                            c1 = ha.iloc[-2]["ha_color"]
                            c2 = ha.iloc[-1]["ha_color"]
                            print(f"   15m HA: [{c1}] -> [{c2}] | No pattern yet")

        # -- Single sleep point for every normal path --------------------------
        time.sleep(SCAN_INTERVAL_SECS)

if __name__ == "__main__":
    main()