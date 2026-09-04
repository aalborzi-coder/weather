#!/usr/bin/env python3
"""
Rain alert: emails you at 6:30 AM local time, but only if rain is likely today.

Data source: Open-Meteo (free, no API key).
Sending: any SMTP server (defaults to Gmail).
Dependencies: none — Python 3.9+ standard library only.

Behaviour:
  - Runs, checks the local clock, and exits immediately unless it's inside the
    morning send window (so a UTC cron can fire twice and only one run counts).
  - Pulls today's hour-by-hour forecast, keeps hours from now until midnight.
  - An hour counts as "rainy" if its precipitation chance is at or above the
    threshold (default 40%).
  - No rainy hours -> no email at all. Silence means a dry day.
  - Rainy hours are merged into contiguous windows and sent as one short email.

Configuration is entirely through environment variables (see README).
"""

import json
import os
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LAT = float(os.environ.get("RAIN_LAT", "40.4433"))
LON = float(os.environ.get("RAIN_LON", "-79.9436"))
TZ_NAME = os.environ.get("RAIN_TZ", "America/New_York")
LOCATION_NAME = os.environ.get("RAIN_LOCATION_NAME", "your area")

# An hour counts as rainy at or above this precipitation chance.
PROB_THRESHOLD = int(os.environ.get("RAIN_PROB_THRESHOLD", "40"))

# Hour (local, 24h) the email should land. The script accepts a 90-minute
# window starting here, which absorbs GitHub Actions' cron delays.
SEND_HOUR = int(os.environ.get("RAIN_SEND_HOUR", "6"))
WINDOW_MINUTES = int(os.environ.get("RAIN_WINDOW_MINUTES", "90"))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or SMTP_USER
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER

FORCE_SEND = os.environ.get("RAIN_FORCE_SEND", "").strip() not in ("", "0", "false")
DRY_RUN = os.environ.get("RAIN_DRY_RUN", "").strip() not in ("", "0", "false")

API_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> short label, worst-wins ordering by key size.
CODE_LABELS = {
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy downpours",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}
SEVERITY = [51, 53, 61, 71, 55, 80, 63, 73, 81, 85, 65, 75, 82, 86,
            56, 57, 66, 67, 95, 96, 99]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def within_send_window(now):
    """True if the local clock is inside the morning send window."""
    minutes = (now.hour * 60 + now.minute) - (SEND_HOUR * 60)
    return 0 <= minutes < WINDOW_MINUTES


def fetch_forecast():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "precipitation_probability,precipitation,snowfall,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": TZ_NAME,
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "rain-alert/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def build_hours(data, now):
    """Today's remaining hours, each as a dict. Times start at 00:00 local."""
    hourly = data["hourly"]
    out = []
    for i, stamp in enumerate(hourly["time"]):
        hour = int(stamp[11:13])
        if hour < now.hour:
            continue  # already happened
        out.append({
            "hour": hour,
            "prob": hourly["precipitation_probability"][i] or 0,
            "precip": hourly["precipitation"][i] or 0.0,
            "snow": hourly["snowfall"][i] or 0.0,
            "code": hourly["weather_code"][i] or 0,
        })
    return out


def group_windows(hours):
    """Merge contiguous rainy hours into windows."""
    windows, current = [], None
    for h in hours:
        rainy = h["prob"] >= PROB_THRESHOLD
        if rainy and current is None:
            current = {"start": h["hour"], "end": h["hour"], "hours": [h]}
        elif rainy:
            current["end"] = h["hour"]
            current["hours"].append(h)
        elif current is not None:
            windows.append(current)
            current = None
    if current is not None:
        windows.append(current)

    for w in windows:
        w["peak"] = max(h["prob"] for h in w["hours"])
        w["precip"] = sum(h["precip"] for h in w["hours"])
        w["snow"] = sum(h["snow"] for h in w["hours"])
        codes = [h["code"] for h in w["hours"] if h["code"] in CODE_LABELS]
        w["label"] = CODE_LABELS[max(codes, key=SEVERITY.index)] if codes else "precipitation"
    return windows


def fmt_hour(h, with_suffix=True):
    h %= 24
    suffix = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh} {suffix}" if with_suffix else str(hh)


def fmt_range(window):
    """Rain during hour 8 means 8:00-9:00, so a window ends at end+1."""
    start, end = window["start"], (window["end"] + 1) % 24
    same_half = (start < 12) == (end < 12) and end != 0
    return f"{fmt_hour(start, not same_half)}–{fmt_hour(end)}"


def subject_line(windows):
    ranges = ", ".join(fmt_range(w) for w in windows)
    peak = max(w["peak"] for w in windows)
    heaviest = max(windows, key=lambda w: w["peak"])["label"]
    word = "Snow" if "snow" in heaviest else "Rain"
    if len(windows) == 1:
        return f"{word} today, {ranges} ({peak}% chance)"
    return f"{word} today: {ranges}"


def amount_text(window):
    if window["snow"] >= 0.1:
        return f"{window['snow']:.1f} in snow"
    if window["precip"] >= 0.005:
        return f"{window['precip']:.2f} in"
    return "trace"


def build_bodies(windows, data, now):
    high = round(data["daily"]["temperature_2m_max"][0])
    low = round(data["daily"]["temperature_2m_min"][0])
    date_str = now.strftime("%A, %B %-d") if os.name != "nt" else now.strftime("%A, %B %d")

    lines = [f"{date_str} — {LOCATION_NAME}", f"High {high}°F / low {low}°F", ""]
    for w in windows:
        lines.append(f"  {fmt_range(w):>16}   {w['peak']}% chance   {w['label']}   {amount_text(w)}")
    lines.append("")
    lines.append(f"Hours counted as rainy at {PROB_THRESHOLD}% chance or above.")
    lines.append("Forecast: Open-Meteo.")
    text = "\n".join(lines)

    rows = "".join(
        f"""<tr>
              <td style="padding:10px 16px 10px 0;font-size:17px;font-weight:600;white-space:nowrap;">{fmt_range(w)}</td>
              <td style="padding:10px 16px 10px 0;font-size:17px;color:#2b6cb0;white-space:nowrap;">{w['peak']}%</td>
              <td style="padding:10px 16px 10px 0;font-size:15px;color:#4a5568;">{w['label']}</td>
              <td style="padding:10px 0;font-size:15px;color:#718096;white-space:nowrap;">{amount_text(w)}</td>
            </tr>"""
        for w in windows
    )
    html = f"""<div style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#1a202c;max-width:520px;">
  <p style="margin:0 0 4px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#718096;">{date_str} · {LOCATION_NAME}</p>
  <h2 style="margin:0 0 2px;font-size:22px;">Take a rain jacket.</h2>
  <p style="margin:0 0 18px;font-size:15px;color:#4a5568;">High {high}°F, low {low}°F.</p>
  <table style="border-collapse:collapse;width:100%;">{rows}</table>
  <p style="margin:22px 0 0;font-size:12px;color:#a0aec0;">
    Hours are counted as rainy at {PROB_THRESHOLD}% chance or above. Forecast from Open-Meteo.
  </p>
</div>"""
    return text, html


def send_email(subject, text, html):
    if not SMTP_USER or not SMTP_PASSWORD or not MAIL_TO:
        sys.exit("Missing SMTP_USER, SMTP_PASSWORD, or MAIL_TO.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=context)
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


# --------------------------------------------------------------------------

def main():
    now = datetime.now(ZoneInfo(TZ_NAME))
    print(f"Local time: {now:%Y-%m-%d %H:%M %Z}")

    if not within_send_window(now) and not FORCE_SEND:
        print(f"Outside the {SEND_HOUR:02d}:00 send window — nothing to do.")
        return

    data = fetch_forecast()
    hours = build_hours(data, now)
    windows = group_windows(hours)

    if not windows:
        peak = max((h["prob"] for h in hours), default=0)
        print(f"No rain likely today (peak chance {peak}%). No email sent.")
        return

    subject = subject_line(windows)
    text, html = build_bodies(windows, data, now)
    print(f"Subject: {subject}")

    if DRY_RUN:
        print("\n--- dry run, not sending ---\n")
        print(text)
        return

    send_email(subject, text, html)
    print(f"Sent to {MAIL_TO}.")


if __name__ == "__main__":
    main()
