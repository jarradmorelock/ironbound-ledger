import json
import os
import time
import requests

LEAGUE_ID = os.environ["SLEEPER_LEAGUE_ID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = "state.json"

def _get(url):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def post(msg):
    r = requests.post(WEBHOOK, json={"content": msg}, timeout=20)
    r.raise_for_status()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_seen_ms": 0}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_transactions(season):
    return _get(
        f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/transactions/{season}"
    )

def league_season() -> str:
    league = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}")
    return str(league.get("season"))

def main():
    state = load_state()

    # Probe a small window of rounds; 0 is commonly used in offseason/preseason
    rounds_to_check = [0, 1, 2, 3]
    found = []
    found_rounds = {}

    for r in rounds_to_check:
        txs = fetch_transactions(r)
        found_rounds[r] = len(txs)
        found.extend(txs)

    newest = state["last_seen_ms"]
    for t in found:
        ts = t.get("status_updated") or t.get("created") or 0
        newest = max(newest, int(ts))

    state["last_seen_ms"] = newest
    save_state(state)

    post(
        "✅ **Ironbound Ledger online (round-based)**\n"
        f"Rounds checked: {rounds_to_check}\n"
        f"Counts: {found_rounds}\n"
        f"Total transactions: {len(found)}\n"
        f"last_seen_ms set to: {newest}"
    )



if __name__ == "__main__":
    main()
