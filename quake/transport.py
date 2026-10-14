"""Bounded, buffered TCP framing with actionable transport diagnostics."""

from dataclasses import dataclass
import codecs
import math
import socket
import ssl
import time


@dataclass(frozen=True)
class Endpoint:
    host: str = "127.0.0.1"
    port: int = 6661
    framing: str = "mllp"
    encoding: str = "utf-8"
    connect_timeout: float = 5.0
    response_timeout: float = 10.0
    max_bytes: int = 4 * 1024 * 1024
    tls: bool = False
    verify_tls: bool = True
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    server_name: str = ""
    prefix_hex: str = "0b"
    suffix_hex: str = "1c0d"

    def validate(self):
        if not self.host.strip():
            raise ValueError("Host / bind address is required.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        if any(not math.isfinite(t) or t <= 0 for t in (self.connect_timeout, self.response_timeout)):
            raise ValueError("Timeouts must be finite positive seconds.")
        if not 1 <= self.max_bytes <= 64 * 1024 * 1024:
            raise ValueError("Maximum payload must be 1 byte to 64 MiB.")
        codecs.lookup(self.encoding)
        framing_bytes(self)
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("TLS certificate and private key must be supplied together.")


def framing_bytes(endpoint):
    if endpoint.framing == "mllp":
        return b"\x0b", b"\x1c\x0d"
    if endpoint.framing == "xml":
        return b"\x00\x00", b"\x00"
    if endpoint.framing == "raw":
        return b"", b""
    if endpoint.framing == "custom":
        prefix, suffix = bytes.fromhex(endpoint.prefix_hex), bytes.fromhex(endpoint.suffix_hex)
        if not suffix or len(prefix) > 64 or len(suffix) > 64:
            raise ValueError("Custom framing requires an end delimiter; delimiters must be at most 64 bytes.")
        return prefix, suffix
    raise ValueError("Framing must be mllp, xml, raw or custom.")


def frame(payload, endpoint):
    prefix, suffix = framing_bytes(endpoint)
    if len(payload) > endpoint.max_bytes:
        raise ValueError(f"Payload exceeds configured {endpoint.max_bytes}-byte limit.")
    return prefix + payload + suffix


class TransportError(Exception):
    def __init__(self, stage, detail, hint="", partial=b""):
        self.stage, self.detail, self.hint, self.partial = stage, detail, hint, partial
        super().__init__(f"{stage}: {detail}" + (f" Hint: {hint}" if hint else ""))


class FrameReader:
    """Retains coalesced frames; reads fragmented delimiters and payloads completely."""
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.buffer = bytearray()

    def receive(self, sock, stop=None, idle_ok=False):
        prefix, suffix = framing_bytes(self.endpoint)
        deadline = time.monotonic() + self.endpoint.response_timeout
        while True:
            if stop is not None and stop.is_set():
                raise TransportError("cancelled", "Operation stopped.", partial=bytes(self.buffer))
            if suffix:
                if prefix and self.buffer:
                    n = min(len(prefix), len(self.buffer))
                    if self.buffer[:n] != prefix[:n]:
                        raise TransportError("framing", "Response does not start with configured framing bytes.",
                                             "Match Mirth's TCP transmission mode to MLLP (0B … 1C 0D), or select the correct framing.", bytes(self.buffer))
                end = self.buffer.find(suffix, len(prefix))
                if end != -1:
                    if end - len(prefix) > self.endpoint.max_bytes:
                        raise TransportError("receive", "Frame exceeds maximum payload size.", partial=bytes(self.buffer[:256]))
                    data = bytes(self.buffer[len(prefix):end])
                    del self.buffer[:end + len(suffix)]
                    return data
            if len(self.buffer) > self.endpoint.max_bytes + len(prefix) + len(suffix):
                raise TransportError("receive", "Frame exceeds maximum payload size.",
                                     "Check the end delimiter and increase the limit only for expected large messages.", bytes(self.buffer[:256]))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not suffix and self.buffer:
                    data = bytes(self.buffer)
                    self.buffer.clear()
                    return data
                if idle_ok and not self.buffer:
                    return None
                raise TransportError("receive timeout", f"No complete response within {self.endpoint.response_timeout:g}s ({len(self.buffer)} bytes received).",
                                     "Check Mirth response/auto-ACK settings, channel errors, and the end delimiter. Increase the response timeout for slow channels.", bytes(self.buffer))
            try:
                sock.settimeout(min(0.2, remaining))
                chunk = sock.recv(min(65536, self.endpoint.max_bytes + 128))
            except socket.timeout:
                continue
            except OSError as exc:
                raise TransportError("receive", str(exc), "The peer may have reset the connection; check its channel/server log.", bytes(self.buffer)) from exc
            if not chunk:
                if not suffix and self.buffer:
                    data = bytes(self.buffer)
                    self.buffer.clear()
                    return data
                if idle_ok and not self.buffer:
                    raise EOFError("Peer closed connection.")
                raise TransportError("receive", "Peer closed connection before a complete response arrived.",
                                     "Check keep-alive and ACK settings, and whether framing matches the remote interface.", bytes(self.buffer))
            self.buffer.extend(chunk)


class Client:
    def __init__(self, endpoint, emit=None):
        endpoint.validate()
        self.endpoint, self.emit = endpoint, emit or (lambda *args, **kwargs: None)
        self.sock = None
        self.reader = FrameReader(endpoint)

    def open(self):
        e = self.endpoint
        started = time.monotonic()
        sock = None
        stage = "connect"
        try:
            sock = socket.create_connection((e.host, e.port), e.connect_timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if e.tls:
                stage = "TLS"
                context = ssl.create_default_context(cafile=e.ca_file or None)
                if not e.verify_tls:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                if e.cert_file:
                    context.load_cert_chain(e.cert_file, e.key_file)
                sock = context.wrap_socket(sock, server_hostname=e.server_name or e.host)
            self.sock = sock
            self.emit("connection", peer=str(sock.getpeername()), local=str(sock.getsockname()),
                      connect_ms=round((time.monotonic() - started) * 1000, 3),
                      tls=str(sock.cipher()) if e.tls else "off")
        except Exception as exc:
            if sock is not None:
                sock.close()
            hint = ("Check certificate trust, hostname, TLS version, and client certificate requirements. Do not disable verification in production."
                    if stage == "TLS" else "Check hostname/DNS, port, firewall, deployed Mirth channel, and TCP Listener bind address.")
            raise TransportError(stage, str(exc), hint) from exc

    def send(self, text, wait=True, stop=None):
        if stop is not None and stop.is_set():
            raise TransportError("cancelled", "Stopped before opening connection.")
        if self.sock is None:
            self.open()
        if stop is not None and stop.is_set():
            self.close()
            raise TransportError("cancelled", "Stopped during connection setup; no message sent.")
        sock = self.sock
        if sock is None:
            raise TransportError("cancelled", "Connection closed before sending.")
        try:
            payload = text.encode(self.endpoint.encoding, errors="strict")
        except UnicodeError as exc:
            raise TransportError("encode", str(exc), "Select an encoding supported by both interfaces; check MSH-18 and non-ASCII characters.") from exc
        wire = frame(payload, self.endpoint)
        self.emit("outbound", payload=payload, wire=wire)
        try:
            sock.settimeout(self.endpoint.response_timeout)
            sock.sendall(wire)
        except OSError as exc:
            raise TransportError("send", str(exc), "Peer disconnected or stopped reading. Delivery is uncertain; retrying may create duplicates.") from exc
        if not wait:
            return b""
        response = self.reader.receive(sock, stop)
        self.emit("inbound", payload=response, wire=frame(response, self.endpoint))
        return response

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


def hex_dump(data, limit=2048):
    lines = []
    for offset in range(0, min(len(data), limit), 16):
        chunk = data[offset:offset + 16]
        lines.append(f"{offset:08x}  {chunk.hex(' '):47}  " + "".join(chr(c) if 32 <= c < 127 else "." for c in chunk))
    if len(data) > limit:
        lines.append(f"… {len(data) - limit} more bytes (full payload is retained in the record)")
    return "\n".join(lines)
