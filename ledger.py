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


def roster_name_map() -> Dict[int, str]:
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
            meta = u.get("metadata") or {}
            name = meta.get("team_name") or u.get("display_name") or name
        out[rid] = name

    return out


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
    lines: List[str] = [f"🧾 **Waiver Receipt**"]

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


def format_trade_receipt(t: Dict[str, Any], rmap: Dict[int, str], pmap: Dict[str, str]) -> Optional[List[str]]:
    adds = t.get("adds") or {}
    draft_picks = t.get("draft_picks") or []
    rosters = t.get("roster_ids") or t.get("consenter_roster_ids") or []

    received: Dict[int, List[str]] = {}

    for pid, dest in adds.items():
        dest = int(dest)
        received.setdefault(dest, []).append(fmt_player(pid, pmap))

    for pk in draft_picks:
        season = pk.get("season")
        rnd = pk.get("round")
        dest = pk.get("roster_id")
        if dest is None:
            continue
        dest = int(dest)
        received.setdefault(dest, []).append(f"{season} R{rnd} pick")

    if not received or len(rosters) < 2:
        return None

    ts = txn_ts(t)
    lines: List[str] = [f"🤝 **Trade Receipt**"]

    for rid in [int(x) for x in rosters]:
        team = rmap.get(rid, f"Roster {rid}")
        rec = received.get(rid, [])
        rec_txt = ", ".join(rec) if rec else "—"
        lines.append(f"**{team} receives:** {rec_txt}")

    return lines


def main():
    state = load_state()
    last_seen = int(state.get("last_seen_ms", 0))

    rmap = roster_name_map()
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
            block = format_trade_receipt(t, rmap, pmap)
            if block:
                if trade_lines:
                    trade_lines.append("")
                trade_lines.extend(block)

    if waiver_lines:
        header = "🏈 **Ironbound Ledger** — waivers\n\n"
        for msg in chunk_lines(header, waiver_lines):
            post(WEBHOOK_WAIVERS, msg)

    if trade_lines:
        header = "🏈 **Ironbound Ledger** — trades\n\n"
        for msg in chunk_lines(header, trade_lines):
            post(WEBHOOK_TRADES, msg)


if __name__ == "__main__":
    main()
