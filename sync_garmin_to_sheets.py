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
    # One row per running activity: pace, HR, cadence, running dynamics, HR zones.
    "run_summary": [
        "activityId", "date", "name", "activityType", "distance_mi", "duration_s",
        "pace_min_mi", "avgHr", "maxHr", "avgCadence", "maxCadence",
        "strideLength_m", "groundContact_ms", "verticalOsc_cm", "verticalRatio",
        "avgPower", "elevGain_m", "aerobicTE", "anaerobicTE", "trainingLoad",
        "hrZ1_s", "hrZ2_s", "hrZ3_s", "hrZ4_s", "hrZ5_s",
    ],
    # One row per lap/split of each running activity (per-mile or per-km
    # auto-laps, or per-step laps on structured workouts like intervals).
    "run_laps": [
        "activityId", "date", "lapIndex", "intensity", "distance_mi", "duration_s",
        "pace_min_mi", "avgHr", "maxHr", "avgCadence", "strideLength_m",
        "groundContact_ms", "verticalOsc_cm", "avgPower", "elevGain_m",
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
    # NEW: per-set weight/reps detail from Garmin's structured Strength
    # Training logging (the watch prompts you to confirm each set) — this
    # is what the weekly plan should read for "what did I actually lift
    # last time" instead of relying on memory or manual reporting.
    "exercise_sets": ["activityId", "date", "exerciseName", "setNumber", "reps", "weightLb"],
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


RUN_TYPES = {"running", "treadmill_running", "trail_running", "track_running"}
RUN_LOOKBACK_DAYS = int(os.environ.get("RUN_LOOKBACK_DAYS", "120"))
M_PER_MI = 1609.344


def pace_str(speed_mps):
    """m/s -> 'm:ss' per mile ('' if not moving)."""
    if not speed_mps or speed_mps <= 0:
        return ""
    sec = M_PER_MI / speed_mps
    return f"{int(sec // 60)}:{int(round(sec % 60)):02d}" if round(sec % 60) < 60 else f"{int(sec // 60) + 1}:00"


def num(v, nd=2):
    return round(v, nd) if isinstance(v, (int, float)) else ""


def run_summary_row(a, detail, zones):
    """a = list-API activity dict; detail = get_activity() dict (may be {})."""
    sd = (detail or {}).get("summaryDTO") or {}
    zsec = {int(z.get("zoneNumber")): z.get("secsInZone") for z in (zones or []) if z.get("zoneNumber")}
    dist_m = a.get("distance") or 0
    return {
        "activityId": a.get("activityId"),
        "date": (a.get("startTimeLocal") or "")[:10],
        "name": a.get("activityName"),
        "activityType": (a.get("activityType") or {}).get("typeKey"),
        "distance_mi": num(dist_m / M_PER_MI, 3),
        "duration_s": num(a.get("duration"), 1),
        "pace_min_mi": pace_str(a.get("averageSpeed") or sd.get("averageSpeed")),
        "avgHr": a.get("averageHR"),
        "maxHr": a.get("maxHR"),
        "avgCadence": num(a.get("averageRunningCadenceInStepsPerMinute") or sd.get("averageRunCadence"), 1),
        "maxCadence": num(a.get("maxRunningCadenceInStepsPerMinute") or sd.get("maxRunCadence"), 1),
        "strideLength_m": num((a.get("avgStrideLength") or sd.get("strideLength") or 0) / (100 if (a.get("avgStrideLength") or 0) > 10 else 1), 2),
        "groundContact_ms": num(a.get("avgGroundContactTime") or sd.get("groundContactTime"), 0),
        "verticalOsc_cm": num(a.get("avgVerticalOscillation") or sd.get("verticalOscillation"), 1),
        "verticalRatio": num(a.get("avgVerticalRatio") or sd.get("verticalRatio"), 1),
        "avgPower": num(a.get("avgPower") or sd.get("averagePower"), 0),
        "elevGain_m": num(a.get("elevationGain") or sd.get("elevationGain"), 1),
        "aerobicTE": a.get("aerobicTrainingEffect"),
        "anaerobicTE": a.get("anaerobicTrainingEffect"),
        "trainingLoad": num(a.get("activityTrainingLoad"), 1),
        "hrZ1_s": zsec.get(1, ""), "hrZ2_s": zsec.get(2, ""), "hrZ3_s": zsec.get(3, ""),
        "hrZ4_s": zsec.get(4, ""), "hrZ5_s": zsec.get(5, ""),
    }


def run_lap_rows(aid, adate, splits):
    rows = []
    for i, lap in enumerate((splits or {}).get("lapDTOs") or [], start=1):
        dist = lap.get("distance") or 0
        rows.append({
            "activityId": aid, "date": adate,
            "lapIndex": lap.get("lapIndex") or i,
            "intensity": lap.get("intensityType") or "",
            "distance_mi": num(dist / M_PER_MI, 3),
            "duration_s": num(lap.get("duration"), 1),
            "pace_min_mi": pace_str(lap.get("averageSpeed")),
            "avgHr": lap.get("averageHR"), "maxHr": lap.get("maxHR"),
            "avgCadence": num(lap.get("averageRunCadence"), 1),
            "strideLength_m": num((lap.get("strideLength") or 0) / (100 if (lap.get("strideLength") or 0) > 10 else 1), 2),
            "groundContact_ms": num(lap.get("groundContactTime"), 0),
            "verticalOsc_cm": num(lap.get("verticalOscillation"), 1),
            "avgPower": num(lap.get("averagePower"), 0),
            "elevGain_m": num(lap.get("elevationGain"), 1),
        })
    return rows


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

    # ---- Run detail: per-run summary + per-lap splits ----
    # Only fetches runs not already in the sheet, so after the first
    # backfill each weekly run costs just the new runs' API calls.
    try:
        run_start = end - timedelta(days=max(RUN_LOOKBACK_DAYS, LOOKBACK_DAYS))
        run_acts = [a for a in api.get_activities_by_date(run_start.isoformat(), end.isoformat())
                    if (a.get("activityType") or {}).get("typeKey") in RUN_TYPES]
        ws_rs = get_or_create_ws(sh, "run_summary", SHEETS["run_summary"])
        ws_rl = get_or_create_ws(sh, "run_laps", SHEETS["run_laps"])
        have_rs = existing_keys(ws_rs, 0)
        have_rl = existing_keys(ws_rl, 0)
        new_rs, new_rl = [], []
        for a in run_acts:
            aid = a.get("activityId")
            if str(aid) in have_rs and str(aid) in have_rl:
                continue
            adate = (a.get("startTimeLocal") or "")[:10]
            detail, zones, splits = {}, [], {}
            for label, fn in (("detail", lambda: api.get_activity(aid)),
                              ("zones", lambda: api.get_activity_hr_in_timezones(aid)),
                              ("splits", lambda: api.get_activity_splits(aid))):
                try:
                    val = fn()
                    if label == "detail": detail = val
                    elif label == "zones": zones = val
                    else: splits = val
                except Exception as e:
                    print(f"run {label} {aid}: {e}")
            if str(aid) not in have_rs:
                new_rs.append(run_summary_row(a, detail, zones))
            if str(aid) not in have_rl:
                new_rl.extend(run_lap_rows(aid, adate, splits))
        if new_rs:
            ws_rs.append_rows([[r.get(h, "") for h in SHEETS["run_summary"]] for r in new_rs], value_input_option="RAW")
        if new_rl:
            ws_rl.append_rows([[r.get(h, "") for h in SHEETS["run_laps"]] for r in new_rl], value_input_option="RAW")
        print(f"run_summary: +{len(new_rs)} rows, run_laps: +{len(new_rl)} rows")
    except Exception as e:
        print(f"run detail: {e}")

    # ---- Per-set exercise detail (weight/reps) for strength activities ----
    # Only strength_training activities have this — the watch's structured
    # "sets" logging feature. Weight comes back from Garmin in GRAMS,
    # confirmed against real data (6804g = 15.0 lb kettlebell, 54431g =
    # 120.0 lb leg press) — convert to lb for the sheet. REST entries are
    # skipped; only ACTIVE (actually-performed) sets are kept. Exercise
    # names are Garmin's own auto-detected labels (e.g.
    # "SMITH_MACHINE_BENCH_PRESS" for a machine bench press) — not
    # normalized to match the app's own exercise names here; that mapping
    # happens by inspection when building the weekly plan, not in code,
    # since Garmin's auto-detected name and the app's exercise name are
    # often a close-but-not-exact match.
    GRAMS_PER_LB = 453.59237
    es_rows = []
    strength_acts = [a for a in act_rows if a.get("activityType") == "strength_training"]
    for a in strength_acts:
        aid = a["activityId"]
        adate = a["date"]
        try:
            data = api.get_activity_exercise_sets(aid)
            set_counters = {}
            for s in data.get("exerciseSets", []):
                if s.get("setType") != "ACTIVE":
                    continue
                exercises = s.get("exercises") or []
                ex_name = exercises[0].get("name") if exercises else "UNKNOWN"
                set_counters[ex_name] = set_counters.get(ex_name, 0) + 1
                weight_g = s.get("weight")
                weight_lb = round(weight_g / GRAMS_PER_LB, 1) if weight_g and weight_g > 0 else ""
                es_rows.append({
                    "activityId": aid,
                    "date": adate,
                    "exerciseName": ex_name,
                    "setNumber": set_counters[ex_name],
                    "reps": s.get("repetitionCount") or "",
                    "weightLb": weight_lb,
                })
        except Exception as e:
            print(f"exercise_sets {aid}: {e}")

    ws = get_or_create_ws(sh, "exercise_sets", SHEETS["exercise_sets"])
    # Per-activity upsert. The user often corrects reps/weights on Garmin
    # Connect after a workout (the watch misses some), so an activity that
    # is already in the sheet must be REPLACED when its sets changed, not
    # skipped. Compare each activity's stored sets with the fresh ones;
    # delete the stale block (bottom-up so row numbers stay valid) and
    # append the fresh rows.
    headers = SHEETS["exercise_sets"]
    def norm(vals):
        return tuple(str(v).strip() for v in vals)
    fresh = {}
    for r in es_rows:
        fresh.setdefault(str(r["activityId"]), []).append(norm([r.get(h, "") for h in headers]))
    all_vals = ws.get_all_values()
    stored = {}   # activityId -> list of (sheet row number, normalized row)
    for i, row in enumerate(all_vals[1:], start=2):
        if row and row[0]:
            stored.setdefault(str(row[0]), []).append((i, norm((row + [""] * len(headers))[:len(headers)])))
    def canon(rows):  # compare order-insensitively; numbers may differ in formatting
        out = []
        for r in rows:
            cells = []
            for c in r:
                try: cells.append(str(float(c)))
                except ValueError: cells.append(c)
            out.append(tuple(cells))
        return sorted(out)
    to_replace = [aid for aid, rows in fresh.items()
                  if aid in stored and canon(rows) != canon([r for _, r in stored[aid]])]
    # delete stale rows bottom-up
    stale_rows = sorted((n for aid in to_replace for n, _ in stored[aid]), reverse=True)
    for n in stale_rows:
        ws.delete_rows(n)
    replace_set = set(to_replace)
    rows_to_add = [r for r in es_rows
                   if str(r["activityId"]) not in stored or str(r["activityId"]) in replace_set]
    if rows_to_add:
        ws.append_rows([[r.get(h, "") for h in headers] for r in rows_to_add], value_input_option="RAW")
    print(f"exercise_sets: +{len(rows_to_add)} rows ({len(replace_set)} edited activities replaced, "
          f"{len({str(r['activityId']) for r in rows_to_add}) - len(replace_set)} new)")

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
