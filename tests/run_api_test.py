"""Exercise the production container with seven sequential daily requests."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from platform_test_utils import PLATFORM_PATH, validate_not_ready, validate_platform_prediction


ROOT = Path(__file__).resolve().parents[1]


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_until_ready(base_url: str) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            status, result = request_json(f"{base_url}{PLATFORM_PATH}/latest")
            if result.get("event_key") == "JNH.Fluxcast.Compute" and (
                    (status == 200 and len(result.get("result_point", [])) == 96)
                    or (status == 404 and result.get("reason") == "no_forecast")):
                return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise RuntimeError("container did not start within 180 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--fixture-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--save-response", type=Path, help="Save final response for the recreation check")
    mode.add_argument("--verify-restored", type=Path, help="Check saved result and repeated inference after recreation")
    args = parser.parse_args()

    wait_until_ready(args.base_url)
    files = sorted(args.fixture_dir.glob("day_*.json"))
    if len(files) != 7:
        raise RuntimeError(f"expected 7 daily fixtures, found {len(files)}")
    last_payload = json.loads(files[-1].read_text(encoding="utf-8"))
    endpoint = PLATFORM_PATH

    if args.verify_restored:
        saved = json.loads(args.verify_restored.read_text(encoding="utf-8"))
        status, latest = request_json(f"{args.base_url}{endpoint}/latest")
        if status != 200 or latest != saved:
            raise AssertionError("latest result was not preserved across container recreation")
        validate_platform_prediction(latest, last_payload)
        status, repeated = request_json(f"{args.base_url}{endpoint}", method="POST", payload=last_payload)
        if status != 200:
            raise AssertionError(f"history was not restored: HTTP {status}: {repeated}")
        validate_platform_prediction(repeated, last_payload)
        if repeated["result_point"] != saved["result_point"]:
            raise AssertionError("repeated prediction changed after container recreation")
        print("Persistence test passed: saved result, history and repeated prediction survived recreation.")
        return

    for index, path in enumerate(files, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        status, result = request_json(
            f"{args.base_url}{endpoint}", method="POST", payload=payload
        )
        expected = 200
        if status != expected:
            raise AssertionError(f"{path.name}: expected HTTP {expected}, got {status}: {result}")
        if index == len(files):
            validate_platform_prediction(result, payload)
        else:
            validate_not_ready(result)
        print(f"{path.name}: HTTP {status}")

    status, latest = request_json(f"{args.base_url}{endpoint}/latest")
    if status != 200 or latest != result:
        raise AssertionError(f"latest endpoint does not match the last successful response: {status}")
    validate_platform_prediction(latest, last_payload)
    if args.save_response:
        args.save_response.parent.mkdir(parents=True, exist_ok=True)
        args.save_response.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Real-data API test passed: latest endpoint returned 96 predictions.")


if __name__ == "__main__":
    main()
