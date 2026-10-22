import os
import sys
import time
import datetime
import json
import re
from curl_cffi import requests

# ==============================================================================
# 1. UNIVERSAL CONFIGURATION (Pulled per-matrix job from GitHub YAML)
# ==============================================================================
VENUE_CODE = os.environ.get("VENUE_CODE")
VENUE_NAME = os.environ.get("VENUE_NAME", VENUE_CODE)
TARGET_EVENT = os.environ.get("TARGET_EVENT")
MOVIE_NAME = os.environ.get("MOVIE_NAME")
CITY_CODE = os.environ.get("CITY_CODE", "hyd")

MIN_SEATS_ALERT_THRESHOLD = int(os.environ.get("MIN_SEATS_ALERT_THRESHOLD", 5))
POLL_INTERVAL_SECONDS = 90  # Strict 90s interval for GitHub Actions ToS safety
MAX_RUNTIME_SECONDS = (5 * 3600) + (50 * 60)

STATUS_AVAILABLE = ['1', '4']
STATUS_BOOKED = ['2']

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "2031401740")

# ==============================================================================
# 2. NETWORK HELPERS
# ==============================================================================
def get_headers():
    return {"X-App-Code": "MOBAND2", "Lang": "en", "X-Platform": "AND"}

def send_telegram_notification(message, timeout=10):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=timeout)
    except Exception:
        pass

# ==============================================================================
# 3. MATRIX PARSER
# ==============================================================================
def fetch_seat_counts(session, session_id):
    url = "https://services-in.bookmyshow.com/doTrans.aspx"
    payload = {
        "strParam4": "", "strParam5": "Y", "strParam6": "", "strParam7": "N",
        "strParam1": session_id, "strParam2": "WEB", "strParam3": "",
        "strVenueCode": VENUE_CODE, "lngTransactionIdentifier": "0",
        "strAppCode": "MOBAND2", "strFormat": "json", "strCommand": "GETSEATLAYOUT"
    }
    headers = get_headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    try:
        response = session.post(url, headers=headers, data=payload, timeout=15)
        str_data = response.json().get("BookMyShow", {}).get("strData", "")
        if not str_data or "||" not in str_data:
            return None, None

        sections = str_data.split("||")
        rows = sections[1].split("|")
        available_seats = {}
        booked_count = 0

        for row in rows:
            if not row: continue
            parts = row.split(":")
            if len(parts) < 3: continue
            row_name = parts[1]

            for item in parts[2:]:
                if "000" in item: continue
                if "+" in item:
                    code, seat_num = item.split("+")
                    if seat_num == "0": continue
                    
                    status = code[len(row_name)] if code.startswith(row_name) else re.search(r'\d', code).group(0)
                    if status in STATUS_AVAILABLE:
                        if row_name not in available_seats:
                            available_seats[row_name] = []
                        available_seats[row_name].append(seat_num)
                    elif status in STATUS_BOOKED:
                        booked_count += 1

        return available_seats, booked_count
    except Exception:
        return None, None

# ==============================================================================
# 4. SWARM RUNNER ENGINE (With Composite State File & Strict Row Logic)
# ==============================================================================
def run_single_runner():
    session_id = os.environ.get("TARGET_SESSION_ID")
    show_time = os.environ.get("TARGET_SHOW_TIME", "Unknown Time")
    date_code = os.environ.get("TARGET_DATE_CODE", "Unknown Date")
    display_date = os.environ.get("TARGET_DISPLAY_DATE", "Unknown Date")
    screen_name = os.environ.get("TARGET_SCREEN", "Standard")
    
    seat_url = f"https://in.bookmyshow.com/movies/{CITY_CODE}/seat-layout/{TARGET_EVENT}/{VENUE_CODE}/{session_id}/{date_code}"

    if not session_id:
        return

    # Composite filename prevents collisions if two venues share the same Session ID number
    state_file = f"state_{VENUE_CODE}_{session_id}.json"
    http_session = requests.Session(impersonate="chrome120")

    print("\n" + "=" * 65)
    print(f"      SWARM RUNNER ACTIVE: {MOVIE_NAME}")
    print("=" * 65)
    print(f"🏛 Venue   : {VENUE_NAME} ({VENUE_CODE})")
    print(f"🖥 Screen  : {screen_name}")
    print(f"📅 Schedule: {display_date} at {show_time}")
    print(f"🎟 Session : {session_id} (Event: {TARGET_EVENT})")
    print(f"🔔 Rules   : >= {MIN_SEATS_ALERT_THRESHOLD} seats in the SAME row")
    print(f"⏱ Interval: {POLL_INTERVAL_SECONDS}s")
    print("=" * 65 + "\n")

    tg_startup_msg = (
        f"🚀 <b>Swarm Runner Active!</b>\n\n"
        f"🎬 <b>Movie:</b> {MOVIE_NAME}\n"
        f"🏛 <b>Venue:</b> {VENUE_NAME} ({VENUE_CODE})\n"
        f"🖥 <b>Screen:</b> {screen_name}\n"
        f"📅 <b>Showtime:</b> {display_date} | {show_time}\n"
        f"🎟 <b>Session ID:</b> {session_id}\n"
        f"⏱ <b>Poll Interval:</b> {POLL_INTERVAL_SECONDS}s"
    )
    send_telegram_notification(tg_startup_msg)

    last_known_seats = None
    last_known_count = 0
    
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                saved_data = json.load(f)
                last_known_seats = saved_data.get("available_seats", {})
                last_known_count = sum(len(seats) for seats in last_known_seats.values())
                print(f"[*] Restored previous state. Total seats: {last_known_count}")
        except Exception:
            pass

    start_time = time.time()

    try:
        while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
            timestamp = datetime.datetime.now().strftime("%I:%M:%S %p")
            avail_seats_dict, booked = fetch_seat_counts(http_session, session_id)

            if avail_seats_dict is not None:
                current_avail_count = sum(len(seats) for seats in avail_seats_dict.values())

                if last_known_seats is None:
                    last_known_seats = avail_seats_dict
                    last_known_count = current_avail_count
                    print(f"[{timestamp}] Initial Count -> Available: {current_avail_count:<3} | Booked: {booked}")
                else:
                    diff = current_avail_count - last_known_count
                    
                    qualifying_rows = {}
                    total_qualifying_unblocked = 0
                    
                    for row in sorted(avail_seats_dict.keys()):
                        seats = avail_seats_dict[row]
                        prev_row_seats = last_known_seats.get(row, [])
                        new_seats = [s for s in seats if s not in prev_row_seats]
                        
                        # Strict Row Validation
                        if len(new_seats) >= MIN_SEATS_ALERT_THRESHOLD:
                            new_seats_sorted = sorted(new_seats, key=lambda x: int(x) if x.isdigit() else x)
                            qualifying_rows[row] = new_seats_sorted
                            total_qualifying_unblocked += len(new_seats)

                    if qualifying_rows:
                        print(f"[{timestamp}] Available: {current_avail_count:<3} (+{total_qualifying_unblocked} UNBLOCKED!) <-- ALERT")
                        
                        unblocked_details = [
                            f"  • <b>Row {row}:</b> {', '.join(seats)}"
                            for row, seats in qualifying_rows.items()
                        ]
                        unblocked_text = "\n".join(unblocked_details)
                        
                        alert_msg = (
                            f"🚨 <b>SEATS UNBLOCKED!</b>\n\n"
                            f"🎬 <b>Movie:</b> {MOVIE_NAME}\n"
                            f"🏛 <b>Venue:</b> {VENUE_NAME} ({VENUE_CODE})\n"
                            f"🖥 <b>Screen:</b> {screen_name}\n\n"
                            f"📅 <b>Showtime:</b> {display_date} | {show_time}\n"
                            f"💺 <b>Unblocked:</b> +{total_qualifying_unblocked} seats (Total Available: {current_avail_count})\n\n"
                            f"🎟 <b>Unblocked Seats:</b>\n{unblocked_text}\n\n"
                            f"👉 <b>Direct Link:</b>\n{seat_url}"
                        )
                        send_telegram_notification(alert_msg)

                    elif diff > 0:
                        print(f"[{timestamp}] Available: {current_avail_count:<3} (+{diff} spread out, no row met threshold)")
                    elif diff < 0:
                        print(f"[{timestamp}] Available: {current_avail_count:<3} ({diff} booked)")
                    else:
                        print(f"[{timestamp}] Available: {current_avail_count:<3}")

                    last_known_seats = avail_seats_dict
                    last_known_count = current_avail_count

                # Save state locally after every poll so cancellations don't lose progress
                try:
                    with open(state_file, "w") as f:
                        json.dump({"available_seats": last_known_seats}, f)
                except Exception:
                    pass

            else:
                print(f"[{timestamp}] Read failed (Network Blip)")

            time.sleep(POLL_INTERVAL_SECONDS)
            
        notify_stop_standalone("Completed 6-Hour Cycle (Relaying runner)")
        
    except KeyboardInterrupt:
        notify_stop_standalone("Manual Cancellation (Ctrl+C)")
        sys.exit(0)
    except Exception:
        raise

# ==============================================================================
# 5. CANCELLATION NOTIFICATION HANDLER
# ==============================================================================
def notify_stop_standalone(reason):
    session_id = os.environ.get("TARGET_SESSION_ID", "Unknown")
    show_time = os.environ.get("TARGET_SHOW_TIME", "Unknown Time")
    display_date = os.environ.get("TARGET_DISPLAY_DATE", "Unknown Date")
    screen_name = os.environ.get("TARGET_SCREEN", "Standard")
    
    stop_msg = (
        f"🛑 <b>BMS Tracker Stopped</b>\n\n"
        f"🎬 <b>Movie:</b> {MOVIE_NAME}\n"
        f"🏛 <b>Venue:</b> {VENUE_NAME} ({VENUE_CODE})\n"
        f"🖥 <b>Screen:</b> {screen_name}\n"
        f"📅 <b>Showtime:</b> {display_date} | {show_time}\n"
        f"🎟 <b>Session ID:</b> {session_id}\n"
        f"ℹ️ <b>Status:</b> {reason}"
    )
    send_telegram_notification(stop_msg)
    print(f"[*] Stop notification sent. Reason: {reason}")

if __name__ == "__main__":
    if "--notify-stop" in sys.argv:
        reason = sys.argv[-1] if len(sys.argv) > 1 else "Unknown"
        notify_stop_standalone(reason)
    else:
        run_single_runner()
