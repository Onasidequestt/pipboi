import os
from pathlib import Path

BOT_ID   = int(os.getenv("BOT_ID", "1"))
DATA_DIR = Path(f"bots/bot{BOT_ID}")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Prestige tier — determines which cycle each bot starts on.
# Tier 1 (bots 1-3): cycle 1 → ◎2.0 milestone, ◎1.0 payout
# Tier 2 (bots 4-6): cycle 2 → ◎3.0 milestone, ◎2.0 payout
BOT_TIER = 1 if BOT_ID <= 3 else 2
