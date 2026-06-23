import httpx

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens"

# Tokens on the static watchlist are known-good — skip the API call
TRUSTED_MINTS = {
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
    "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk",    # WEN
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",   # MEW
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",  # PYTH
    "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",   # BOME
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",  # POPCAT — verified $3.3M liq
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",   # PENGU  — verified $3.7M liq
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",   # TRUMP  — verified $34.7M liq
}

# RugCheck scores go from 0 (clean) to ~10000 (extremely risky).
# Reject anything above this — well above normal for established tokens.
RISK_SCORE_LIMIT = 5000


async def is_safe_token(client: httpx.AsyncClient, mint: str) -> tuple[bool, str]:
    """
    Check token safety via RugCheck.xyz.
    Returns (safe, reason). Defaults to True on API failure so the bot
    isn't blocked by a third-party outage.
    """
    if mint in TRUSTED_MINTS:
        return True, "trusted watchlist token"

    try:
        r = await client.get(
            f"{RUGCHECK_URL}/{mint}/report/summary",
            timeout=3,
        )
        if r.status_code == 404:
            return True, "not in RugCheck database — allowing"
        r.raise_for_status()
        data = r.json()

        if data.get("rugged"):
            return False, "confirmed rug pull"

        score = data.get("score", 0)
        if score > RISK_SCORE_LIMIT:
            return False, f"risk score {score} > {RISK_SCORE_LIMIT}"

        return True, f"score {score}"
    except Exception as e:
        return True, f"safety check skipped ({e})"
