import os
import time
import urllib.parse
from datetime import datetime
import pytz
import requests

# ================= Configuration =================
BARK_KEY = os.environ.get("BARK_KEY", "")  # Loaded securely from GitHub Secrets
POLL_INTERVAL = 4                          # Polling interval in seconds during live games
MAX_ASK_CENTS = 50                         # Alert threshold: yes_ask <= 50 cents
MAX_RUN_SECONDS = int(5.5 * 3600)          # Max runtime (5.5 hours) to stay safely under GitHub's 6-hour limit
# =================================================

# Map ESPN team abbreviations to Kalshi ticker abbreviations
ESPN_TO_KALSHI_ABBR = {
    "GS": "GSW",
    "NO": "NOP",
    "NY": "NYK",
    "SA": "SAS",
    "UTAH": "UTA",
    "WSH": "WAS",
}


def send_bark(title: str, body: str, url: str = "") -> None:
    """Send a time-sensitive push notification via iOS Bark."""
    if not BARK_KEY:
        print("BARK_KEY is missing. Skipping notification.", flush=True)
        return

    encoded_title = urllib.parse.quote(title)
    encoded_body = urllib.parse.quote(body)
    api_url = f"https://api.day.app/{BARK_KEY}/{encoded_title}/{encoded_body}"
    params = {
        "level": "timeSensitive",
        "sound": "bell",
        "group": "KalshiNBA",
    }
    if url:
        params["url"] = url

    try:
        requests.get(api_url, params=params, timeout=5)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Notification sent: {title} | {body}", flush=True)
    except Exception as err:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Failed to send Bark alert: {err}", flush=True)


def get_league_rankings() -> dict:
    """Fetch unified NBA league-wide standings (1 to 30) sorted by win percentage."""
    url = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
    resp = requests.get(url, timeout=10).json()
    teams = []

    for conf in resp.get("children", []):
        for entry in conf.get("standings", {}).get("entries", []):
            raw_abbr = entry["team"]["abbreviation"]
            abbr = ESPN_TO_KALSHI_ABBR.get(raw_abbr, raw_abbr)
            win_pct = 0.0
            for stat in entry.get("stats", []):
                if stat["name"] == "winPercent":
                    win_pct = float(stat["value"])
                    break
            teams.append((abbr, win_pct))

    # Sort descending by win percentage to assign league-wide ranks 1..30
    teams.sort(key=lambda x: x[1], reverse=True)
    return {abbr: idx + 1 for idx, (abbr, _) in enumerate(teams)}


def get_todays_games() -> list:
    """Fetch today's NBA scoreboard including game state, period, clock, and scores."""
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    resp = requests.get(url, timeout=10).json()
    games = []

    for event in resp.get("events", []):
        comp = event["competitions"][0]
        state = comp["status"]["type"]["state"]  # 'pre', 'in', or 'post'
        period = comp["status"].get("period", 0)
        clock = comp["status"].get("displayClock", "")

        home_team, away_team = None, None
        home_score, away_score = "0", "0"
        for competitor in comp["competitors"]:
            raw_abbr = competitor["team"]["abbreviation"]
            abbr = ESPN_TO_KALSHI_ABBR.get(raw_abbr, raw_abbr)
            if competitor["homeAway"] == "home":
                home_team, home_score = abbr, competitor.get("score", "0")
            else:
                away_team, away_score = abbr, competitor.get("score", "0")

        games.append({
            "state": state,
            "period": period,
            "clock": clock,
            "home": home_team,
            "away": away_team,
            "score": f"{away_team} {away_score} : {home_score} {home_team}",
        })
    return games


def fetch_kalshi_nba_markets() -> dict:
    """Fetch all open NBA single-game winner markets from Kalshi."""
    url = "https://api.elections.kalshi.com/trade-api/v2/markets"
    params = {"series_ticker": "KXNBAGAME", "status": "open", "limit": 100}

    try:
        resp = requests.get(url, params=params, timeout=8).json()
        markets = {}
        for m in resp.get("markets", []):
            # Example ticker format: KXNBAGAME-26NOV23BOSLAL-BOS
            ticker = m.get("ticker", "")
            parts = ticker.split("-")
            if len(parts) == 3:
                match_code = parts[1]
                team_abbr = parts[2]
                markets[(match_code, team_abbr)] = {
                    "ticker": ticker,
                    "event_ticker": m.get("event_ticker", ""),
                    "yes_ask": m.get("yes_ask", 100),
                    "yes_bid": m.get("yes_bid", 0),
                }
        return markets
    except Exception as err:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Kalshi API error: {err}", flush=True)
        return {}


def filter_target_matchups(games: list, rankings: dict) -> list:
    """
    Filter matchups based on unified league rankings (1..30):
    1. Top 5 team (rank <= 5) vs Bottom 15 team (16 <= rank <= 30)
    2. Rank 6-10 team (6 <= rank <= 10) vs Bottom 5 team (26 <= rank <= 30)
    """
    targets = []
    for g in games:
        if g["state"] == "post":
            continue

        h, a = g["home"], g["away"]
        rh, ra = rankings.get(h, 99), rankings.get(a, 99)

        home_qualifies = (rh <= 5 and 16 <= ra <= 30) or (6 <= rh <= 10 and 26 <= ra <= 30)
        away_qualifies = (ra <= 5 and 16 <= rh <= 30) or (6 <= ra <= 10 and 26 <= rh <= 30)

        if home_qualifies:
            targets.append({"strong": h, "weak": a, "strong_rank": rh, "weak_rank": ra})
        elif away_qualifies:
            targets.append({"strong": a, "weak": h, "strong_rank": ra, "weak_rank": rh})

    return targets


def run_monitor() -> None:
    pst = pytz.timezone("America/Los_Angeles")
    start_ts = time.time()
    print(f"[{datetime.now(pst).strftime('%Y-%m-%d %H:%M:%S %Z')}] Starting NBA Kalshi monitor...", flush=True)

    rankings = get_league_rankings()
    notified_matchups = set()

    while True:
        # Exit cleanly before reaching GitHub's 6-hour hard timeout
        if time.time() - start_ts > MAX_RUN_SECONDS:
            print(f"[{datetime.now(pst).strftime('%H:%M:%S')}] Max runtime reached. Exiting.", flush=True)
            break

        try:
            games = get_todays_games()
            targets = filter_target_matchups(games, rankings)

            if not targets:
                print(f"[{datetime.now(pst).strftime('%H:%M:%S')}] No qualifying matchups today. Exiting early.", flush=True)
                break

            # Exit immediately once all qualifying matchups have finished Q3 (entered Q4 or ended)
            active_first_3q = [
                g for g in games
                if g["state"] in ("pre", "in")
                and g["period"] <= 3
                and any(t["strong"] in (g["home"], g["away"]) and t["weak"] in (g["home"], g["away"]) for t in targets)
            ]
            if not active_first_3q:
                print(f"[{datetime.now(pst).strftime('%H:%M:%S')}] All qualifying games passed Q3. Exiting early.", flush=True)
                break

            # Only poll Kalshi when at least one qualifying game is actively in Q1-Q3
            live_q1_q3_games = [g for g in active_first_3q if g["state"] == "in"]
            if not live_q1_q3_games:
                time.sleep(30)
                continue

            kalshi_markets = fetch_kalshi_nba_markets()

            for g in live_q1_q3_games:
                for t in targets:
                    strong = t["strong"]
                    weak = t["weak"]
                    if strong not in (g["home"], g["away"]) or weak not in (g["home"], g["away"]):
                        continue

                    match_key = f"{strong}_vs_{weak}"
                    if match_key in notified_matchups:
                        continue

                    # Match the strong team's Yes contract on Kalshi
                    for (match_code, team_abbr), m_data in kalshi_markets.items():
                        if team_abbr == strong and (strong in match_code and weak in match_code):
                            ask = m_data["yes_ask"]
                            bid = m_data["yes_bid"]

                            if 0 < ask <= MAX_ASK_CENTS:
                                title = f"NBA Dip Alert: {strong} (#{t['strong_rank']}) at {ask}c"
                                body = (
                                    f"Score: {g['score']} | Q{g['period']} {g['clock']}\n"
                                    f"Opp Rank: #{t['weak_rank']} | Bid/Ask: {bid}c / {ask}c"
                                )
                                kalshi_url = f"https://kalshi.com/markets/kxnbagame/{m_data['event_ticker'].lower()}"
                                send_bark(title, body, kalshi_url)
                                notified_matchups.add(match_key)
                            break

        except Exception as loop_err:
            print(f"[{datetime.now(pst).strftime('%H:%M:%S')}] Loop error (retrying): {loop_err}", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_monitor()
