import json
import os
import sys
import time
from typing import Dict, Any, List, Tuple
import requests

LEAGUE_ID = os.environ["SLEEPER_LEAGUE_ID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

# If you want to hard-set season, set SEASON="2026" in repo secrets/vars or here.
SEASON = os.environ.get("SLEEPER_SEASON")  # optional

STATE_FILE = "state.json"


def _get(url: str) -> Any:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def _post_discord(content: str) -> None:
    r = requests.post(WEBHOOK, json={"content": content}, timeout=20)
    r.raise_for_status()


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"last_seen_ms": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def current_season() -> str:
    if SEASON:
        return SEASON
    # Sleeper exposes current NFL state; we use "season" from it.
    state = _get("https://api.sleeper.app/v1/state/nfl")
    return str(state["season"])


def roster_name_map(league_id: str) -> Dict[int, str]:
    users = _get(f"https://api.sleeper.app/v1/league/{league_id}/users")
    rosters = _get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")

    user_id_to_name = {}
    for u in users:
        # display_name is most readable; fall back to username
        user_id_to_name[u["user_id"]] = u.get("display_name") or u.get("username") or u["user_id"]

    roster_id_to_name = {}
    for r in rosters:
        rid = int(r["roster_id"])
        owner = r.get("owner_id")
        roster_id_to_name[rid] = user_id_to_name.get(owner, f"Roster {rid}")

    return roster_id_to_name


def format_picks(draft_picks: List[Dict[str, Any]], roster_names: Dict[int, str]) -> List[str]:
    out = []
    for p in draft_picks or []:
        season = p.get("season")
        rnd = p.get("round")
        orig = p.get("roster_id")  # original owner roster_id
        orig_name = roster_names.get(int(orig), f"Roster {orig}") if orig is not None else "Unknown"
        out.append(f"{season} R{rnd} (orig {orig_name})")
    return out


def group_assets_by_receiver(tx: Dict[str, Any]) -> Dict[int, Dict[str, List[str]]]:
    """
    Returns: {roster_id: {"adds":[player_ids], "drops":[player_ids], "picks":[...]} }
    Note: player IDs are Sleeper IDs. (Optional upgrade: map IDs -> names.)
    """
    assets: Dict[int, Dict[str, List[str]]] = {}

    def ensure(rid: int) -> Dict[str, List[str]]:
        if rid not in assets:
            assets[rid] = {"adds": [], "drops": [], "picks": []}
        return assets[rid]

    adds = tx.get("adds") or {}
    drops = tx.get("drops") or {}
    # adds/drops map: player_id -> roster_id (int)
    for player_id, rid in adds.items():
        ensure(int(rid))["adds"].append(str(player_id))
    for player_id, rid in drops.items():
        ensure(int(rid))["drops"].append(str(player_id))

    # draft_picks are explicit objects; in a trade they represent items moved.
    # Sleeper includes a "roster_id" field representing original roster of the pick.
    # The receiving side isn't directly labeled, so we just list picks in the summary (below).
    return assets


def tx_timestamp_ms(tx: Dict[str, Any]) -> int:
    # Sleeper provides 'status_updated' (ms) in transactions; fall back to 'created' if needed.
    return int(tx.get("status_updated") or tx.get("created") or 0)


def main() -> int:
    season = current_season()
    roster_names = roster_name_map(LEAGUE_ID)
    state = load_state()
    last_seen = int(state.get("last_seen_ms", 0))

    txs = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/transactions/{season}")
    # Transactions are usually newest-first. We'll sort ascending to post in chronological order.
    txs_sorted = sorted(txs, key=tx_timestamp_ms)

    new_txs = [t for t in txs_sorted if tx_timestamp_ms(t) > last_seen]
    if not new_txs:
        print("No new transactions.")
        return 0

    for t in new_txs:
        ttype = t.get("type")
        ts_ms = tx_timestamp_ms(t)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_ms / 1000))

        if ttype == "trade":
            # Basic trade receipt. (Player IDs shown; picks formatted.)
            draft_picks = format_picks(t.get("draft_picks") or [], roster_names)
            msg_lines = [
                "📜 **TRADE FINALIZED**",
                f"*{when} UTC*",
            ]

            # Try to infer participating rosters from adds/drops mapping (not perfect but useful)
            assets = group_assets_by_receiver(t)
            if assets:
                msg_lines.append("")
                msg_lines.append("**Assets by receiving roster:**")
                for rid, bucket in assets.items():
                    rname = roster_names.get(int(rid), f"Roster {rid}")
                    parts = []
                    if bucket["adds"]:
                        parts.append("adds: " + ", ".join(bucket["adds"]))
                    if bucket["drops"]:
                        parts.append("drops: " + ", ".join(bucket["drops"]))
                    msg_lines.append(f"- **{rname}** → " + (" | ".join(parts) if parts else "—"))

            if draft_picks:
                msg_lines.append("")
                msg_lines.append("**Draft picks involved:**")
                for p in draft_picks:
                    msg_lines.append(f"- {p}")

            _post_discord("\n".join(msg_lines))

        elif ttype in ("waiver", "free_agent"):
            settings = t.get("settings") or {}
            bid = settings.get("waiver_bid", 0)
            msg_lines = [
                "🧾 **TRANSACTION**",
                f"*{when} UTC*",
                f"Type: `{ttype}` | FAAB: **{bid}**",
            ]
            _post_discord("\n".join(msg_lines))

        # Update state after each post so partial failures don't spam duplicates next run.
        state["last_seen_ms"] = max(int(state.get("last_seen_ms", 0)), ts_ms)
        save_state(state)

    print(f"Posted {len(new_txs)} new transaction(s). last_seen_ms={state['last_seen_ms']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("ERROR:", e)
        raise
