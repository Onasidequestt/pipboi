# DATBOI — FAQ

Honest answers to the questions newcomers actually ask. If yours isn't here, open
the repo in [Claude Code](https://claude.com/claude-code) and ask, drop it in the
[Telegram](https://t.me/datpipboi), or [open a question issue](../../issues/new/choose).

---

### Is this safe to run? Will it steal my money?

DATBOI runs **entirely on your machine** — there is no account, no server, no
telemetry, nothing phones home. Your API keys and wallet live in local files that
are git-ignored and never transmitted. The bot can only ever spend the SOL in the
**one wallet you point it at**, and it has no feature to transfer funds to anyone
else. The whole repo is open — read every line, or have your own Claude read it for
you. Details: **[SECURITY.md](SECURITY.md)**.

### Will I make money?

**No promises — and honestly, probably not by default.** A durable trading edge is
*hard*, and on free data it's frequently unproven. DATBOI is built to be honest
about exactly that: it ships verdict tools (`kill_criterion.py`, `prove_edge.py`)
whose whole job is to tell you when an edge **isn't** real, and its live dashboard
will literally show you when it's down. Treat this as a tool to **learn, research,
and paper-trade** autonomous trading — not as income. You can lose everything you
put in.

### Do you (the creators) control or hold my funds?

**No. Never.** You run your own copy, on your own wallet, with your own keys. We
can't see your funds, move them, or touch them. There is no custody, no deposit
address, no "send us SOL to start." If anyone ever tells you to send SOL somewhere
to use DATBOI, **it's a scam.**

### How much SOL should I start with?

A **small** amount you're 100% okay losing — e.g. 0.1–0.5 SOL. The bot sizes every
trade as a small fraction of your live balance, so a small wallet = small trades.
Start by just **watching** the dashboard before you scale anything.

### Is $DATBOI a good investment? Is it a rug?

`$DATBOI` is a **community token / flag** for an open-source project — **not a fund,
not a managed account, and not a promise of returns.** It carries no claim on any
profits or assets. Like *any* memecoin it is high-risk and can go to zero — only
buy what you can afford to lose, and do your own research. The **real product is the
open-source bot**; the token is just the community around it. You never need the
token to run the bot.

### What's the official contract? How do I avoid fakes?

There is exactly **one** official $DATBOI:

```
59E8gLC4Zuh1zdfiTsoV7zMgoMBtHcnyGkfb58s4pump
```

Always verify it against the [official site](https://onasidequestt.github.io/datpipboi/)
and [pump.fun page](https://pump.fun/coin/59E8gLC4Zuh1zdfiTsoV7zMgoMBtHcnyGkfb58s4pump).
We will **never** DM you first, run a "presale", or ask you to send SOL anywhere.
Launches attract impersonator tokens and scam DMs — assume anything that isn't the
address above is fake.

### What do I need to run it?

- **Python 3.9+** (`python3 --version`)
- **macOS or Linux** (Windows: use [WSL](https://learn.microsoft.com/windows/wsl/install))
- A free **[Helius](https://dashboard.helius.dev)** API key
- A small amount of **SOL** (the dashboard can generate a wallet for you)

### It's not trading / "0 candidates" — is it broken?

Usually not. The bot is **selective by design**, and free data sources rate-limit,
so quiet stretches are normal — it self-recovers. Check the `SCOUT` tab for the
reasons it's skipping. If something's actually wrong, run **`./datboi doctor`** — it
checks your Python, dependencies, keys, wallet, and port and tells you the one
thing to fix.

### How do I get help?

1. **`./datboi doctor`** — the built-in checkup.
2. **Your own Claude** — open the repo in Claude Code and ask; `CLAUDE.md` makes it
   a co-pilot for this exact codebase.
3. **[Telegram](https://t.me/datpipboi)** or **[a GitHub issue](../../issues/new/choose)**.

### Is this affiliated with Anthropic or Solana?

No. DATBOI is an independent, open-source community project. It's *built to be
operated with* Claude and *trades on* Solana, but it is **not affiliated with or
endorsed by** Anthropic or Solana.
