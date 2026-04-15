"""
╔══════════════════════════════════════════════════════════════════════════════╗
║        REAL-TIME AI TRADING ASSISTANT  —  Multi-Key Failover                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os as _os
import threading
import time
import json
import requests as _req
from datetime import datetime
from typing import Optional, Callable, Dict, Any

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

GROQ_KEYS = [k for k in [
    _os.environ.get("GROQ_API_KEY", ""),
    _os.environ.get("GROQ_API_KEY_2", ""),   # optional second key for failover
] if k and "YOUR_GROQ" not in k]

NVIDIA_API_KEY = _os.environ.get("NVIDIA_API_KEY", "YOUR_NVIDIA_API_KEY_HERE")
AI_ENABLED = True

# -- Models --
GROQ_MODEL   = "llama-3.3-70b-versatile"
NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"

# -- Behaviour --
AI_SCAN_INTERVAL_SECONDS    = 35
AI_MAX_TOKENS               = 400
AI_TEMPERATURE              = 0.2
AI_AUTO_EXIT_ENABLED        = True
AI_MIN_HOLD_MINUTES         = 15
AI_EXIT_PROFIT_ONLY         = False
AI_LOSS_EXIT_THRESHOLD_PCT  = 5.0
AI_SILENT_WHEN_NO_POSITIONS = True

# -- Soft AI Exit Settings --
AI_SOFT_EXIT_ENABLED   = True
AI_TRAIL_TRIGGER_PCT   = 15.0
AI_TRAIL_TIGHT_PCT     = 3.0
AI_FORCE_EXIT_PCT      = 25.0

# -- Failover --
GROQ_RETRY_SECONDS  = 300
_GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
_NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# -- State Tracking --
_ai_thread:         Optional[threading.Thread] = None
_ai_call_count:     int = 0
_groq_call_count:   int = 0
_nvidia_call_count: int = 0
_current_groq_key_idx: int = 0
_using_nvidia:         bool = False
_groq_failed_at:       Optional[datetime] = None

_bot_globals_getter: Optional[Callable[[], Dict[str, Any]]] = None
_trader_getter:      Optional[Callable[[], Any]] = None

_SYSTEM_PROMPT = """You are a real-time trading assistant for an Upstox F&O bot.
Analyse the snapshot and decide for each open position: HOLD, WATCH, or EXIT.
Rules: EXIT only if Heikin-Ashi colour flip AND Klinger confirms reversal.
Minimum hold: 15m (unless loss >5% or profit >25%).
Output format:
SUMMARY: [text]
POSITIONS: [SYMBOL]: [ACTION] [REASON]
END."""

# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_market_context(trader) -> Dict[str, Any]:
    context = {}
    if not trader:
        return context
    try:
        # Replace these keys with your actual NSE index instrument keys if different
        nifty = trader.get_ltp("NSE_INDEX|Nifty 50")
        bnifty = trader.get_ltp("NSE_INDEX|Nifty Bank")
        if nifty:
            context["nifty"] = nifty
        if bnifty:
            context["bank_nifty"] = bnifty
    except Exception:
        pass
    return context

def _build_snapshot(globals_dict: Dict[str, Any], trader=None) -> Dict[str, Any]:
    snapshot = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "market_context": _fetch_market_context(trader),
        "positions": [],
        "recent_trades": [],
        "daily_pnl": globals_dict.get("DAILY_PNL", 0.0),
        "order_counts": {
            "r3_s3": globals_dict.get("DAILY_ORDER_COUNT", 0),
            "box": globals_dict.get("BOX_ORDER_COUNT", 0),
            "range": globals_dict.get("RANGE_ORDER_COUNT", 0),
            "gap": globals_dict.get("GAP_ORDER_COUNT", 0),
            "fast": globals_dict.get("FAST_TRADE_ORDER_COUNT", 0),
            "orb": globals_dict.get("ORB_ORDER_COUNT", 0),
        }
    }

    active = globals_dict.get("ACTIVE_POSITIONS", {})
    for pos_id, pos in active.items():
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", entry)
        pnl_pct = ((current - entry) / entry * 100) if entry else 0
        snapshot["positions"].append({
            "symbol": pos.get("symbol"),
            "strategy": pos.get("strategy"),
            "direction": pos.get("option_type", "CE"),
            "entry": round(entry, 2),
            "current": round(current, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_time_min": round((datetime.now() - pos.get("timestamp", datetime.now())).total_seconds() / 60, 1),
            "klinger_confirmed": pos.get("klinger_confirmed", False)
        })

    closed = globals_dict.get("CLOSED_POSITIONS", [])
    for trade in closed[-5:]:
        snapshot["recent_trades"].append({
            "symbol": trade.get("symbol"),
            "pnl_pct": round(trade.get("pnl_percent", 0), 2),
            "exit_reason": trade.get("exit_reason")
        })

    return snapshot

def _parse_response(response: str) -> Dict[str, str]:
    decisions = {}
    lines = response.split("\n")
    in_positions = False
    for line in lines:
        if "POSITIONS:" in line:
            in_positions = True
            continue
        if in_positions and line.strip() and not line.startswith("WATCHLIST:"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                sym = parts[0].strip().split()[-1]
                content = parts[1].strip()
                if "EXIT" in content.upper():
                    decisions[sym] = "EXIT"
                elif "WATCH" in content.upper():
                    decisions[sym] = "WATCH"
                else:
                    decisions[sym] = "HOLD"
        elif in_positions and line.strip() == "":
            in_positions = False
    return decisions

def _print_ai_response(response: str):
    print("\n" + "="*100)
    print("🤖 AI ASSISTANT ANALYSIS")
    print("="*100)
    print(response)
    print("="*100 + "\n")

# ══════════════════════════════════════════════════════════════════════════════
# AI PROVIDER ROUTER
# ══════════════════════════════════════════════════════════════════════════════

def _call_ai(snapshot: dict) -> Optional[str]:
    global _using_nvidia, _groq_failed_at, _ai_call_count, _current_groq_key_idx
    global _groq_call_count, _nvidia_call_count

    # Check for Groq recovery
    if _using_nvidia and _groq_failed_at:
        if (datetime.now() - _groq_failed_at).total_seconds() >= GROQ_RETRY_SECONDS:
            _using_nvidia = False

    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(snapshot)}
        ],
        "max_tokens": AI_MAX_TOKENS,
        "temperature": AI_TEMPERATURE
    }

    # 1. Try Groq pool (rotate keys)
    if not _using_nvidia:
        for _ in range(len(GROQ_KEYS)):
            key = GROQ_KEYS[_current_groq_key_idx]
            if not key or "YOUR_GROQ" in key:
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)
                continue
            try:
                p = payload.copy()
                p["model"] = GROQ_MODEL
                resp = _req.post(_GROQ_URL, headers={"Authorization": f"Bearer {key}"}, json=p, timeout=12)
                if resp.status_code == 200:
                    _groq_call_count += 1
                    _ai_call_count += 1
                    return resp.json()["choices"][0]["message"]["content"].strip()
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)
            except Exception:
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)

        # All Groq keys failed
        _using_nvidia = True
        _groq_failed_at = datetime.now()

    # 2. Fallback to NVIDIA
    try:
        p = payload.copy()
        p["model"] = NVIDIA_MODEL
        resp = _req.post(_NVIDIA_URL, headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"}, json=p, timeout=20)
        if resp.status_code == 200:
            _nvidia_call_count += 1
            _ai_call_count += 1
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        pass

    return None

# ══════════════════════════════════════════════════════════════════════════════
# EXIT ENFORCEMENT (with soft trailing)
# ══════════════════════════════════════════════════════════════════════════════

def _enforce_exits(decisions: Dict[str, str], trader, globals_dict: Dict[str, Any]):
    active = globals_dict.get("ACTIVE_POSITIONS", {})
    for sym, action in decisions.items():
        if action != "EXIT":
            continue

        # Find position
        pos_id = None
        pos_data = None
        for pid, p in active.items():
            if p.get("symbol") == sym:
                pos_id = pid
                pos_data = p
                break
        if not pos_data:
            continue

        # Check minimum hold time
        entry_time = pos_data.get("timestamp", datetime.now())
        hold_min = (datetime.now() - entry_time).total_seconds() / 60
        pnl_pct = pos_data.get("pnl_percent", 0)

        if hold_min < AI_MIN_HOLD_MINUTES:
            if not (pnl_pct <= -AI_LOSS_EXIT_THRESHOLD_PCT or pnl_pct >= AI_FORCE_EXIT_PCT):
                continue

        # Soft exit trailing logic
        if AI_SOFT_EXIT_ENABLED and pnl_pct > AI_TRAIL_TRIGGER_PCT:
            peak = pos_data.get("ai_peak_pnl", 0.0)
            if pnl_pct > peak:
                pos_data["ai_peak_pnl"] = pnl_pct
                peak = pnl_pct
            if (peak - pnl_pct) < AI_TRAIL_TIGHT_PCT:
                continue   # not enough drop from peak

        # Execute exit
        print(f"🤖 AI auto‑exiting {sym} (PnL: {pnl_pct:.1f}%)")
        option_key = pos_data.get("instrument_key")
        if option_key:
            ltp = trader.get_ltp(option_key)
            if ltp:
                exit_func = globals_dict.get("exit_position")
                if exit_func:
                    exit_func(trader, pos_id, pos_data, ltp, "AI_RECOMMENDED_EXIT")
                else:
                    print("⚠️ exit_position function not found in globals")
            else:
                print(f"⚠️ Could not fetch LTP for {sym}")
        else:
            print(f"⚠️ No instrument key for {sym}")

# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND THREAD
# ══════════════════════════════════════════════════════════════════════════════

def _ai_loop():
    while AI_ENABLED:
        try:
            if _bot_globals_getter is None or _trader_getter is None:
                time.sleep(5)
                continue

            globals_dict = _bot_globals_getter()
            trader = _trader_getter()

            active = globals_dict.get("ACTIVE_POSITIONS", {})
            if AI_SILENT_WHEN_NO_POSITIONS and not active:
                time.sleep(AI_SCAN_INTERVAL_SECONDS)
                continue

            snapshot = _build_snapshot(globals_dict, trader)
            response = _call_ai(snapshot)
            if response:
                _print_ai_response(response)
                decisions = _parse_response(response)
                if AI_AUTO_EXIT_ENABLED and trader:
                    _enforce_exits(decisions, trader, globals_dict)
            else:
                print("⚠️ AI assistant unavailable (no response)")

        except Exception as e:
            print(f"⚠️ AI loop error: {e}")

        time.sleep(AI_SCAN_INTERVAL_SECONDS)

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE (called by main bot)
# ══════════════════════════════════════════════════════════════════════════════

def start_ai_assistant(bot_globals_getter: Callable[[], Dict[str, Any]],
                       trader_getter: Callable[[], Any]) -> None:
    global _ai_thread, _bot_globals_getter, _trader_getter
    if not AI_ENABLED:
        print("🤖 AI Assistant disabled by configuration.")
        return
    if _ai_thread and _ai_thread.is_alive():
        print("🤖 AI Assistant already running.")
        return

    _bot_globals_getter = bot_globals_getter
    _trader_getter = trader_getter
    _ai_thread = threading.Thread(target=_ai_loop, daemon=True)
    _ai_thread.start()
    print("🤖 AI Assistant started (background thread).")

def ai_status() -> str:
    if not AI_ENABLED:
        return "🤖 AI Assistant: DISABLED"
    status = f"🤖 AI Assistant: Groq ({len(GROQ_KEYS)} keys) + NVIDIA fallback"
    if _using_nvidia:
        status += " | Currently using NVIDIA (Groq failed)"
    else:
        status += " | Active: Groq"
    status += f" | Calls: {_ai_call_count} (Groq: {_groq_call_count}, NVIDIA: {_nvidia_call_count})"
    return status

if __name__ == "__main__":
    print("AI assistant module – import and call start_ai_assistant()")