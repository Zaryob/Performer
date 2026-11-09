"""Local, acknowledged start/stop control for a prepared capture.

The Unix socket is accessible only to the target's owner and root. Requests
contain no code, paths to execute or probe selection. Probes stay gated while
GDB inspects the stopped target. One connection carries one JSON line.
"""
from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Set

from .errors import PerformerError
from .window import clock_now_ns

MAX_MESSAGE = 4096
CLIENT_TIMEOUT_S = 15.0
STOP_LEAD_NS = 10_000_000
ACK_GRACE_NS = 2_000_000_000


def _read_line(connection: socket.socket) -> Dict[str, Any]:
    data = bytearray()
    while b"\n" not in data:
        block = connection.recv(min(1024, MAX_MESSAGE + 1 - len(data)))
        if not block:
            raise PerformerError("capture control connection closed without a response")
        data.extend(block)
        if len(data) > MAX_MESSAGE:
            raise PerformerError("capture control message is too large")
    line, trailing = bytes(data).split(b"\n", 1)
    if trailing.strip():
        raise PerformerError("capture control accepts one request per connection")
    try:
        document = json.loads(line)
    except (ValueError, UnicodeError) as exc:
        raise PerformerError("capture control message is not valid JSON") from exc
    if not isinstance(document, dict):
        raise PerformerError("capture control message must be an object")
    return document


def request(path: Path, command: str, *, pid: Optional[int] = None,
            timeout: float = CLIENT_TIMEOUT_S) -> Dict[str, Any]:
    if command not in {"status", "start", "stop"}:
        raise PerformerError("capture control command must be status, start or stop")
    payload: Dict[str, Any] = {"command": command, "sent_ns": str(clock_now_ns())}
    if pid is not None:
        payload["pid"] = pid
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall(json.dumps(payload).encode() + b"\n")
            response = _read_line(connection)
    except OSError as exc:
        raise PerformerError(f"cannot {command} capture through {path}: {exc}") from exc
    if not response.get("ok"):
        raise PerformerError(str(response.get("error", "capture control request failed")))
    if pid is not None and response.get("pid") != pid:
        raise PerformerError("capture control response belongs to a different target PID")
    return response


class CaptureControl:
    def __init__(self, path: Path, pid: int) -> None:
        self.path, self.pid = Path(path).absolute(), pid
        self.state = "preparing"
        self.socket: Optional[socket.socket] = None
        self.inode: Optional[int] = None
        self.pending: Set[socket.socket] = set()
        self.metadata: Dict[str, Any] = {"mode": "local debugger control", "stop_lead_ns": STOP_LEAD_NS}

    def open(self) -> None:
        # Never unlink an existing socket or file. Binding is exclusive and
        # cleanup later verifies the inode, so another session is not removed.
        if len(os.fsencode(self.path)) >= 104:
            raise PerformerError("capture control socket path is too long; use a short path in /tmp")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_mask = os.umask(0o177)
            try:
                listener.bind(str(self.path))
            finally:
                os.umask(old_mask)
            self.inode = self.path.lstat().st_ino
            os.chmod(self.path, 0o600)
            if os.geteuid() == 0:
                owner = Path(f"/proc/{self.pid}").stat()
                os.chown(self.path, owner.st_uid, owner.st_gid)
            listener.listen(4)
            listener.settimeout(0.01)
            self.socket = listener
            self.state = "ready"
        except OSError as exc:
            listener.close()
            self.close()
            raise PerformerError(f"cannot create capture control socket {self.path}: {exc}") from exc

    def receive(self) -> Optional[Tuple[socket.socket, str]]:
        if self.socket is None:
            raise PerformerError("capture control socket is not open")
        try:
            connection, _address = self.socket.accept()
        except socket.timeout:
            return None
        self.pending.add(connection)
        try:
            connection.settimeout(2.0)
            document = _read_line(connection)
            if set(document) - {"command", "pid", "sent_ns"}:
                raise PerformerError("unknown capture control request fields")
            command = document.get("command")
            if not isinstance(command, str):
                raise PerformerError("capture control command must be a string")
            if "pid" in document and (type(document["pid"]) is not int or document["pid"] != self.pid):
                raise PerformerError("capture control target PID does not match")
            if command == "status":
                self.reply(connection)
                return None
            required_state = {"start": "ready", "stop": "recording"}.get(command)
            if required_state is None or self.state != required_state:
                raise PerformerError(f"cannot {command!r} capture in state {self.state!r}")
            self.metadata[f"{command}_request_ns"] = str(clock_now_ns())
            sent = document.get("sent_ns")
            if isinstance(sent, str) and sent.isdecimal() and 0 < len(sent) <= 20:
                self.metadata[f"{command}_client_ns"] = sent
            return connection, command
        except (PerformerError, OSError) as exc:
            self.reply(connection, error=str(exc))
            return None

    def reply(self, connection: socket.socket, *, error: Optional[str] = None,
              **details: Any) -> bool:
        response = {"ok": error is None, "protocol": 1, "state": self.state, "pid": self.pid, **details}
        if error is not None:
            response["error"] = error
        try:
            connection.sendall(json.dumps(response).encode() + b"\n")
            return True
        except OSError:
            return False
        finally:
            self.pending.discard(connection)
            connection.close()

    def close(self) -> None:
        for connection in tuple(self.pending):
            self.reply(connection, error="capture ended before the request completed")
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.inode is not None:
            try:
                info = self.path.lstat()
                if info.st_ino == self.inode and stat.S_ISSOCK(info.st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.inode = None
