"""
Copies one account's watchlist out of the desktop app's database into
cloud_watchlist.json. The database is opened read-only, so the running app is
not disturbed.

  python export_watchlist.py <app.db> <cloud_watchlist.json> [account]

Exit code 0 = file written or already up to date, 1 = could not read the watchlist.
"""
import json
import sqlite3
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    db_path, out_path = sys.argv[1], sys.argv[2]
    wanted = sys.argv[3] if len(sys.argv) > 3 else ""

    conn = sqlite3.connect(f"file:{db_path.replace(chr(92), '/')}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        users = conn.execute("SELECT id, username FROM users ORDER BY id").fetchall()
        user = next((u for u in users if u["username"].lower() == wanted.lower()), None) if wanted else None
        user = user or (users[0] if users else None)
        if user is None:
            print("no account found in the app database")
            return 1
        row = conn.execute("SELECT tickers FROM watchlists WHERE user_id = ?", (user["id"],)).fetchone()
    finally:
        conn.close()

    tickers = json.loads(row["tickers"]) if row and row["tickers"] else []
    tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not tickers:
        print(f"the watchlist of {user['username']} is empty; cloud_watchlist.json left unchanged")
        return 1

    text = json.dumps({"account": user["username"], "tickers": tickers}, indent=2) + "\n"
    try:
        with open(out_path, encoding="utf-8") as f:
            if f.read() == text:
                print(f"watchlist unchanged ({len(tickers)} stocks)")
                return 0
    except OSError:
        pass
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"watchlist written: {len(tickers)} stocks for {user['username']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
