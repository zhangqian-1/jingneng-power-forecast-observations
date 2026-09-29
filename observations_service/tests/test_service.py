import ast
import copy
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from observations_service.api import ForecastSync, MAX_BODY, ROUTE, make_server
from observations_service.contract import EVENT_KEY, POINTS, STATION_POINTS, forecast_curve, measurement, utc_slot
from observations_service.launcher import supervise
from observations_service.storage import Store

STAMP = "2026-11-25 05:00:00"
TARGET = utc_slot(STAMP)
ROOT = Path(__file__).resolve().parents[2]


def request(stamp=STAMP):
    values = (100, 120, 80, 150, 160, 80, 60, 140, 110, 115, 70, 90, 95, 100, 130, 50, 60, 40, 45)
    return {"point_table": list(POINTS),
            "frames": [{"timestamp": stamp, **dict(zip(POINTS, values))}]}


def curve(value=1850, start=TARGET):
    return {"event_key": EVENT_KEY, "result_point": [
        {"varname": "totalPowerForecast",
         "timestamp": datetime.fromtimestamp(start + i * 900, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "value": float(value)} for i in range(96)]}


def results(response):
    return {point["varname"]: point["value"]
            for points in (response["result_point"], response["extra_info"]) for point in points}


class ContractTests(unittest.TestCase):
    def test_point_mapping_matches_unchanged_predictor(self):
        tree = ast.parse((ROOT / "app/input_adapter.py").read_text(encoding="utf-8"))
        mapping = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "STATION_LOAD_POINTS" for t in node.targets))
        self.assertEqual({frozenset(points) for points in mapping.values()},
                         {frozenset(points) for points in STATION_POINTS.values()})
        self.assertEqual(len(POINTS), 19)

    def test_all_accepted_time_spellings_are_one_utc_instant(self):
        for separator in (" ", "T"):
            for suffix in ("", "Z", "+00:00", "+0000"):
                for fraction in ("", ".0", ".000000000"):
                    with self.subTest(separator=separator, suffix=suffix, fraction=fraction):
                        stamp = STAMP.replace(" ", separator) + fraction + suffix
                        self.assertEqual(measurement(request(stamp))["slot"], TARGET)
                        self.assertEqual(measurement(request(stamp))["timestamp"], stamp)

    def test_invalid_timestamps_are_not_rounded_or_converted(self):
        for stamp in (None, 1, "2026-11-25", "2026-11-25 05:01:00", "2026-11-25 05:00:01",
                      "2026-11-25 05:00:00.000000001Z", "2026-11-25T13:00:00+08:00",
                      "2026-11-31 05:00:00", "2026-11-25 05:00:00Z ", STAMP + "-00:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                utc_slot(stamp)

    def test_complete_sum_and_true_zero_negative(self):
        self.assertEqual(measurement(request())["total"], 1795.0)
        payload = request()
        payload["frames"][0][POINTS[0]] = 0
        payload["frames"][0][POINTS[1]] = -5
        self.assertEqual(measurement(payload)["total"], 1570.0)
        payload["frames"][0].update(dict.fromkeys(POINTS, 0))
        self.assertEqual(measurement(payload)["status"], "complete")
        self.assertEqual(measurement(payload)["total"], 0.0)

    def test_unusable_values_and_omission_are_missing_not_zero(self):
        for value in (None, True, False, "100", "bad", [], {}, math.nan, math.inf, -math.inf, 10**500):
            with self.subTest(value=str(value)[:20]):
                payload = request()
                payload["frames"][0][POINTS[0]] = value
                item = measurement(payload)
                self.assertEqual(item["status"], "incomplete")
                self.assertIsNone(item["total"])
                self.assertEqual(item["missing"], [POINTS[0]])
        payload = request()
        del payload["frames"][0][POINTS[0]]
        self.assertEqual(measurement(payload)["status"], "incomplete")

    def test_all_missing(self):
        payload = request()
        payload["frames"] = [{"timestamp": STAMP}]
        self.assertEqual(measurement(payload)["status"], "missing")

    def test_bad_schema(self):
        malformed = [None, [], {}, {"point_table": []}, {**request(), "unexpected": 1}]
        for frames in ([], [request()["frames"][0]] * 2, [None], {}):
            malformed.append({**request(), "frames": frames})
        for table in ([], list(POINTS[:-1]), list(POINTS) + [POINTS[0]],
                      list(POINTS[:-1]) + ["unknown"], [None] * 19, {}, list(POINTS[:-1]) + [POINTS[0]]):
            malformed.append({**request(), "point_table": table})
        payload = request()
        payload["frames"][0]["unknown"] = 1
        malformed.append(payload)
        for payload in malformed:
            with self.subTest(payload=str(payload)[:90]), self.assertRaises(ValueError):
                measurement(payload)

    def test_power_sum_overflow_rejected(self):
        payload = request()
        payload["frames"][0].update(dict.fromkeys(POINTS, 1e308))
        with self.assertRaises(ValueError):
            measurement(payload)

    def test_forecast_validation(self):
        invalid = []
        payload = curve()
        payload["result_point"].pop()
        invalid.append(payload)
        for name, value in (("value", math.nan), ("value", True), ("value", "1850"),
                            ("varname", "wrong"), ("timestamp", STAMP + "+08:00"),
                            ("timestamp", "2026-11-25 05:01:00")):
            payload = curve()
            payload["result_point"][0][name] = value
            invalid.append(payload)
        payload = curve()
        payload["result_point"][1] = payload["result_point"][0].copy()
        invalid.append(payload)
        invalid.extend([None, {}, {**curve(), "event_key": "wrong"}])
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]), self.assertRaises(ValueError):
                forecast_curve(payload)
        self.assertEqual(len(forecast_curve(curve())), 96)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "observations.sqlite3"
        self.store = Store(self.path)

    def test_no_forecast_returns_actual_only(self):
        response = self.store.receive(request())
        self.assertEqual(results(response), {"totalPowerActual": 1795.0,
                                            "dataStatus": "complete", "reason": "no_matching_forecast"})

    def test_matching_and_format_preservation(self):
        self.store.archive(curve(), TARGET - 1)
        for stamp in (STAMP, STAMP.replace(" ", "T") + "Z", STAMP + ".000000000+0000"):
            response = self.store.receive(request(stamp))
            self.assertEqual(results(response), {"totalPowerActual": 1795.0,
                                                "totalPowerDeviation": 55.0, "dataStatus": "complete"})
            for group in ("result_point", "extra_info"):
                self.assertTrue(all(p["timestamp"] == stamp for p in response[group]))
                self.assertEqual(len(response[group]), len({p["varname"] for p in response[group]}))
            self.assertTrue(all(type(p["value"]) is float for p in response["result_point"]))
        self.assertEqual(self.store.counts()["observations"], 1)

    def test_late_or_same_time_forecasts_are_not_eligible(self):
        for seen in (TARGET, TARGET + 900):
            self.store.archive(curve(value=seen), seen)
        self.assertNotIn("totalPowerDeviation", results(self.store.receive(request())))

    def test_same_batch_repoll_does_not_change_first_seen(self):
        batch = self.store.archive(curve(), TARGET + 1)
        self.assertEqual(batch, self.store.archive(curve(), TARGET - 1))
        self.assertNotIn("totalPowerDeviation", results(self.store.receive(request())))

    def test_equivalent_format_and_order_do_not_create_another_batch(self):
        batch = self.store.archive(curve(), TARGET - 10)
        alternate = curve()
        for point in alternate["result_point"]:
            point["timestamp"] = point["timestamp"].replace("T", " ").removesuffix("Z")
        alternate["result_point"].reverse()
        self.assertEqual(batch, self.store.archive(alternate, TARGET + 10))
        self.assertEqual(self.store.counts()["batches"], 1)
        self.assertEqual(results(self.store.receive(request()))["totalPowerDeviation"], 55.0)

    def test_fixed_association_survives_restart(self):
        self.store.archive(curve(), TARGET - 100)
        self.store.receive(request())
        self.store.archive(curve(2000), TARGET - 50)
        self.assertEqual(results(Store(self.path).receive(request()))["totalPowerDeviation"], 55.0)

    def test_corrections_keep_original_batch_even_after_new_batch(self):
        self.store.archive(curve(), TARGET - 100)
        self.store.receive(request())
        self.store.archive(curve(2000), TARGET - 50)
        payload = request(STAMP + "Z")
        payload["frames"][0][POINTS[0]] = 90
        response = self.store.receive(payload)
        self.assertEqual(results(response)["totalPowerDeviation"], 65.0)
        self.assertEqual(self.store.counts(), {"batches": 2, "observations": 1})

    def test_latest_eligible_batch_selected_on_first_receipt(self):
        self.store.archive(curve(), TARGET - 100)
        self.store.archive(curve(2000), TARGET - 50)
        self.store.archive(curve(9000), TARGET + 10)
        self.assertEqual(results(self.store.receive(request()))["totalPowerDeviation"], 205.0)

    def test_archived_batch_survives_restart_and_new_latest_curve(self):
        self.store.archive(curve(), TARGET - 100)
        self.store.archive(curve(2000, TARGET + 86400), TARGET + 100)
        store = Store(self.path)
        self.assertEqual(results(store.receive(request()))["totalPowerDeviation"], 55.0)

    def test_no_nearest_time_or_eight_hour_shift(self):
        self.store.archive(curve(start=TARGET + 900), TARGET - 100)
        self.assertNotIn("totalPowerDeviation", results(self.store.receive(request())))
        midnight = "2026-11-25 23:45:00"
        self.store.archive(curve(start=utc_slot(midnight)), TARGET - 100)
        self.assertEqual(results(self.store.receive(request("2026-11-26T00:00:00Z")))["totalPowerDeviation"], 55.0)

    def test_incomplete_and_missing_omit_all_numeric_results(self):
        self.store.archive(curve(), TARGET - 10)
        payload = request()
        payload["frames"][0][POINTS[0]] = None
        response = self.store.receive(payload)
        self.assertEqual(response["result_point"], [])
        self.assertEqual(results(response)["dataStatus"], "incomplete")
        payload["frames"] = [{"timestamp": STAMP}]
        response = self.store.receive(payload)
        self.assertEqual(response["result_point"], [])
        self.assertEqual(results(response)["dataStatus"], "missing")
        self.assertEqual(results(self.store.receive(request()))["totalPowerDeviation"], 55.0)

    def test_concurrent_retries_are_idempotent(self):
        self.store.archive(curve(), TARGET - 100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.store.receive(request()), range(16)))
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(self.store.counts()["observations"], 1)

    def test_invalid_curve_does_not_partially_archive(self):
        payload = curve()
        payload["result_point"][-1]["value"] = None
        with self.assertRaises(ValueError):
            self.store.archive(payload, TARGET - 10)
        self.assertEqual(self.store.counts()["batches"], 0)

    def test_example_files_are_consistent_and_explicitly_illustrative(self):
        folder = ROOT / "observations_service/examples"
        payload = json.loads((folder / "input_complete.json").read_text(encoding="utf-8"))
        self.assertEqual(payload, request())
        self.assertEqual(self.store.receive(payload), json.loads(
            (folder / "output_no_forecast.json").read_text(encoding="utf-8")))
        self.store.archive(curve(), TARGET - 1)
        self.assertEqual(self.store.receive(payload), json.loads(
            (folder / "output_complete.json").read_text(encoding="utf-8")))
        for state in ("incomplete", "missing"):
            payload = json.loads((folder / f"input_{state}.json").read_text(encoding="utf-8"))
            self.assertEqual(self.store.receive(payload), json.loads(
                (folder / f"output_{state}.json").read_text(encoding="utf-8")))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "db.sqlite3")
        self.upstream_requests = []
        self.upstream_payload = curve()
        self.upstream_status = 200
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.upstream_requests.append(("GET", self.path))
                self.send_response(owner.upstream_status)
                self.end_headers()
                self.wfile.write(json.dumps(owner.upstream_payload).encode())

            def do_POST(self):
                owner.upstream_requests.append(("POST", self.path))
                self.send_response(500)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.url = f"http://127.0.0.1:{self.upstream.server_port}/api/v1/fluxcast/compute/latest"
        self.sync = ForecastSync(self.store, self.url, interval=60, timeout=1)
        self.server = make_server("127.0.0.1", 0, self.store, self.sync)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_servers)

    def close_servers(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.sync.thread.is_alive():
            self.sync.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()

    def call(self, method="POST", path=ROUTE, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body,
                               headers=headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_sync_is_get_only_and_post_never_runs_prediction(self):
        before = copy.deepcopy(self.upstream_payload)
        with patch("observations_service.api.utc_now", return_value=TARGET - 1):
            self.sync.once()
        count = len(self.upstream_requests)
        code, response = self.call(body=json.dumps(request()))
        self.assertEqual(code, 200)
        self.assertEqual(results(response)["totalPowerDeviation"], 55.0)
        self.assertEqual(len(self.upstream_requests), count)
        self.assertEqual(self.upstream_requests, [("GET", "/api/v1/fluxcast/compute/latest")])
        self.assertEqual(self.upstream_payload, before)

    def test_upstream_failure_retains_archived_predictions(self):
        self.store.archive(curve(), TARGET - 1)
        for status, payload in ((500, {}), (404, {}), (200, {})):
            self.upstream_status, self.upstream_payload = status, payload
            self.sync.once()
            code, response = self.call(body=json.dumps(request()))
            self.assertEqual(code, 200)
            self.assertEqual(results(response)["totalPowerDeviation"], 55.0)

    def test_timeout_does_not_block_actual_request(self):
        with patch.object(self.sync.opener, "open", side_effect=TimeoutError("timeout")):
            self.sync.once()
        code, response = self.call(body=json.dumps(request()))
        self.assertEqual(code, 200)
        self.assertEqual(results(response)["totalPowerActual"], 1795.0)
        self.assertEqual(self.sync.status, "unavailable")

    def test_bad_requests_and_paths(self):
        for body in ('{}', '{', 'NaN', '{"point_table":[],"point_table":[],"frames":[]}',
                     json.dumps(request()) + "garbage"):
            with self.subTest(body=body[:50]):
                self.assertEqual(self.call(body=body)[0], 400)
        self.assertEqual(self.call(body="{}", headers={"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.call(path="/api/v1/fluxcast/compute", body="{}")[0], 404)
        self.assertEqual(self.call("GET", ROUTE)[0], 404)
        self.assertEqual(self.call(body="{}", headers={
            "Content-Type": "application/json", "Content-Length": str(MAX_BODY + 1)})[0], 413)

    def test_storage_failure_never_returns_success(self):
        with patch.object(self.store, "receive", side_effect=OSError("database unavailable")):
            with self.assertLogs("observations", level="ERROR"):
                code, response = self.call(body=json.dumps(request()))
            self.assertEqual(code, 500)
            self.assertEqual(response["result_point"], [])
        with patch.object(self.store, "counts", side_effect=OSError("database unavailable")):
            with self.assertLogs("observations", level="ERROR"):
                self.assertEqual(self.call("GET", "/health")[0], 503)

    def test_health_requires_live_archive_worker_but_not_ready_forecast(self):
        self.assertEqual(self.call("GET", "/health")[0], 503)
        self.upstream_status = 404
        self.sync.start()
        self.assertEqual(self.call("GET", "/health")[0], 200)


class LauncherTests(unittest.TestCase):
    def test_child_exit_stops_other_child_and_fails_container(self):
        failed, other = Mock(), Mock()
        failed.pid = 12345
        failed.poll.return_value = 0
        other.poll.return_value = None
        with patch("observations_service.launcher.subprocess.Popen", side_effect=[failed, other]):
            self.assertEqual(supervise([["predict"], ["observe"]], threading.Event()), 1)
        other.terminate.assert_called_once()
        other.wait.assert_called_once()

    def test_stop_shuts_down_both_children(self):
        children = [Mock(), Mock()]
        for child in children:
            child.poll.return_value = None
        stop = threading.Event()
        stop.set()
        with patch("observations_service.launcher.subprocess.Popen", side_effect=children):
            self.assertEqual(supervise([["predict"], ["observe"]], stop), 0)
        for child in children:
            child.terminate.assert_called_once()
            child.wait.assert_called_once()

    def test_failed_second_start_cleans_up_first_child(self):
        child = Mock()
        child.poll.return_value = None
        with patch("observations_service.launcher.subprocess.Popen", side_effect=[child, OSError("failed")]):
            with self.assertRaises(OSError):
                supervise([["predict"], ["observe"]], threading.Event())
        child.terminate.assert_called_once()

    def test_stuck_process_is_killed_after_grace_period(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("test", 20), 0]
        stop = threading.Event()
        stop.set()
        with patch("observations_service.launcher.subprocess.Popen", return_value=child):
            self.assertEqual(supervise([["observe"]], stop), 0)
        child.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
