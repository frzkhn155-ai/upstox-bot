# Upstox Shared Token Management

This guide explains how to use the shared token system across your three Upstox trading bot repositories.

## Overview

Instead of each bot generating its own Upstox token, you now have:
- **One token generated per day** - saved to `upstox_token.txt`
- **Shared across all three repos** - `Final`, `Final-ORB-Only`, `upstox-bot`
- **Automatic caching** - reused throughout the day without regeneration
- **Auto-refresh** - refreshes automatically if expired

## Files Added

1. **`token_manager.py`** - Core token management module
   - Handles token generation, validation, and caching
   - Can be imported by other scripts
   - Includes CLI commands for manual operations

2. **`init_token.py`** - Token initialization script
   - Run this FIRST at the start of your trading day
   - Generates or retrieves cached token
   - Shows token status

3. **`TOKEN_SETUP.md`** - This documentation file

## Setup Instructions

### Step 1: Set Environment Variables

Set your Upstox credentials (you only need to do this once):

**On Linux/Mac:**
```bash
export UPSTOX_API_KEY="your_api_key_here"
export UPSTOX_API_SECRET="your_api_secret_here"
```

**On Windows (PowerShell):**
```powershell
$env:UPSTOX_API_KEY="your_api_key_here"
$env:UPSTOX_API_SECRET="your_api_secret_here"
```

**Alternative: Create config file** (if you prefer not to use env vars)
Create `upstox_config.json` in the repository root:
```json
{
  "api_key": "your_api_key_here",
  "api_secret": "your_api_secret_here"
}
```

### Step 2: Generate Token (Run ONCE Daily)

```bash
python init_token.py
```

This will:
- Generate a new token if none exists
- Return cached token if still valid (within 24 hours)
- Show token status and expiry time

Expected output:
```
============================================================
Upstox Token Initialization
============================================================

[1/2] Retrieving token...
✓ Token ready: eyJhbGciOiJIUzI1NiIs...

[2/2] Token status:
  token_exists: True
  generated_at: 2026-05-24T10:30:45.123456
  expiry: 2026-05-25T10:30:45.123456
  valid: True
  hours_remaining: 23.5

============================================================
✓ Token ready! Your bots can now run.
  All three repositories will share this token.
============================================================
```

### Step 3: Update Your Bot Scripts

In each of your three repositories, update the main script to use the shared token:

**Before (in `Both4withcache10_headless.py`):**
```python
# Old way - generates token every run
client = upstox_client.Client(...)
# ... token generation code ...
```

**After:**
```python
# New way - uses shared cached token
from token_manager import get_token

# At the start of your main function/script:
access_token = get_token()

# Then use it:
client = upstox_client.Client(access_token=access_token, ...)
```

## Daily Workflow

1. **Start of trading day (once):**
   ```bash
   python init_token.py
   ```

2. **Run your bots normally:**
   ```bash
   python Both4withcache10_headless.py  # Final repo
   python Both4withcache10_headless.py  # Final-ORB-Only repo
   # etc.
   ```

All three bots will automatically use the cached token without regenerating it.

## Token Manager CLI Commands

You can also use `token_manager.py` directly for manual operations:

```bash
# Generate a fresh token (ignores cache)
python token_manager.py generate

# Get current token (from cache or generate)
python token_manager.py get

# Show token status
python token_manager.py status

# Check if refresh is needed
python token_manager.py refresh

# Clear cached token (for testing)
python token_manager.py clear
```

## Troubleshooting

### "Credentials not found" error

**Solution:** Make sure environment variables are set:
```bash
echo $UPSTOX_API_KEY  # Should show your key
```

Or create `upstox_config.json`:
```json
{
  "api_key": "your_key",
  "api_secret": "your_secret"
}
```

### Token is not being shared between repos

**Check that:**
1. All three repos have `token_manager.py` and `init_token.py`
2. All three repos have the same credentials set
3. Token was generated in first repo before running other repos
4. Token file location is the same in all repos

### Token expired error

**Solution:** The token expires after 24 hours. Simply run:
```bash
python init_token.py
```

Or add this to your bot startup script:
```python
from token_manager import refresh_token_if_needed
access_token = refresh_token_if_needed()
```

## Files Generated

When you run `init_token.py`, these files are created:
- `upstox_token.txt` - Contains the access token
- `upstox_token_metadata.json` - Contains generation time, expiry, etc.

**Keep these files in your repo root directory.**

## Security Notes

- **Do not commit credentials** to GitHub
  - Add to `.gitignore`: credentials, config files with keys
  - Use environment variables instead

- **Token file:** The `upstox_token.txt` is safe to commit (it's just a token, not credentials)
  - But keep it private if possible

- **Expiry:** Tokens automatically expire after 24 hours for security

## Summary

| Aspect | Before | After |
|--------|--------|-------|
| Tokens generated per day | 3 (one per bot) | 1 (shared) |
| Token generation time | ~3s per run | ~3s per day |
| Cache mechanism | None | Automatic |
| Token refresh | Manual | Automatic |
| Setup effort | Per bot | Once daily |

---

**Questions?** Check `token_manager.py` source code for more details.
