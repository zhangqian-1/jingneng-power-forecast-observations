"""Independent client-side checks for the platform contract."""
from datetime import datetime, timedelta
import math


PLATFORM_PATH = "/api/v1/fluxcast/compute"


def validate_platform_prediction(result: dict, payload: dict) -> None:
    if result.get("event_key") != "JNH.Fluxcast.Compute":
        raise AssertionError("Incorrect platform event_key")
    rows = result.get("result_point")
    if not isinstance(rows, list) or len(rows) != 96:
        raise AssertionError("Platform result must contain 96 predictions")
    cutoff = max(datetime.fromisoformat(row["timestamp"]) for row in payload["frames"])
    sample = payload["frames"][0]["timestamp"]
    for index, row in enumerate(rows, start=1):
        target = (cutoff + timedelta(minutes=15 * index)).strftime("%Y-%m-%d %H:%M:%S")
        target = target[:10] + sample[10] + target[11:] + sample[19:]
        if row.get("varname") != "totalPowerForecast" or row.get("timestamp") != target:
            raise AssertionError("Incorrect platform variable or target timestamp")
        if type(row.get("value")) is not float or not math.isfinite(row["value"]):
            raise AssertionError("Platform value must be a finite float")


def validate_not_ready(result: dict) -> None:
    if (result.get("event_key") != "JNH.Fluxcast.Compute" or result.get("result_point") != []
            or result.get("reason") not in {"history_not_ready", "weather_history_not_ready"}
            or not isinstance(result.get("message"), str) or not result["message"]
            or result.get("required_points") != 672
            or not 0 <= result.get("continuous_points", -1) < 672):
        raise AssertionError("Incorrect platform not-ready response")
