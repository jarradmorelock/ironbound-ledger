import argparse
import hashlib
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
    """Never silently discard receipt content when a message is too long."""
    if len(msg) > 1900:
        raise ValueError(f"Unsafely long Discord message: {len(msg)} characters")
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

def user_name_map() -> Dict[str, str]:
    users = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/users")
    out: Dict[str, str] = {}
    for u in users:
        uid = u.get("user_id")
        if not uid:
            continue
        # prefer team name if they set one; fallback to display_name
        md = u.get("metadata") or {}
        name = md.get("team_name") or u.get("display_name") or f"User {uid}"
        out[str(uid)] = name
    return out

def fetch_transactions(round_num: int) -> List[Dict[str, Any]]:
    return _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/transactions/{round_num}")

def roster_name_map():
    rosters = _get(f"https://api.sleeper.app/v1/league/{LEAGUE_ID}/rosters")
    umap = user_name_map()

    rmap: Dict[int, str] = {}
    user_to_rid: Dict[int, int] = {}

    for r in rosters:
        rid = r.get("roster_id")
        oid = r.get("owner_id")

        if rid is None:
            continue

        # name comes from users endpoint, because roster.metadata is None in your league
        name = umap.get(str(oid), f"Roster {rid}") if oid is not None else f"Roster {rid}"
        rmap[int(rid)] = name

        if oid is not None:
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

def resolve_rid(val: Any, rmap: Dict[int, str], user_to_rid: Dict[int, int]) -> Optional[int]:
    if val is None:
        return None
    try:
        x = int(val)
    except (TypeError, ValueError):
        return None
    if x in rmap:          # already a roster_id
        return x
    if x in user_to_rid:   # it's a user_id
        return user_to_rid[x]
    return None

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


def transaction_key(t: Dict[str, Any]) -> str:
    """Stable identity across repeated and overlapping Sleeper round responses."""
    key = t.get("transaction_id") or t.get("id")
    if key is not None:
        return str(key)
    payload = json.dumps(t, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def automatic_transaction_rounds() -> List[int]:
    """Include the historical gap and current week, plus the upcoming round.

    Unlike a rolling two-week window, this can recover transactions missed
    while the original reporter was stuck on rounds 0-3.
    """
    nfl_state = _get("https://api.sleeper.app/v1/state/nfl")
    try:
        week = int(nfl_state.get("leg") or nfl_state.get("week") or 1)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("Sleeper NFL state has no usable week") from exc
    if week < 1:
        week = 1
    return list(range(0, min(18, week + 1) + 1))


def new_final_transactions(
    transactions: List[Dict[str, Any]], state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    last_seen = int(state.get("last_seen_ms", 0))
    seen_at_last = {str(x) for x in state.get("seen_at_last_ms", [])}
    unique = {
        transaction_key(t): t for t in transactions if is_final_status(t)
    }
    selected = [
        t for key, t in unique.items()
        if txn_ts(t) > last_seen
        or (txn_ts(t) == last_seen and key not in seen_at_last)
    ]
    return sorted(selected, key=lambda t: (txn_ts(t), transaction_key(t)))


def acknowledge_transaction(state: Dict[str, Any], t: Dict[str, Any]) -> None:
    """Advance only after the receipt was sent successfully."""
    ts = txn_ts(t)
    prior = int(state.get("last_seen_ms", 0))
    key = transaction_key(t)
    if ts > prior:
        state["last_seen_ms"] = ts
        state["seen_at_last_ms"] = [key]
    elif ts == prior:
        state["seen_at_last_ms"] = sorted(
            set(str(x) for x in state.get("seen_at_last_ms", [])) | {key}
        )


def chunk_lines(header: str, lines: List[str]) -> List[str]:
    """Losslessly split long receipts into Discord-safe chunks.

    Split on line boundaries when possible, then spaces; an individual
    overlong line is split at the limit rather than silently truncated.
    """
    max_chars = 1800
    remaining = (header + "\n".join(lines)).strip()
    output: List[str] = []
    while remaining:
        if len(remaining) <= max_chars:
            output.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        output.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n ")
    return output

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

def format_trade_receipt(
    t: Dict[str, Any],
    rmap: Dict[int, str],
    pmap: Dict[str, str],
    user_to_rid: Dict[int, int],
) -> Optional[List[str]]:

    adds = t.get("adds") or {}
    draft_picks = t.get("draft_picks") or []
    rosters = t.get("roster_ids") or t.get("consenter_roster_ids") or []

    def resolve_rid(val: Any) -> Optional[int]:
        if val is None:
            return None
        try:
            x = int(val)
        except (TypeError, ValueError):
            return None
        # Some payloads give roster_id, some give user_id
        if x in rmap:
            return x
        if x in user_to_rid:
            return user_to_rid[x]
        return None

    received: Dict[int, List[str]] = {}

    # players
    for pid, dest in adds.items():
        rid = resolve_rid(dest)
        if rid is None:
            continue
        received.setdefault(rid, []).append(fmt_player(pid, pmap))

    # picks
    for pk in draft_picks:
        season = pk.get("season", "?")
        rnd = pk.get("round", "?")

        dest = resolve_rid(pk.get("owner_id") or pk.get("roster_id"))
        if dest is None:
            continue

        orig = resolve_rid(pk.get("roster_id") or pk.get("previous_owner_id") or pk.get("previous_roster_id"))
        orig_txt = f" (from {rmap.get(orig, f'Roster {orig}')})" if orig is not None else ""

        received.setdefault(dest, []).append(f"{season} Rd {rnd} Pick{orig_txt}")

    # If Sleeper didn't include both rosters, still try to print what we have
    roster_list: List[int] = []
    for rv in rosters:
        rid = resolve_rid(rv)
        if rid is not None:
            roster_list.append(rid)

    # Fall back to keys we actually saw if roster_ids missing
    if len(roster_list) < 2:
        roster_list = sorted(received.keys())

    if not received or len(roster_list) < 1:
        return None

    lines: List[str] = ["🤝 **Trade Receipt**"]
    for rid in roster_list:
        team = rmap.get(rid, f"Roster {rid}")
        rec = received.get(rid, [])
        rec_txt = ", ".join(rec) if rec else "—"
        lines.append(f"**{team} receives:** {rec_txt}")

    return lines


def publish_transactions(
    pending: List[Dict[str, Any]],
    state: Dict[str, Any],
    rmap: Dict[int, str],
    pmap: Dict[str, str],
    user_to_rid: Dict[int, int],
    limit: int = 8,
    root_state: Optional[Dict[str, Any]] = None,
) -> int:
    """Post a bounded chronological batch, checkpointing each successful receipt.

    The bounded batch prevents a month of transactions from flooding Discord
    in a single run. Subsequent scheduled runs continue from the checkpoint.
    """
    published = 0
    for t in pending[:limit]:
        ttype = (t.get("type") or "").lower()
        block = None
        webhook = None
        if ttype in ("waiver", "free_agent", "add_drop"):
            block = format_waiver_receipt(t, rmap, pmap)
            webhook = WEBHOOK_WAIVERS
        elif ttype == "trade":
            block = format_trade_receipt(t, rmap, pmap, user_to_rid)
            webhook = WEBHOOK_TRADES

        if block and webhook:
            for msg in chunk_lines("", block):
                post(webhook, msg)
        else:
            print(f"No renderable receipt for {ttype or 'unknown'} "
                  f"transaction {transaction_key(t)}; advancing checkpoint")

        acknowledge_transaction(state, t)
        save_state(root_state if root_state is not None else state)
        published += 1
    return published


def pending_by_channel(
    transactions: List[Dict[str, Any]], root_state: Dict[str, Any]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Separate trade and waiver watermarks so waivers cannot starve trades.

    On migration, the old cursor is the chronological checkpoint from
    the original combined queue: transactions at or before it have
    already been processed. Future runs track each channel separately.
    """
    trade_state = root_state.setdefault("trade_state", {
        "last_seen_ms": int(root_state.get("last_seen_ms", 0)),
        "seen_at_last_ms": [],
    })
    trade_txs = [
        t for t in transactions if (t.get("type") or "").lower() == "trade"
    ]
    waiver_txs = [
        t for t in transactions
        if (t.get("type") or "").lower() in ("waiver", "free_agent", "add_drop")
    ]
    return (
        new_final_transactions(trade_txs, trade_state),
        new_final_transactions(waiver_txs, root_state),
        trade_state,
    )


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Ironbound Sleeper transaction ledger")
    parser.add_argument(
        "--preview", action="store_true",
        help="List pending transactions without Discord posting or state updates",
    )
    parser.add_argument(
        "--batch-limit", type=int, default=8,
        help="Maximum waiver transactions per run (default: 8)",
    )
    parser.add_argument(
        "--trade-limit", type=int, default=12,
        help="Maximum trades per run, independently of waivers (default: 12)",
    )
    args = parser.parse_args(argv)
    if args.batch_limit < 1 or args.trade_limit < 1:
        parser.error("Batch limits must be at least 1")

    state = load_state()
    rmap, user_to_rid = roster_name_map()
    pmap = player_name_map()

    rounds_to_check = automatic_transaction_rounds()
    all_txs: List[Dict[str, Any]] = []
    for round_num in rounds_to_check:
        all_txs.extend(fetch_transactions(round_num))

    pending_trades, pending_waivers, trade_state = pending_by_channel(all_txs, state)
    print(
        f"Checked Sleeper rounds {rounds_to_check[0]}-{rounds_to_check[-1]}; "
        f"{len(pending_trades)} pending trades; "
        f"{len(pending_waivers)} pending player transactions."
    )
    if args.preview:
        for t in pending_trades:
            block = format_trade_receipt(t, rmap, pmap, user_to_rid)
            if block:
                print(f"Transaction {transaction_key(t)} at {txn_ts(t)}:")
                print("\n".join(block))
        print("PREVIEW ONLY: Discord and state.json were not changed.")
        return

    trade_count = publish_transactions(
        pending_trades, trade_state, rmap, pmap, user_to_rid,
        limit=args.trade_limit, root_state=state,
    )
    waiver_count = publish_transactions(
        pending_waivers, state, rmap, pmap, user_to_rid,
        limit=args.batch_limit, root_state=state,
    )
    print(f"Published {trade_count} trade(s) and {waiver_count} "
          f"player transaction(s). Remaining: "
          f"{len(pending_trades) - trade_count} trades; "
          f"{len(pending_waivers) - waiver_count} player transactions.")

if __name__ == "__main__":
    main()
