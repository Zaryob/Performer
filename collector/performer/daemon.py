"""A localhost API, so a measurement can be started from the browser.

This is the one part of Performer that listens on a socket, and it runs as
root on a machine somebody cares about.  Everything about its design is
therefore a refusal:

  * it binds ``127.0.0.1`` and nothing else, and rejects a request whose
    ``Host`` header is not a loopback name -- that is what stops a DNS
    rebinding attack from turning a browser on this machine into a proxy for
    the network;
  * every request carries a bearer token printed once at startup, compared in
    constant time.  There is no cookie and no session, so a page on another
    origin has nothing to replay;
  * a profile is chosen by *name* from a whitelist and resolved to a file by
    the collector, never by a path from the request;
  * nothing reaches a shell.  The collector already runs bpftrace through
    ``subprocess`` without ``shell=True``, and the daemon adds no place where
    request text could become a command;
  * a label goes into a file name, so it is matched against
    ``layout.LABEL_RE`` before anything else happens, and the run directory a
    download resolves to is checked to be inside the output directory after
    ``resolve()``.

The one thing it will do is expensive and privileged -- attach eBPF programs
to a process -- so it does exactly one at a time and reports what it is doing.

Threading model: one job at a time in a worker thread, guarded by a lock.
The HTTP server is threaded so that polling a job's progress does not block
behind the collection that is producing it.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, layout, preflight, proc, profiles
from .bundle import Bundle
from .errors import PerformerError

#: Only ever this. Passing an address in would be a footgun with no use case:
#: the API starts privileged measurements and has no authorisation model
#: beyond "you can read the token this process printed".
BIND_HOST = "127.0.0.1"

#: Host headers a browser can legitimately send for a loopback server.
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: A request body large enough for any of these endpoints is an attack or a
#: bug; either way it is refused before being read into memory.
MAX_BODY_BYTES = 64 * 1024

#: Collection durations the API will accept, in seconds.  The upper bound is
#: not a safety rail against the tool -- profiles have their own -- but
#: against a request that would hold the one job slot for a day.
MIN_DURATION_S = 1.0
MAX_DURATION_S = 900.0

#: Kept so a browser left open overnight does not accumulate unbounded state.
MAX_JOBS_REMEMBERED = 50

_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    pid: int
    profile: str
    label: str
    duration_s: float
    state: str = "queued"  # queued | running | done | failed | cancelled
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    lines: List[str] = field(default_factory=list)
    error: Optional[str] = None
    run_id: Optional[str] = None
    archive: Optional[str] = None
    status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "profile": self.profile,
            "label": self.label,
            "duration_s": self.duration_s,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_s": (
                round((self.ended_at or time.time()) - self.started_at, 1)
                if self.started_at
                else None
            ),
            "log": list(self.lines),
            "error": self.error,
            "run_id": self.run_id,
            "archive": self.archive,
            "status": self.status,
        }


class Rejected(PerformerError):
    """A request that will not be served, with the status to answer with."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate_pid(raw: Any) -> int:
    """A pid must be an integer naming a live process, and not this one.

    Rejecting a string that merely looks numeric matters: the value ends up in
    a bpftrace program's positional parameters, and "1234; kill -9 1" is a
    perfectly good string.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise Rejected(HTTPStatus.BAD_REQUEST, "pid must be an integer")
    if raw <= 1:
        raise Rejected(HTTPStatus.BAD_REQUEST, f"pid {raw} is not a valid target")
    if raw == os.getpid():
        raise Rejected(
            HTTPStatus.BAD_REQUEST,
            "refusing to trace the daemon itself; the probes would record their own overhead",
        )
    if not proc.is_running(raw):
        raise Rejected(HTTPStatus.NOT_FOUND, f"no running process with pid {raw}")
    return raw


def validate_profile(raw: Any) -> str:
    """Profiles are chosen by name from a whitelist, never by path.

    ``profiles.load`` would itself refuse a name that is not a bare
    identifier, but the whitelist is checked here as well so the API's answer
    is "not one of these" rather than a file-system error naming a path.
    """
    if not isinstance(raw, str):
        raise Rejected(HTTPStatus.BAD_REQUEST, "profile must be a string")
    allowed = sorted(profiles.available())
    if raw not in allowed:
        raise Rejected(
            HTTPStatus.BAD_REQUEST,
            f"unknown profile {raw!r}; available: {', '.join(allowed)}",
        )
    return raw


def validate_label(raw: Any) -> str:
    """A label becomes part of a directory and an archive name."""
    if not isinstance(raw, str) or not raw:
        raise Rejected(HTTPStatus.BAD_REQUEST, "label is required")
    if not layout.LABEL_RE.match(raw):
        raise Rejected(
            HTTPStatus.BAD_REQUEST,
            "label may contain only letters, digits, dot, dash and underscore "
            "(it becomes a directory and an archive name)",
        )
    return raw


def validate_duration(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise Rejected(HTTPStatus.BAD_REQUEST, "duration_s must be a number")
    value = float(raw)
    if not (MIN_DURATION_S <= value <= MAX_DURATION_S):
        raise Rejected(
            HTTPStatus.BAD_REQUEST,
            f"duration_s must be between {MIN_DURATION_S:g} and {MAX_DURATION_S:g}",
        )
    return value


def validate_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 16:
        raise Rejected(HTTPStatus.BAD_REQUEST, "tags must be a list of at most 16 strings")
    for tag in raw:
        if not isinstance(tag, str) or not _TAG_RE.match(tag):
            raise Rejected(
                HTTPStatus.BAD_REQUEST,
                f"tag {tag!r} may contain only letters, digits, dot, dash and underscore",
            )
    return list(raw)


def validate_notes(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise Rejected(HTTPStatus.BAD_REQUEST, "notes must be a string")
    if len(raw) > 2000:
        raise Rejected(HTTPStatus.BAD_REQUEST, "notes is limited to 2000 characters")
    return raw


def validate_run_id(raw: str) -> str:
    """A run id addresses a file, so it is matched, never joined blindly.

    ``layout.RUN_ID_RE`` admits no separator and no dot pair, which is what
    makes the join below safe -- but the resolved path is checked against the
    output directory anyway, because one validation between a request and a
    file read is one too few.
    """
    if not layout.RUN_ID_RE.match(raw):
        raise Rejected(HTTPStatus.BAD_REQUEST, "malformed run id")
    return raw


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


@dataclass
class DaemonOptions:
    out_dir: Path
    port: int = 7878
    token: Optional[str] = None
    #: Profiles the API will run.  Defaults to everything installed; narrowing
    #: it is how an operator says "this box may only ever run `light`".
    allowed_profiles: Optional[Tuple[str, ...]] = None
    bpftrace: Optional[str] = None
    #: Serve the built viewer at `/`. Without it the API is reachable only by
    #: a client that already has the token, which is a useful mode for scripts
    #: and a useless one for people.
    serve_viewer: bool = True
    open_browser: bool = False


class Service:
    """The API's state, separated from HTTP so it can be tested directly."""

    def __init__(self, options: DaemonOptions) -> None:
        self.options = options
        self.out_dir = Path(options.out_dir).resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.token = options.token or secrets.token_urlsafe(32)
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._current: Optional[str] = None
        self._cancel = threading.Event()
        self._collect_fn: Callable[..., Any] = _real_collect

    # -- auth ------------------------------------------------------------

    def check_token(self, presented: Optional[str]) -> None:
        # compare_digest rather than ==: the token is a secret and the
        # comparison is against attacker-supplied input.
        if not presented or not hmac.compare_digest(presented, self.token):
            raise Rejected(HTTPStatus.UNAUTHORIZED, "missing or invalid token")

    # -- profiles ---------------------------------------------------------

    def allowed_profiles(self) -> List[str]:
        installed = sorted(profiles.available())
        allowed = self.options.allowed_profiles
        if allowed is None:
            return installed
        return [name for name in installed if name in allowed]

    def profile_info(self) -> List[Dict[str, Any]]:
        info = []
        for name in self.allowed_profiles():
            try:
                profile = profiles.load(name)
            except PerformerError:
                continue
            info.append(
                {
                    "name": profile.name,
                    "description": profile.description,
                    "probes": list(profile.probe_names),
                    "max_duration_s": profile.max_duration_s,
                    "expected_overhead": profile.expected_overhead,
                    "tool_issues": self._probe_program_issues(profile),
                }
            )
        return info

    @staticmethod
    def _probe_program_issues(profile: profiles.Profile) -> List[str]:
        check = preflight.check_probe_programs(profile)
        if check.status == preflight.PASS:
            return []
        missing = check.details.get("missing", [])
        return [f"missing probe program: {path}" for path in missing]

    def tool_issues(self, profile: profiles.Profile) -> List[str]:
        report = preflight.PreflightReport()
        bpftrace = preflight.check_bpftrace(report, binary=self.options.bpftrace)
        issues = [] if bpftrace.status == preflight.PASS else [bpftrace.message]
        return issues + self._probe_program_issues(profile)

    # -- targets ----------------------------------------------------------

    def targets(self, minimum_threads: int = 2) -> List[Dict[str, Any]]:
        """Processes worth offering as a target.

        Everything here comes from /proc and nothing is executed.  Single
        threaded processes are filtered out by default because this tool has
        nothing to say about them, not because they are unsafe.
        """
        found: List[Dict[str, Any]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 1 or pid == os.getpid():
                continue
            threads = proc.thread_count(pid)
            if threads < minimum_threads:
                continue
            comm = proc.read_comm(pid)
            if comm is None:
                continue  # exited between listing and reading
            found.append(
                {
                    "pid": pid,
                    "comm": comm,
                    "threads": threads,
                    "cmdline": proc.read_cmdline(pid)[:8],
                    "exe": proc.read_exe(pid),
                }
            )
        found.sort(key=lambda item: (-item["threads"], item["pid"]))
        return found[:200]

    # -- runs -------------------------------------------------------------

    def runs(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for archive in sorted(self.out_dir.glob("performer-*.tgz")):
            try:
                with Bundle.open(archive) as bundle:
                    manifest = bundle.manifest
            except PerformerError:
                continue
            target = manifest.get("target") or {}
            entries.append(
                {
                    "run_id": manifest.get("run_id"),
                    "label": manifest.get("label"),
                    "status": manifest.get("status"),
                    "profile": manifest.get("profile"),
                    "started_at": manifest.get("started_at"),
                    "duration_s": manifest.get("actual_duration_s")
                    or manifest.get("duration_s"),
                    "comm": target.get("comm"),
                    "pid": target.get("pid"),
                    "bytes": archive.stat().st_size,
                }
            )
        entries.sort(key=lambda item: str(item.get("started_at")), reverse=True)
        return entries

    def archive_path(self, run_id: str) -> Path:
        """Resolve a run id to its archive, refusing anything outside out_dir.

        The id is already matched against ``RUN_ID_RE``, so this cannot
        currently be reached with a traversal -- which is exactly why it is
        here.  A future change to that pattern must not silently become a file
        disclosure bug.
        """
        validate_run_id(run_id)
        candidate = (self.out_dir / layout.archive_name(run_id)).resolve()
        if candidate.parent != self.out_dir:
            raise Rejected(HTTPStatus.BAD_REQUEST, "run id escapes the output directory")
        if not candidate.is_file():
            raise Rejected(HTTPStatus.NOT_FOUND, f"no bundle for run {run_id}")
        return candidate

    # -- jobs -------------------------------------------------------------

    def jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._jobs[key].to_dict() for key in reversed(self._order)]

    def job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise Rejected(HTTPStatus.NOT_FOUND, "no such job")
        return job.to_dict()

    def start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        pid = validate_pid(body.get("pid"))
        profile_name = validate_profile(body.get("profile", profiles.DEFAULT_PROFILE))
        if profile_name not in self.allowed_profiles():
            raise Rejected(
                HTTPStatus.FORBIDDEN,
                f"profile {profile_name!r} is not enabled on this daemon",
            )
        label = validate_label(body.get("label"))
        duration = validate_duration(body.get("duration_s", 60))
        tags = validate_tags(body.get("tags"))
        notes = validate_notes(body.get("notes"))

        profile = profiles.load(profile_name)
        if duration > profile.max_duration_s:
            raise Rejected(
                HTTPStatus.BAD_REQUEST,
                f"profile {profile_name!r} allows at most {profile.max_duration_s} s "
                f"(it costs {profile.expected_overhead})",
            )

        issues = self.tool_issues(profile)
        if issues:
            raise Rejected(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "missing collection tools: " + "; ".join(issues),
            )

        with self._lock:
            if self._current is not None:
                running = self._jobs[self._current]
                raise Rejected(
                    HTTPStatus.CONFLICT,
                    f"a measurement is already running (job {running.id}, pid {running.pid}); "
                    "this daemon runs one at a time on purpose",
                )
            job = Job(
                id=uuid.uuid4().hex[:12],
                pid=pid,
                profile=profile_name,
                label=label,
                duration_s=duration,
            )
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()
            self._current = job.id
            self._cancel.clear()

        thread = threading.Thread(
            target=self._run_job,
            args=(job, tags, notes),
            name=f"performer-job-{job.id}",
            daemon=True,
        )
        thread.start()
        return job.to_dict()

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise Rejected(HTTPStatus.NOT_FOUND, "no such job")
            if job.state not in ("queued", "running"):
                raise Rejected(HTTPStatus.CONFLICT, f"job is already {job.state}")
            self._cancel.set()
            job.lines.append("cancellation requested")
        return job.to_dict()

    def _trim(self) -> None:
        while len(self._order) > MAX_JOBS_REMEMBERED:
            oldest = self._order.pop(0)
            if oldest != self._current:
                self._jobs.pop(oldest, None)

    def _run_job(self, job: Job, tags: List[str], notes: str) -> None:
        with self._lock:
            job.state = "running"
            job.started_at = time.time()

        def printer(line: str) -> None:
            with self._lock:
                for part in str(line).splitlines() or [""]:
                    job.lines.append(part)
                # A preflight report is a page of text; a browser polling this
                # does not need the whole history, only the tail.
                if len(job.lines) > 400:
                    del job.lines[: len(job.lines) - 400]

        try:
            result = self._collect_fn(
                pid=job.pid,
                label=job.label,
                out_dir=self.out_dir,
                profile_name=job.profile,
                duration_s=job.duration_s,
                tags=tags,
                notes=notes,
                bpftrace=self.options.bpftrace,
                printer=printer,
                cancel=self._cancel,
            )
        except PerformerError as exc:
            with self._lock:
                job.state = "failed"
                job.error = str(exc)
                job.ended_at = time.time()
                self._current = None
            return
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                job.state = "failed"
                job.error = f"internal error: {exc}"
                job.lines.extend(traceback.format_exc().splitlines()[-5:])
                job.ended_at = time.time()
                self._current = None
            return

        with self._lock:
            job.state = "cancelled" if self._cancel.is_set() else "done"
            job.run_id = result.manifest.get("run_id")
            job.status = result.manifest.get("status")
            job.archive = str(result.archive) if result.archive else None
            job.ended_at = time.time()
            self._current = None

    # -- health -----------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        with self._lock:
            current = self._current
        report = preflight.PreflightReport()
        bpftrace = preflight.check_bpftrace(report, binary=self.options.bpftrace)
        tool_issues = [] if bpftrace.status == preflight.PASS else [bpftrace.message]
        return {
            "performer": __version__,
            "schema_version": layout.SCHEMA_VERSION,
            "uptime_s": round(time.time() - self.started_at, 1),
            "out_dir": str(self.out_dir),
            "profiles": self.allowed_profiles(),
            "busy": current is not None,
            "current_job": current,
            "can_collect": os.geteuid() == 0 and not tool_issues,
            "tool_issues": tool_issues,
            "viewer": layout.viewer_index() is not None,
        }


def _real_collect(
    *,
    pid: int,
    label: str,
    out_dir: Path,
    profile_name: str,
    duration_s: float,
    tags: List[str],
    notes: str,
    bpftrace: Optional[str],
    printer: Callable[[str], None],
    cancel: threading.Event,
) -> Any:
    """Bridge to :func:`collect.collect`, imported late so `--help` is fast."""
    from .collect import CollectOptions, collect

    return collect(
        CollectOptions(
            pid=pid,
            label=label,
            out_dir=out_dir,
            profile_name=profile_name,
            duration_s=duration_s,
            tags=tags,
            notes=notes,
            bpftrace=bpftrace,
        ),
        printer=printer,
        cancel=cancel,
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"performer/{__version__}"
    sys_version = ""  # do not advertise the Python build
    protocol_version = "HTTP/1.1"

    service: Service  # set on the server, read through self.server

    # -- plumbing ---------------------------------------------------------

    @property
    def _service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default logs to stderr on every request, which buries the one
        # line an operator actually needs (the token).
        pass

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            # Setting close_connection shuts the socket but does not say so:
            # without the header the client believes it may send another
            # request down a connection that is about to disappear.
            self.send_header("Connection", "close")
        # No CORS headers anywhere: a page on another origin must not be able
        # to read these responses, and silence is how that is said.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, document: Any) -> None:
        self._send(status, json.dumps(document, indent=2).encode("utf-8"), "application/json")

    def _error(self, status: HTTPStatus, message: str) -> None:
        # Every rejection ends the connection.  This is HTTP/1.1, so it is
        # kept alive by default -- and a request refused *before* its body was
        # read (an oversized Content-Length, a bad Host, a cross-origin POST)
        # leaves those bytes in the socket, where the next request on the same
        # connection would parse them as a request line.  Closing is the only
        # answer that cannot desync.
        self.close_connection = True
        self._json(status, {"error": message, "status": int(status)})

    # -- guards -----------------------------------------------------------

    def _check_host(self) -> None:
        """Refuse a request that did not address us as loopback.

        This is the DNS rebinding defence: an attacker's page can make a
        browser on this machine issue requests to a hostname that resolves to
        127.0.0.1, and the only thing distinguishing those from a real one is
        the Host header they carry.
        """
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name not in _ALLOWED_HOSTS:
            raise Rejected(HTTPStatus.MISDIRECTED_REQUEST, f"unexpected Host header {host!r}")

    def _check_origin(self) -> None:
        """Refuse a cross-origin write.

        Reads are already protected by the token and by the absence of CORS
        headers; this closes the remaining gap, where a form post from another
        origin reaches the handler before the browser checks anything.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return
        allowed = {
            f"http://127.0.0.1:{self.server.server_address[1]}",
            f"http://localhost:{self.server.server_address[1]}",
        }
        if origin not in allowed:
            raise Rejected(HTTPStatus.FORBIDDEN, f"cross-origin request from {origin!r}")

    def _authorise(self) -> None:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        self._service.check_token(token)

    def _body(self) -> Dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise Rejected(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError:
            raise Rejected(HTTPStatus.BAD_REQUEST, "malformed Content-Length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise Rejected(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"request body may be at most {MAX_BODY_BYTES} bytes",
            )
        raw = self.rfile.read(length) if length else b"{}"
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Rejected(HTTPStatus.BAD_REQUEST, f"body is not valid JSON: {exc}") from None
        if not isinstance(document, dict):
            raise Rejected(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return document

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            self._check_host()
            if method == "POST":
                self._check_origin()
            self._route(method, path)
        except Rejected as exc:
            self._error(exc.status, str(exc))
        except PerformerError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:  # pragma: no cover - client went away
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {exc}")

    def _route(self, method: str, path: str) -> None:
        service = self._service

        # The viewer is the only unauthenticated thing served, because it is a
        # static asset that contains no run data -- every byte of that comes
        # from the API below, which does require the token.
        if method == "GET" and path in ("/", "/index.html"):
            return self._serve_viewer()

        if method == "GET" and path == "/api/health":
            # The only unauthenticated endpoint, and deliberately
            # uninformative: it exists so a client can tell "the daemon is up"
            # from "the port is closed" before it has a token, and it says
            # nothing that is not already implied by the port being open.
            return self._json(HTTPStatus.OK, {"ok": True, "performer": __version__})

        self._authorise()

        if method == "GET":
            if path == "/api/status":
                return self._json(HTTPStatus.OK, service.health())
            if path == "/api/profiles":
                return self._json(HTTPStatus.OK, {"profiles": service.profile_info()})
            if path == "/api/targets":
                return self._json(HTTPStatus.OK, {"targets": service.targets()})
            if path == "/api/runs":
                return self._json(HTTPStatus.OK, {"runs": service.runs()})
            if path == "/api/jobs":
                return self._json(HTTPStatus.OK, {"jobs": service.jobs()})
            if path.startswith("/api/jobs/"):
                return self._json(HTTPStatus.OK, service.job(path[len("/api/jobs/") :]))
            if path.startswith("/api/runs/") and path.endswith("/bundle"):
                run_id = path[len("/api/runs/") : -len("/bundle")]
                return self._serve_bundle(service.archive_path(run_id))
            return self._error(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

        if method == "POST":
            if path == "/api/collect":
                return self._json(HTTPStatus.ACCEPTED, service.start(self._body()))
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path[len("/api/jobs/") : -len("/cancel")]
                return self._json(HTTPStatus.OK, service.cancel(job_id))
            return self._error(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

        self._error(HTTPStatus.METHOD_NOT_ALLOWED, f"{method} is not supported")

    def _serve_viewer(self) -> None:
        index = layout.viewer_index()
        if index is None or not self._service.options.serve_viewer:
            return self._error(
                HTTPStatus.NOT_FOUND,
                "no built viewer to serve; run 'npm run build' in viewer/, "
                "or use the API directly",
            )
        body = index.read_bytes()
        # The page is a single self-contained file, so the policy can be
        # absolute: no network of any kind, and the only script is inline.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_bundle(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{path.name}"'
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class DaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service: Service, port: int) -> None:
        self.service = service
        super().__init__((BIND_HOST, port), Handler)


def serve(options: DaemonOptions, *, printer: Callable[[str], None] = print) -> int:
    service = Service(options)
    try:
        server = DaemonServer(service, options.port)
    except OSError as exc:
        raise PerformerError(
            f"could not bind {BIND_HOST}:{options.port}: {exc}"
        ) from exc

    port = server.server_address[1]
    url = f"http://{BIND_HOST}:{port}/"
    printer(f"performer daemon on {url}")
    printer(f"  output directory: {service.out_dir}")
    printer(f"  profiles:         {', '.join(service.allowed_profiles()) or 'none'}")
    if os.geteuid() != 0:
        printer("  note: not running as root, so collection will fail preflight")
    printer("")
    # Printed once, to a terminal, and never written to disk. Anyone who can
    # read this line can start a privileged measurement, which is the point.
    printer(f"  token: {service.token}")
    printer(f"  open:  {url}?token={service.token}")
    printer("")
    printer("Ctrl-C to stop.")

    if options.open_browser:  # pragma: no cover - needs a desktop
        import webbrowser

        webbrowser.open(f"{url}?token={service.token}")

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        printer("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0
