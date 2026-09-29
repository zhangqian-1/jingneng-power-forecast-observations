"""Measurement and UTC contracts without importing the prediction runtime."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

EVENT_KEY = "JNH.Fluxcast.Compute"
STATION_POINTS = {
    "GARD": ("GARD_11MBY0100000BJ01XQ01", "GARD_12MBY0100000BJ01XQ01", "GARD_13MKA01CE903BJ01XQ01"),
    "JXRD": ("JXRD_11MBY0100000BJ01XQ01", "JXRD_12MBY0100000BJ01XQ01",
             "JXRD_13MKA01GA001BJ02XQ01", "JXRD_14MBY0100000BJ01XQ01", "JXRD_15MKA01GA001BJ02XQ01"),
    "JYRD": ("JYRD_LOADCTL:GTMWSEL1_1.OUT", "JYRD_LOADCTL:GTMWSEL1_2.OUT", "JYRD_30DCS01:FU101.PNT"),
    "JQRD": ("JQRD_10CBA00FA107XQ93", "JQRD_10CBA00FA108XQ93", "JQRD_10CBA00FA109XQ93"),
    "JFRD": ("JFRD_11MKA01GA001BJ40XQ01",),
    "WLRD": ("WLRD_13MKA0100000BJ01XQ01", "WLRD_11MBY10CE901XQ01"),
    "SZRD": ("SZRD_10DCS02FA133", "SZRD_10DCS02FA134"),
}
POINTS = tuple(point for points in STATION_POINTS.values() for point in points)
UTC_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?P<fraction>\.[0-9]{1,9})?(?:Z|\+00:00|\+0000)?"
)


def utc_slot(value: str) -> int:
    """Use UTC instants as keys, retaining the original spelling separately."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a UTC string")
    match = UTC_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("Expected YYYY-MM-DD[ T]HH:mm:ss with an optional UTC suffix")
    if any(digit != "0" for digit in (match["fraction"] or ".")[1:]):
        raise ValueError("timestamp must be exactly aligned to 15 minutes")
    stamp = datetime.strptime(value[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    if stamp.minute % 15 or stamp.second:
        raise ValueError("timestamp must be exactly aligned to 15 minutes")
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())


def numeric(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def measurement(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"point_table", "frames"}:
        raise ValueError("Request must contain only point_table and frames")
    table = payload["point_table"]
    if (not isinstance(table, list) or not all(isinstance(p, str) for p in table)
            or len(table) != len(POINTS) or set(table) != set(POINTS)):
        raise ValueError("point_table must list all 19 unique power point codes")
    frames = payload["frames"]
    if not isinstance(frames, list) or len(frames) != 1 or not isinstance(frames[0], dict):
        raise ValueError("frames must contain exactly one object")
    frame = frames[0]
    if set(frame) - set(POINTS) - {"timestamp"}:
        raise ValueError("frame contains unknown point codes")
    stamp = frame.get("timestamp")
    slot = utc_slot(stamp)
    values = {point: numeric(frame.get(point)) for point in POINTS}
    missing = [point for point, value in values.items() if value is None]
    status = "complete" if not missing else ("missing" if len(missing) == len(POINTS) else "incomplete")
    total = None
    if status == "complete":
        try:
            total = math.fsum(values.values())
        except OverflowError as exc:
            raise ValueError("Power sum is outside the finite numeric range") from exc
        if not math.isfinite(total):
            raise ValueError("Power sum is outside the finite numeric range")
    return {"timestamp": stamp, "slot": slot, "status": status, "values": values,
            "missing": missing, "total": total}


def forecast_curve(payload: dict) -> list[tuple[int, float]]:
    if not isinstance(payload, dict) or payload.get("event_key") != EVENT_KEY:
        raise ValueError("Unexpected forecast event_key")
    points = payload.get("result_point")
    if not isinstance(points, list) or len(points) != 96:
        raise ValueError("Forecast must contain 96 points")
    curve = []
    for point in points:
        if not isinstance(point, dict) or point.get("varname") != "totalPowerForecast":
            raise ValueError("Unexpected forecast varname")
        value = numeric(point.get("value"))
        if value is None:
            raise ValueError("Forecast values must be finite numbers")
        curve.append((utc_slot(point.get("timestamp")), value))
    curve.sort()
    if any(right[0] - left[0] != 900 for left, right in zip(curve, curve[1:])):
        raise ValueError("Forecast timestamps must be unique and continuous")
    return curve


def report(item: dict, prediction: float | None) -> dict:
    stamp = item["timestamp"]
    def point(name, value):
        return {"varname": name, "timestamp": stamp, "value": value}

    numbers = []
    info = [point("dataStatus", item["status"])]
    if item["status"] == "complete":
        numbers.append(point("totalPowerActual", float(item["total"])))
        if prediction is None:
            info.append(point("reason", "no_matching_forecast"))
        else:
            deviation = prediction - item["total"]
            if math.isfinite(deviation):
                numbers.append(point("totalPowerDeviation", float(deviation)))
            else:
                info.append(point("reason", "deviation_out_of_range"))
    else:
        info.append(point("missingPoints", ",".join(item["missing"])))
    return {"result_point": numbers, "extra_info": info, "event_key": EVENT_KEY}
