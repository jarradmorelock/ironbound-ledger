import json
import os
from typing import Dict, Any, List, Optional

import requests

LEAGUE_ID = os.environ["SLEEPER_LEAGUE_ID"]
WEBHOOK_WAIVERS = os.environ["DISCORD_WEBHOOK_WAIVERS"]
WEBHOOK_TRADES = os.environ["DISCORD_WEBHOOK_TRADES"]

STATE_FILE = "state.json"
USER_AGENT = "ironbound-ledger-bot/1.0"


def _get(url: str) -> Any:
    r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def post(webhook: str, msg: str) -> None:
    # Discord hard limit is 2000 chars
    if len(msg) > 1950:
        msg = msg[:1950] + "…"
    r = requests.post(webhook, json={"content": msg}, timeout=30)
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


def roster_name_map():
    rosters = get_json(f"{BASE}/league/{LEAGUE_ID}/rosters")

    rmap = {}
    user_to_rid = {}

    for r in rosters:
        rid = r.get("roster_id")
        oid = r.get("owner_id")
        name = r.get("metadata", {}).get("team_name", f"Roster {rid}")

        if rid is not None:
            rmap[int(rid)] = name

        if oid is not None and rid is not None:
            user_to_rid[int(oid)] = int(rid)

    return rmap, user_to_rid

def player_name_map() -> Dict[str, str]:
    players = _get("https://api.sleeper.app/v1/players/nfl")
    out: Dict[str, str] = {}
    for pid, p in players.items():
        name = (p.get("full_name") or "").strip() or pid
        pos = (p.get("position") or "").strip()
        team = (p.get("team") or "").strip()
        if pos and team:
            out[pid] = f"{name} ({pos} {team})"
        elif pos:
            out[pid] = f"{name} ({pos})"
        else:
            out[pid] = name
    return out


def fmt_player(pid: str, pmap: Dict[str, str]) -> str:
    return pmap.get(pid, pid)

def fmt_pick(p: dict, rmap: dict[int, str]) -> str:
    season = p.get("season", "?")
    rnd = p.get("round", "?")
    orig = p.get("roster_id")  # "original" roster this pick belongs to
    orig_txt = f" ({rmap.get(orig, f'Roster {orig}')} pick)" if orig is not None else ""
    return f"{season} - Rd {rnd}{orig_txt}"

def is_final_status(t: Dict[str, Any]) -> bool:
    status = (t.get("status") or "").lower()
    return status in ("complete", "approved", "executed")


def txn_ts(t: Dict[str, Any]) -> int:
    return int(t.get("status_updated") or t.get("created") or 0)


def chunk_lines(header: str, lines: List[str]) -> List[str]:
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


def format_waiver_receipt(t: Dict[str, Any], rmap: Dict[int, str], pmap: Dict[str, str]) -> Optional[List[str]]:
    adds = t.get("adds") or {}
    drops = t.get("drops") or {}
    if not adds and not drops:
        return None

    per: Dict[int, Dict[str, List[str]]] = {}

    for pid, rid in adds.items():
        rid = int(rid)
        per.setdefault(rid, {"adds": [], "drops": []})
        per[rid]["adds"].append(fmt_player(pid, pmap))

    for pid, rid in drops.items():
        rid = int(rid)
        per.setdefault(rid, {"adds": [], "drops": []})
        per[rid]["drops"].append(fmt_player(pid, pmap))

    ts = txn_ts(t)
    lines: List[str] = [f"🧾 **Player Transaction**"]

    for rid in sorted(per.keys()):
        team = rmap.get(rid, f"Roster {rid}")
        adds = per[rid]["adds"]
        drops = per[rid]["drops"]

        lines.append(f"**{team}**")

        if adds:
            lines.append("➕ **Adds:**")
            for p in adds:
                lines.append(p)

        if drops:
            lines.append("➖ **Drops:**")
            for p in drops:
                lines.append(p)

        lines.append("")  # spacer


    # remove trailing blank spacer
    while lines and lines[-1] == "":
        lines.pop()

    return lines


def format_trade_receipt(t: Dict[str, Any], rmap: Dict[int, str], pmap: Dict[str, str], user_to_rid: Dict[int, int]) -> Optional[List[str]]:
    adds = t.get("adds") or {}
    draft_picks = t.get("draft_picks") or []
    rosters = t.get("roster_ids") or t.get("consenter_roster_ids") or []

    received: Dict[int, List[str]] = {}

    for pid, dest in adds.items():
        dest = resolve_rid(dest)
        if dest is None:
            continue
        received.setdefault(dest, []).append(fmt_player(pid, pmap))


   def resolve_rid(val) -> Optional[int]:
        if val is None:
            return None
        try:
            x = int(val)
        except (TypeError, ValueError):
            return None
        if x in rmap:                 # already a roster_id
            return x
        if x in user_to_rid:          # it’s a user_id
            return user_to_rid[x]
        return None

    for pk in draft_picks:
        season = pk.get("season")
        rnd = pk.get("round")

        dest = resolve_rid(pk.get("roster_id") or pk.get("owner_id"))
        if dest is None:
            continue

        orig = resolve_rid(pk.get("previous_roster_id") or pk.get("previous_owner_id"))
        orig_txt = f" (from {rmap.get(orig, f'Roster {orig}')})" if orig is not None else ""

        received.setdefault(dest, []).append(f"{season} Rd {rnd} Pick{orig_txt}")



    if not received or len(rosters) < 2:
        return None

    lines: List[str] = ["🤝 **Trade Receipt**"]
    for rid_val in rosters:
        rid = resolve_rid(rid_val)
        if rid is None:
            continue
        team = rmap.get(rid, f"Roster {rid}")
        rec = received.get(rid, [])

        rec_txt = ", ".join(rec) if rec else "—"
        lines.append(f"**{team} receives:** {rec_txt}")

    return lines



def main():
    state = load_state()
    last_seen = int(state.get("last_seen_ms", 0))

    rmap, user_to_rid = roster_name_map()
    pmap = player_name_map()

    # Keep this window small; round 1 is clearly active for you right now.
    rounds_to_check = [0, 1, 2, 3]

    all_txs: List[Dict[str, Any]] = []
    for r in rounds_to_check:
        all_txs.extend(fetch_transactions(r))

    # Only new + final
    new_txs = []
    newest = last_seen

    for t in all_txs:
        ts = txn_ts(t)
        newest = max(newest, ts)
        if ts > last_seen and is_final_status(t):
            new_txs.append(t)

    # Advance state first (prevents replay if message posting fails mid-run)
    if newest > last_seen:
        state["last_seen_ms"] = newest
        save_state(state)

    if not new_txs:
        return

    new_txs.sort(key=txn_ts)

    waiver_lines: List[str] = []
    trade_lines: List[str] = []

    for t in new_txs:
        ttype = (t.get("type") or "").lower()

        if ttype in ("waiver", "free_agent", "add_drop"):
            block = format_waiver_receipt(t, rmap, pmap)
            if block:
                if waiver_lines:
                    waiver_lines.append("")  # spacer between receipts
                waiver_lines.extend(block)

        elif ttype == "trade":
            block = format_trade_receipt(t, rmap, pmap, user_to_rid)
            if block:
                if trade_lines:
                    trade_lines.append("")
                trade_lines.extend(block)

    if waiver_lines:
        for msg in chunk_lines("", waiver_lines):
            post(WEBHOOK_WAIVERS, msg)

    if trade_lines:
        for msg in chunk_lines("", trade_lines):
            post(WEBHOOK_TRADES, msg)


if __name__ == "__main__":
    main()
