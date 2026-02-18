import json
import os
import time
from typing import Dict, Any, List, Optional

import requests

LEAGUE_ID = os.environ["SLEEPER_LEAGUE_ID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = "state.json"
USER_AGENT = "ironbound-ledger-bot/1.0"


def _get(url: str) -> Any:
    r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def post(msg: str) -> None:
    # Discord hard limit is 2000 chars per message
    if len(msg) > 1950:
        msg = msg[:1950] + "…"
    r = requests.post(WEBHOOK, json={"content": msg}, timeout=30)
    r.raise_for_status()


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"last_seen_ms": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_transactions(round_num: int) -> List[Dict[str, Any]]:
    return _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/transactions/{round_num}")


def roster_name_map() -> Dict[int, str]:
    """
    Map roster_id -> team name (prefer user metadata team_name, fallback to display_name).
    """
    users = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/users")
    rosters = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/rosters")

    user_by_id = {u["user_id"]: u for u in users}
    out: Dict[int, str] = {}

    for r in rosters:
        rid = int(r["roster_id"])
        owner_id = r.get("owner_id")
        name = f"Roster {rid}"
        if owner_id and owner_id in user_by_id:
            u = user_by_id[owner_id]
            meta = (u.get("metadata") or {})
            name = meta.get("team_name") or u.get("display_name") or name
        out[rid] = name

    return out


def player_name_map() -> Dict[str, str]:
    """
    Pull the player database once. It's large but fine for GH Actions.
    """
    players = _get("https://api.sleeper.app/v1/players/nfl")
    out: Dict[str, str] = {}
    for pid, p in players.items():
        fn = (p.get("first_name") or "").strip()
        ln = (p.get("last_name") or "").strip()
        pos = (p.get("position") or "").strip()
        team = (p.get("team") or "").strip()
        name = (p.get("full_name") or f"{fn} {ln}".strip() or pid).strip()
        if pos and team:
            out[pid] = f"{name} ({pos} {team})"
        elif pos:
            out[pid] = f"{name} ({pos})"
        else:
            out[pid] = name
    return out


def fmt_asset(pid: str, pmap: Dict[str, str]) -> str:
    # Sleeper uses "FAAB" in some contexts; keep it obvious if it shows up
    return pmap.get(pid, pid)


def chunk_messages(lines: List[str], header: str) -> List[str]:
    msgs = []
    cur = header
    for line in lines:
        if len(cur) + len(line) + 1 > 1900:
            msgs.append(cur)
            cur = header + line + "\n"
        else:
            cur += line + "\n"
    if cur.strip() != header.strip():
        msgs.append(cur)
    return msgs


def format_txn(t: Dict[str, Any], rmap: Dict[int, str], pmap: Dict[str, str]) -> Optional[List[str]]:
    ttype = (t.get("type") or "").lower()
    status = (t.get("status") or "").lower()
    ts = int(t.get("status_updated") or t.get("created") or 0)

    # ignore pending stuff; you want receipts
    if status not in ("complete", "approved", "executed"):
        return None

    # Some txns are commissioner/internal; skip if no real payload
    adds = t.get("adds") or {}
    drops = t.get("drops") or {}
    cons = t.get("consenter_roster_ids") or []
    rosters = t.get("roster_ids") or cons

    def rname(rid: Any) -> str:
        try:
            return rmap.get(int(rid), f"Roster {rid}")
        except Exception:
            return f"Roster {rid}"

    # --- WAIVER / FREE_AGENT / ADD_DROP ---
    if ttype in ("waiver", "free_agent", "add_drop"):
        # adds/drops are dict pid -> roster_id (adds) and pid -> roster_id (drops)
        # we want a per-roster grouping
        per: Dict[int, Dict[str, List[str]]] = {}
        for pid, rid in adds.items():
            rid = int(rid)
            per.setdefault(rid, {"adds": [], "drops": []})
            per[rid]["adds"].append(fmt_asset(pid, pmap))
        for pid, rid in drops.items():
            rid = int(rid)
            per.setdefault(rid, {"adds": [], "drops": []})
            per[rid]["drops"].append(fmt_asset(pid, pmap))

        lines = []
        for rid, payload in per.items():
            a = payload["adds"]
            d = payload["drops"]
            a_txt = ", ".join(a) if a else "—"
            d_txt = ", ".join(d) if d else "—"
            lines.append(f"**{rname(rid)}**: + {a_txt} | - {d_txt}")
        if not lines:
            return None

        return [
            f"🧾 **Waiver Receipt**  <t:{ts//1000}:f>",
            *lines
        ]

    # --- TRADE ---
    if ttype == "trade":
        # t["adds"] maps player_id -> destination_roster_id
        # t["draft_picks"] contains moved picks (if any)
        draft_picks = t.get("draft_picks") or []

        # Build per-roster received lists from adds + picks
        received: Dict[int, List[str]] = {}
        for pid, dest in adds.items():
            dest = int(dest)
            received.setdefault(dest, []).append(fmt_asset(pid, pmap))

        for pk in draft_picks:
            # Example: {"season":"2026","round":1,"roster_id":2,"owner_id":...,"previous_owner_id":...}
            season = pk.get("season")
            rnd = pk.get("round")
            dest = pk.get("roster_id")
            if dest is None:
                continue
            dest = int(dest)
            received.setdefault(dest, [])
            received[dest].append(f"{season} R{rnd} pick")

        # If no assets, nothing to show
        if not received or len(rosters) < 2:
            return None

        # Two-sided receipt view
        # If more than 2 rosters, still works (3-team trades)
        lines = []
        for rid in rosters:
            rid = int(rid)
            rec = received.get(rid, [])
            rec_txt = ", ".join(rec) if rec else "—"
            lines.append(f"**{rname(rid)} receives:** {rec_txt}")

        return [
            f"🤝 **Trade Receipt**  <t:{ts//1000}:f>",
            *lines
        ]

    return None


def main():
    state = load_state()
    last_seen = int(state.get("last_seen_ms", 0))

    # Pull maps once
    rmap = roster_name_map()
    pmap = player_name_map()

    # Check a small window of recent rounds (offseason tends to be 0/1; in-season use current week)
    rounds_to_check = [0, 1, 2, 3]

    all_txs: List[Dict[str, Any]] = []
    for r in rounds_to_check:
        all_txs.extend(fetch_transactions(r))

    # Filter only new + completed
    new_txs = []
    newest = last_seen
    for t in all_txs:
        ts = int(t.get("status_updated") or t.get("created") or 0)
        newest = max(newest, ts)
        if ts > last_seen:
            new_txs.append(t)

    # Always advance state if we saw a newer timestamp (prevents replay if GH job runs late)
    if newest > last_seen:
        state["last_seen_ms"] = newest
        save_state(state)

    # Nothing new? Stay silent.
    if not new_txs:
        return

    # Sort oldest -> newest for readable receipts
    new_txs.sort(key=lambda x: int(x.get("status_updated") or x.get("created") or 0))

    lines: List[str] = []
    for t in new_txs:
        formatted = format_txn(t, rmap, pmap)
        if not formatted:
            continue
        # add a blank line between receipts
        if lines:
            lines.append("")
        lines.extend(formatted)

    if not lines:
        return

    header = "🏈 **Ironbound Ledger** — new receipts\n\n"
    for msg in chunk_messages(lines, header):
        post(msg)


if __name__ == "__main__":
    main()
