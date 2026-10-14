"""Thread-safe load runner, inbound simulator, capture and aggregate metrics."""

from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import ssl
import threading
import tempfile
import time
import traceback

from .messages import HL7, build_ack, inspect_ack, render_template
from .transport import Client, Endpoint, FrameReader, TransportError, frame


def write_json(path, data):
    """Replace completed files atomically rather than truncating an existing report/profile."""
    destination = Path(path).absolute()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".mirthquake-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


@dataclass(frozen=True)
class RunOptions:
    count: int = 1                 # zero = run until stopped / duration expires
    rate: float = 5.0             # aggregate messages per second, not per worker
    workers: int = 1
    duration: float = 0.0
    persistent: bool = True
    wait_response: bool = True
    check_ack: bool = True
    correlate: bool = True
    refresh: bool = True
    validate_hl7: bool = True
    payload_type: str = "HL7"
    retries: int = 0
    retry_delay: float = 1.0
    stop_on_error: bool = False

    def validate(self):
        if self.count < 0 or self.count > 100_000_000:
            raise ValueError("Message count must be 0 (continuous) to 100,000,000.")
        if not math.isfinite(self.rate) or not 0 < self.rate <= 100_000:
            raise ValueError("Rate must be greater than 0 and at most 100,000 messages/second.")
        if not 1 <= self.workers <= 128:
            raise ValueError("Workers must be between 1 and 128.")
        if not math.isfinite(self.duration) or self.duration < 0:
            raise ValueError("Duration must be finite and non-negative.")
        if not 0 <= self.retries <= 10 or not math.isfinite(self.retry_delay) or self.retry_delay < 0:
            raise ValueError("Retries must be 0–10 and retry delay finite and non-negative.")
        if self.payload_type not in ("HL7", "XML", "Raw"):
            raise ValueError("Payload type must be HL7, XML or Raw.")


class Capture:
    """Bounded detailed history; lifetime counters do not depend on retained history."""
    def __init__(self, limit=1000, callback=None):
        self.lock = threading.RLock()
        self.records = deque(maxlen=limit)
        self.events = deque(maxlen=limit)
        self.latencies = deque(maxlen=10000)
        self.callback = callback or (lambda event: None)
        self.counters = Counter(completed=0, success=0, failed=0, attempts=0)
        self.started = time.monotonic()
        self.finished = None
        self.metadata = {}
        self.latency_total = 0.0
        self.latency_count = 0

    def emit(self, kind, **data):
        event = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        with self.lock:
            self.events.append(event)
            if kind == "error":
                self.counters["diagnostic_errors"] += 1
        self.callback(event)

    def record(self, record):
        with self.lock:
            self.records.append(record)
            self.counters["completed"] += 1
            self.counters["success" if record["success"] else "failed"] += 1
            self.counters["attempts"] += record.get("attempts", 1)
            if record.get("ack"):
                self.counters[record["ack"]["code"]] += 1
            if record.get("elapsed_ms") is not None:
                self.latencies.append(record["elapsed_ms"])
                self.latency_total += record["elapsed_ms"]
                self.latency_count += 1
        self.emit("record", record=record)

    def summary(self):
        with self.lock:
            elapsed = max((self.finished or time.monotonic()) - self.started, 0.001)
            latency = sorted(self.latencies)
            def percentile(p):
                return round(latency[min(len(latency) - 1, math.ceil(p * len(latency)) - 1)], 3) if latency else 0
            return {**dict(self.counters), "elapsed_seconds": round(elapsed, 3),
                    "completed_per_second": round(self.counters["completed"] / elapsed, 3),
                    "mean_ms": round(self.latency_total / self.latency_count, 3) if self.latency_count else 0,
                    "p50_ms": percentile(.50), "p95_ms": percentile(.95), "p99_ms": percentile(.99),
                    "percentile_sample_size": len(latency), "retained_records": len(self.records),
                    "history_limit": self.records.maxlen}

    def finish(self):
        with self.lock:
            self.finished = time.monotonic()
        self.emit("finished", summary=self.summary())

    def export(self, path, include_payloads=False):
        def clean(value):
            if isinstance(value, bytes):
                return {"hex": value.hex(), "bytes": len(value)} if include_payloads else {"bytes": len(value), "omitted": True}
            if isinstance(value, dict):
                # IDs, errors and tracebacks may also contain identifying payload values.
                sensitive = {"payload", "wire", "outbound", "inbound", "partial", "ack", "error", "traceback", "issues", "control_id"}
                return {k: ("[omitted; enable sensitive diagnostics export]" if not include_payloads and k in sensitive else clean(v))
                        for k, v in value.items()}
            if isinstance(value, (list, tuple, deque)):
                return [clean(v) for v in value]
            return value
        with self.lock:
            report = {"tool": "MirthQuake", "schema_version": 1, "metadata": clean(self.metadata),
                      "summary": self.summary(), "records": clean(list(self.records)), "events": clean(list(self.events)),
                      "sensitive_diagnostics_included": include_payloads}
        write_json(path, report)


class LoadRunner:
    def __init__(self, endpoint, options, template, capture=None):
        endpoint.validate()
        options.validate()
        if not template.strip():
            raise ValueError("Message is empty. Load a sample, paste a message or open a file.")
        preview = render_template(template, hl7=options.payload_type == "HL7", refresh=False)
        if options.validate_hl7 and options.payload_type == "HL7":
            errors = [i for i in HL7(preview).validate() if i.severity == "ERROR"]
            if errors:
                raise ValueError("HL7 validation failed: " + "; ".join(f"{i.location}: {i.description}" for i in errors))
        # Check refresh compatibility and size/encoding before starting network work.
        preview = render_template(template, hl7=options.payload_type == "HL7", refresh=options.refresh)
        try:
            preview_bytes = preview.encode(endpoint.encoding)
        except UnicodeError as exc:
            raise TransportError("encode", str(exc), "Select an encoding supported by the peer and check non-ASCII characters.") from exc
        frame(preview_bytes, endpoint)
        self.endpoint, self.options, self.template = endpoint, options, template
        self.capture = capture or Capture()
        self.capture.metadata = {"mode": "send", "endpoint": asdict(endpoint), "options": asdict(options)}
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()
        self.sequence = 0
        self.start_time = 0
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Runner already started.")
        self.thread = threading.Thread(target=self.run, name="mq-runner", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.close()

    def run(self):
        self.start_time = time.monotonic()
        self.capture.started = self.start_time
        self.capture.emit("started", message="Outbound test started.")
        if self.options.retries:
            self.capture.emit("warning", message="Retries are enabled. A timed-out message may already have been delivered; duplicates are possible. Retries reuse the same control ID.")
        if not self.options.wait_response:
            self.capture.emit("warning", message="Response reads are disabled. Success only means sendall completed, not that the receiver accepted the message.")
        workers = [threading.Thread(target=self._worker, args=(i + 1,), daemon=True) for i in range(self.options.workers)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            self.stop()
            self.capture.finish()

    def _reserve(self):
        with self.lock:
            if self.stop_event.is_set() or (self.options.count and self.sequence >= self.options.count):
                return None
            self.sequence += 1
            seq = self.sequence
            due = self.start_time + (seq - 1) / self.options.rate
        if self.options.duration and due >= self.start_time + self.options.duration:
            return None
        if self.stop_event.wait(max(0, due - time.monotonic())):
            return None
        if self.options.duration and time.monotonic() - self.start_time >= self.options.duration:
            return None
        return seq

    def _worker(self, worker):
        client = None
        try:
            while not self.stop_event.is_set():
                seq = self._reserve()
                if seq is None:
                    break
                record = {"sequence": seq, "worker": worker, "time": datetime.now(timezone.utc).isoformat(),
                          "success": False, "attempts": 0, "outbound": b"", "inbound": b""}
                started = time.monotonic()
                try:
                    text = render_template(self.template, seq, self.options.refresh, self.options.payload_type == "HL7")
                    record["control_id"] = HL7(text).control_id if self.options.payload_type == "HL7" else ""
                    # Encoding is performed here as well so the record retains outbound bytes even if connect fails.
                    record["outbound"] = text.encode(self.endpoint.encoding)
                    for attempt in range(self.options.retries + 1):
                        if self.stop_event.is_set():
                            break
                        record["attempts"] = attempt + 1
                        try:
                            if client is None:
                                client = Client(self.endpoint, lambda kind, **data: self.capture.emit(kind, sequence=seq, worker=worker, **data))
                                with self.lock:
                                    self.clients.add(client)
                            record["inbound"] = client.send(text, self.options.wait_response, self.stop_event)
                            if self.options.wait_response:
                                try:
                                    response = record["inbound"].decode(self.endpoint.encoding, errors="strict")
                                except UnicodeError as exc:
                                    raise TransportError("decode", str(exc), "Check peer encoding and MSH-18. Raw response bytes are retained.") from exc
                                if self.options.check_ack and self.options.payload_type == "HL7":
                                    record["ack"] = inspect_ack(response, record["control_id"], self.options.correlate)
                                    if not record["ack"]["accepted"]:
                                        raise TransportError("negative ACK", f"{record['ack']['code']}: {record['ack']['detail']} {record['ack']['errors']}",
                                                             "Inspect MSA/ERR and the Mirth channel's source/transformer/destination errors. A transport ACK does not prove downstream delivery.")
                            record["success"] = True
                            break
                        except Exception as exc:
                            client.close()
                            with self.lock:
                                self.clients.discard(client)
                            client = None
                            self.capture.emit("attempt_error", sequence=seq, worker=worker, attempt=attempt + 1,
                                              error=str(exc), stage=getattr(exc, "stage", "ACK / exchange"),
                                              partial=getattr(exc, "partial", b""))
                            if attempt == self.options.retries or self.stop_event.is_set():
                                raise
                            if self.stop_event.wait(self.options.retry_delay):
                                raise TransportError("cancelled", "Stopped during retry delay.")
                    if not record["success"]:
                        raise TransportError("cancelled", "Stopped before exchange completed.")
                except Exception as exc:
                    record.update(error=str(exc), stage=getattr(exc, "stage", "message / ACK"),
                                  partial=getattr(exc, "partial", b""), traceback=traceback.format_exc())
                record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
                self.capture.record(record)
                if client is not None and (not self.options.persistent or not self.options.wait_response):
                    # Fire-and-forget cannot safely reuse sockets if the peer sends unread ACKs.
                    client.close()
                    with self.lock:
                        self.clients.discard(client)
                    client = None
                if not record["success"] and self.options.stop_on_error:
                    self.stop()
        finally:
            if client is not None:
                client.close()
                with self.lock:
                    self.clients.discard(client)


@dataclass(frozen=True)
class ListenerOptions:
    ack_code: str = "AA"          # AA/AE/AR/CA/CE/CR, none, close
    delay: float = 0.0
    error_text: str = "Synthetic test rejection"
    validate_hl7: bool = True
    max_clients: int = 32

    def validate(self):
        if self.ack_code not in ("AA", "AE", "AR", "CA", "CE", "CR", "none", "close"):
            raise ValueError("Unsupported listener ACK mode.")
        if not math.isfinite(self.delay) or self.delay < 0:
            raise ValueError("ACK delay must be finite and non-negative.")
        if not 1 <= self.max_clients <= 128:
            raise ValueError("Listener client limit must be between 1 and 128.")


class Listener:
    def __init__(self, endpoint, options=None, capture=None):
        endpoint.validate()
        self.options = options or ListenerOptions()
        self.options.validate()
        if endpoint.framing == "raw":
            raise ValueError("The inbound listener needs a message delimiter. Choose MLLP, XML or custom framing; raw TCP has no message boundary.")
        self.endpoint = endpoint
        self.capture = capture or Capture()
        self.capture.metadata = {"mode": "listen", "endpoint": asdict(endpoint), "options": asdict(self.options)}
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.connections = set()
        self.handlers = set()
        self.server = None
        self.thread = None
        self.sequence = 0
        self.context = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Listener already started.")
        if self.endpoint.tls:
            if not self.endpoint.cert_file or not self.endpoint.key_file:
                raise ValueError("TLS listener requires a certificate file and private key file.")
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(self.endpoint.cert_file, self.endpoint.key_file)
        # Bind synchronously so address-in-use errors can be presented immediately.
        infos = socket.getaddrinfo(self.endpoint.host, self.endpoint.port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        last_error = None
        for family, socktype, proto, _, address in infos:
            server = socket.socket(family, socktype, proto)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(address)
                server.listen(self.options.max_clients)
                server.settimeout(.2)
                self.server = server
                break
            except OSError as exc:
                server.close()
                last_error = exc
        if self.server is None:
            raise TransportError("listen / bind", str(last_error), "Choose a free port and a local bind address. Mirth and MirthQuake cannot listen on the same address/port.")
        self.capture.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True, name="mq-listener")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.server is not None:
            self.server.close()
        with self.lock:
            connections = list(self.connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def run(self):
        self.capture.emit("listening", message=f"Listening on {self.endpoint.host}:{self.endpoint.port}. Point Mirth's TCP Sender here.")
        try:
            while not self.stop_event.is_set():
                try:
                    sock, address = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                with self.lock:
                    if len(self.connections) >= self.options.max_clients:
                        sock.close()
                        self.capture.emit("warning", message="Listener client limit reached; connection rejected.")
                        continue
                    self.connections.add(sock)
                    handler = threading.Thread(target=self._handle, args=(sock, address), daemon=True)
                    self.handlers.add(handler)
                handler.start()
        except Exception as exc:
            self.capture.emit("error", error=str(exc), traceback=traceback.format_exc())
        finally:
            self.stop()
            with self.lock:
                handlers = list(self.handlers)
            for handler in handlers:
                handler.join(self.endpoint.connect_timeout + 1)
            self.capture.finish()

    def _handle(self, sock, address):
        original = sock
        peer = str(address)
        try:
            if self.context is not None:
                sock.settimeout(self.endpoint.connect_timeout)
                sock = self.context.wrap_socket(sock, server_side=True)
                with self.lock:
                    self.connections.discard(original)
                    self.connections.add(sock)
            self.capture.emit("connection", peer=peer, message="Inbound connection accepted.")
            reader = FrameReader(self.endpoint)
            while not self.stop_event.is_set():
                try:
                    payload = reader.receive(sock, self.stop_event, idle_ok=True)
                except EOFError:
                    break
                if payload is None:
                    continue
                with self.lock:
                    self.sequence += 1
                    seq = self.sequence
                started = time.monotonic()
                record = {"sequence": seq, "peer": peer, "time": datetime.now(timezone.utc).isoformat(),
                          "inbound": payload, "outbound": b"", "success": False, "attempts": 1}
                self.capture.emit("inbound", sequence=seq, peer=peer, payload=payload, wire=frame(payload, self.endpoint))
                try:
                    text = payload.decode(self.endpoint.encoding, errors="strict")
                    msg = HL7(text)
                    record["control_id"] = msg.control_id
                    issues = msg.validate() if self.options.validate_hl7 else []
                    record["issues"] = [asdict(i) for i in issues]
                    invalid = any(i.severity == "ERROR" for i in issues)
                    code = "AE" if invalid and self.options.ack_code in ("AA", "CA") else self.options.ack_code
                    if self.stop_event.wait(self.options.delay):
                        break
                    if code == "close":
                        record["error"] = "Deliberately closed connection without ACK."
                    elif code == "none":
                        record["success"] = not invalid
                        record["note"] = "No ACK sent (timeout simulation)."
                    else:
                        error = "; ".join(i.description for i in issues if i.severity == "ERROR") if invalid else (self.options.error_text if code not in ("AA", "CA") else "")
                        ack = build_ack(text, code, error)
                        record["outbound"] = ack.encode(self.endpoint.encoding)
                        sock.settimeout(self.endpoint.response_timeout)
                        sock.sendall(frame(record["outbound"], self.endpoint))
                        record["ack"] = inspect_ack(ack, correlate=False)
                        record["success"] = record["ack"]["accepted"] and not invalid
                        self.capture.emit("outbound", sequence=seq, peer=peer, payload=record["outbound"], wire=frame(record["outbound"], self.endpoint))
                except Exception as exc:
                    record.update(error=str(exc), traceback=traceback.format_exc())
                record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
                self.capture.record(record)
                if self.options.ack_code == "close":
                    break
        except Exception as exc:
            if not self.stop_event.is_set():
                with self.lock:
                    self.sequence += 1
                    seq = self.sequence
                self.capture.record({"sequence": seq, "peer": peer, "time": datetime.now(timezone.utc).isoformat(),
                                     "success": False, "attempts": 0, "inbound": b"", "partial": getattr(exc, "partial", b""), "outbound": b"",
                                     "stage": getattr(exc, "stage", "listener"), "error": str(exc), "traceback": traceback.format_exc()})
                self.capture.emit("error", peer=peer, error=str(exc), stage=getattr(exc, "stage", "listener"),
                                  partial=getattr(exc, "partial", b""), traceback=traceback.format_exc())
        finally:
            sock.close()
            with self.lock:
                self.connections.discard(original)
                self.connections.discard(sock)
                self.handlers.discard(threading.current_thread())


def save_profile(path, endpoint, options, listener_options, template, listener_endpoint=None):
    endpoint.validate()
    options.validate()
    listener_options.validate()
    data = {"schema_version": 1, "endpoint": asdict(endpoint), "run": asdict(options),
            "listener": asdict(listener_options), "template": template}
    if listener_endpoint is not None:
        Endpoint(host=listener_endpoint["host"], port=listener_endpoint["port"]).validate()
        data["listener_endpoint"] = listener_endpoint
    write_json(path, data)


def load_profile(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported profile version.")
    endpoint = Endpoint(**data["endpoint"])
    options = RunOptions(**data["run"])
    listener = ListenerOptions(**data.get("listener", {}))
    template = data["template"]
    if not isinstance(template, str):
        raise ValueError("Profile template must be text.")
    endpoint.validate()
    options.validate()
    listener.validate()
    if "listener_endpoint" in data:
        Endpoint(**data["listener_endpoint"]).validate()
    return endpoint, options, listener, template
