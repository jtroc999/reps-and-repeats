"""
Pulls the last LOOKBACK_DAYS of Garmin Connect data (activities, VO2max/
fitness-age, training readiness, sleep, HRV) plus the current + next
month's scheduled workouts (where Runna's synced training plan appears),
and appends any new rows to a Google Sheet. Designed to run on a schedule
via GitHub Actions — see .github/workflows/sync.yml.

Required environment variables (set as GitHub Actions secrets):
    GARMINTOKENS                  - token string from login_once.py
    GOOGLE_SERVICE_ACCOUNT_JSON   - full JSON key of a Google service account
    SHEET_ID                       - the target Google Sheet's ID (from its URL)

Safe to re-run: it de-duplicates by activity ID / date before appending, so
running it daily or weekly with an overlapping lookback window won't create
duplicate rows.
"""
import json
import os
from datetime import date, timedelta

import gspread
from google.oauth2.service_account import Credentials
from garminconnect import Garmin

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))

# Garmin's numeric training-status codes, as documented across the
# Connect IQ / API community (not officially published by Garmin, so
# treat with a little skepticism if a label looks inconsistent with the
# readiness data for that day).
TRAINING_STATUS_LABELS = {
    0: "NO_STATUS",
    1: "DETRAINING",
    2: "RECOVERY",
    3: "MAINTAINING",
    4: "PRODUCTIVE",
    5: "PEAKING",
    6: "OVERREACHING",
    7: "STRAINED",
    8: "UNPRODUCTIVE",
}

SHEETS = {
    "activities": [
        "activityId", "date", "name", "activityType", "duration_s",
        "distance_km", "avgHr", "maxHr", "vo2Max", "aerobicTE",
        "anaerobicTE", "trainingLoad", "calories",
    ],
    "fitnessage": ["date", "biometricVo2Max", "rhr", "chronologicalAge", "currentBioAge"],
    "readiness": ["date", "score", "sleepScore", "hrvWeeklyAverage", "acuteLoad", "acwrFactorPercent"],
    "training_status": ["date", "trainingStatus"],
    "sleep": ["date", "overallScore", "totalSleepSec", "avgSleepStress"],
    "hrv": ["date", "lastNightAvg", "weeklyAvg", "status"],
    # NEW: Runna (or any provider) scheduled workouts, keyed by the real
    # calendar date they're scheduled on. `date` is the authoritative
    # field for placing a session in the week; Runna's own day-name text
    # in `title` has checked out correctly against `date` in practice,
    # but compute the real weekday from `date` when it matters rather
    # than trusting title text on faith.
    "scheduled_workouts": ["date", "title", "sportType", "workoutId", "provider", "description"],
}


def connect_garmin() -> Garmin:
    tokens = os.environ["GARMINTOKENS"]
    api = Garmin()
    api.login(tokens)
    return api


def connect_sheet():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["SHEET_ID"])


def get_or_create_ws(sh, name: str, header: list[str]):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(header) + 2)
        ws.append_row(header)
    return ws


def existing_keys(ws, key_col_idx: int) -> set:
    col = ws.col_values(key_col_idx + 1)  # 1-indexed, skip header
    return set(col[1:])


def append_new_rows(ws, header: list[str], rows: list[dict], key_field: str):
    have = existing_keys(ws, header.index(key_field))
    new_rows = [r for r in rows if str(r.get(key_field)) not in have]
    if not new_rows:
        return 0
    ws.append_rows([[r.get(h, "") for h in header] for r in new_rows], value_input_option="RAW")
    return len(new_rows)


def main():
    api = connect_garmin()
    sh = connect_sheet()

    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)

    # ---- Activities ----
    acts = api.get_activities_by_date(start.isoformat(), end.isoformat())
    act_rows = []
    for a in acts:
        act_rows.append({
            "activityId": a.get("activityId"),
            "date": (a.get("startTimeLocal") or "")[:10],
            "name": a.get("activityName"),
            "activityType": (a.get("activityType") or {}).get("typeKey"),
            "duration_s": a.get("duration"),
            "distance_km": (a.get("distance") or 0) / 1000.0,
            "avgHr": a.get("averageHR"),
            "maxHr": a.get("maxHR"),
            "vo2Max": a.get("vO2MaxValue"),
            "aerobicTE": a.get("aerobicTrainingEffect"),
            "anaerobicTE": a.get("anaerobicTrainingEffect"),
            "trainingLoad": a.get("activityTrainingLoad"),
            "calories": a.get("calories"),
        })
    ws = get_or_create_ws(sh, "activities", SHEETS["activities"])
    n = append_new_rows(ws, SHEETS["activities"], act_rows, "activityId")
    print(f"activities: +{n} new rows")

    # ---- Per-day metrics (fitness age, readiness, training status, sleep, HRV) ----
    fa_rows, tr_rows, ts_rows, sl_rows, hrv_rows = [], [], [], [], []
    d = start
    while d <= end:
        ds = d.isoformat()

        ts = None
        try:
            ts = api.get_training_status(ds)
        except Exception as e:
            print(f"training_status {ds}: {e}")

        try:
            fa = api.get_fitnessage_data(ds)
            if fa:
                vo2 = ((ts or {}).get("mostRecentVO2Max") or {}).get("generic") or {}
                fa_rows.append({
                    "date": ds,
                    "biometricVo2Max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
                    "rhr": (fa.get("components") or {}).get("rhr", {}).get("value"),
                    "chronologicalAge": fa.get("chronologicalAge"),
                    "currentBioAge": fa.get("fitnessAge"),
                })
        except Exception as e:
            print(f"fitnessage {ds}: {e}")

        try:
            tr = api.get_training_readiness(ds)
            if tr:
                row = tr[0] if isinstance(tr, list) else tr
                tr_rows.append({
                    "date": ds,
                    "score": row.get("score"),
                    "sleepScore": row.get("sleepScore"),
                    "hrvWeeklyAverage": row.get("hrvWeeklyAverage"),
                    "acuteLoad": row.get("acuteLoad"),
                    "acwrFactorPercent": row.get("acwrFactorPercent"),
                })
        except Exception as e:
            print(f"readiness {ds}: {e}")

        try:
            if ts:
                mrts = ts.get("mostRecentTrainingStatus") or {}
                lts = mrts.get("latestTrainingStatusData") or {}
                device_entry = next(iter(lts.values()), {}) if lts else {}
                code = device_entry.get("trainingStatus")
                ts_rows.append({
                    "date": ds,
                    "trainingStatus": TRAINING_STATUS_LABELS.get(code, code),
                })
        except Exception as e:
            print(f"training_status (parse) {ds}: {e}")

        try:
            sl = api.get_sleep_data(ds)
            if sl and sl.get("dailySleepDTO"):
                dto = sl["dailySleepDTO"]
                scores = dto.get("sleepScores", {})
                sl_rows.append({
                    "date": ds,
                    "overallScore": (scores.get("overall") or {}).get("value"),
                    "totalSleepSec": (dto.get("sleepTimeSeconds") or 0),
                    "avgSleepStress": dto.get("avgSleepStress"),
                })
        except Exception as e:
            print(f"sleep {ds}: {e}")

        try:
            hrv = api.get_hrv_data(ds)
            if hrv and hrv.get("hrvSummary"):
                summ = hrv["hrvSummary"]
                hrv_rows.append({
                    "date": ds,
                    "lastNightAvg": summ.get("lastNightAvg"),
                    "weeklyAvg": summ.get("weeklyAvg"),
                    "status": summ.get("status"),
                })
        except Exception as e:
            print(f"hrv {ds}: {e}")

        d += timedelta(days=1)

    for sheet_name, rows in [
        ("fitnessage", fa_rows), ("readiness", tr_rows),
        ("training_status", ts_rows), ("sleep", sl_rows), ("hrv", hrv_rows),
    ]:
        ws = get_or_create_ws(sh, sheet_name, SHEETS[sheet_name])
        n = append_new_rows(ws, SHEETS[sheet_name], rows, "date")
        print(f"{sheet_name}: +{n} new rows")

    # ---- Scheduled workouts (Runna's plan, via Garmin Calendar) ----
    # Build a workoutId -> description lookup from the workout library
    # first, since the calendar entries themselves don't carry the full
    # pace/structure text.
    workout_lookup = {}
    try:
        for w in api.get_workouts(0, 100):
            wid = w.get("workoutId")
            if wid is not None:
                workout_lookup[wid] = {
                    "provider": w.get("workoutProvider"),
                    "description": w.get("description"),
                }
    except Exception as e:
        print(f"workout library: {e}")

    sw_rows = []
    months_to_check = [(end.year, end.month)]
    next_month = end.month + 1
    next_year = end.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    months_to_check.append((next_year, next_month))

    for year, month in months_to_check:
        try:
            data = api.get_scheduled_workouts(year, month)
            for item in data.get("calendarItems", []):
                if item.get("itemType") != "workout":
                    continue  # skip "weight" (scale log noise) and "activity" (already completed) entries
                wid = item.get("workoutId")
                lookup = workout_lookup.get(wid, {})
                sw_rows.append({
                    "date": item.get("date"),
                    "title": item.get("title"),
                    "sportType": item.get("sportTypeKey"),
                    "workoutId": wid,
                    "provider": lookup.get("provider") or "",
                    "description": (lookup.get("description") or "").replace("\n", " | "),
                })
        except Exception as e:
            print(f"scheduled_workouts {year}-{month:02d}: {e}")

    ws = get_or_create_ws(sh, "scheduled_workouts", SHEETS["scheduled_workouts"])
    n = append_new_rows(ws, SHEETS["scheduled_workouts"], sw_rows, "date")
    print(f"scheduled_workouts: +{n} new rows")


if __name__ == "__main__":
    main()
