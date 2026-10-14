import json
import io
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hl7 import Connection, Message
from quake.cli import main as cli_main
from quake.engine import Capture, Listener, ListenerOptions, LoadRunner, RunOptions, load_profile, save_profile, write_json
from quake.messages import HL7, SAMPLES, build_ack, inspect_ack, normalize, render_template
from quake.transport import Client, Endpoint, FrameReader, TransportError, frame


def message(sequence=1):
    return render_template(next(iter(SAMPLES.values())), sequence)


class Peer:
    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.errors = []
        def serve():
            try:
                conn, _ = self.sock.accept()
                with conn:
                    conn.settimeout(2)
                    handler(conn)
            except Exception as exc:
                self.errors.append(exc)
            finally:
                self.sock.close()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()
        self.thread.join(3)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MessagesTest(unittest.TestCase):
    def test_samples_validate(self):
        for sample in list(SAMPLES.values())[:3]:
            self.assertFalse([i for i in HL7(render_template(sample)).validate() if i.severity == "ERROR"])

    def test_normalize_and_unframe(self):
        self.assertEqual(normalize("\x0bMSH|x\r\x1c\r"), "MSH|x\r")
        self.assertEqual(normalize("PID|a|  \r\nOBX|b|\n"), "PID|a|  \rOBX|b|\r")
        self.assertEqual(normalize("\x0bMSH|x\n\x1c\n"), "MSH|x\r")

    def test_msh_numbering(self):
        parsed = HL7(message())
        self.assertEqual(parsed.field("MSH", 1), "|")
        self.assertEqual(parsed.field("MSH", 2), "^~\\&")
        self.assertEqual(parsed.field("MSH", 9), "ADT^A01")
        self.assertEqual(dict(parsed.fields())["MSH[1]-10"], parsed.control_id)
        self.assertEqual(parsed.field("PID", 1), "1")

    def test_ack_swap_and_correlate(self):
        parsed = HL7(message())
        ack = build_ack(parsed.text)
        self.assertEqual(HL7(ack).field("MSH", 5), parsed.field("MSH", 3))
        self.assertTrue(inspect_ack(ack, parsed.control_id)["accepted"])

    def test_custom_delimiter_ack(self):
        text = message().replace("|", "!")
        ack = build_ack(text, "AE", "bad!value\rMSA|AA")
        self.assertEqual(HL7(ack).field("MSA", 2), HL7(text).control_id)
        self.assertFalse(inspect_ack(ack)["accepted"])
        self.assertEqual(len(HL7(ack).segments), 2)

    def test_ack_negative_and_mismatch(self):
        ack = build_ack(message(), "AR", "Rejected")
        self.assertFalse(inspect_ack(ack)["accepted"])
        with self.assertRaisesRegex(ValueError, "correlation"):
            inspect_ack(ack, "wrong-id")
        with self.assertRaisesRegex(ValueError, "MSA-1"):
            inspect_ack("not an ack")

    def test_validation_errors(self):
        self.assertTrue(HL7("PID|a").validate())
        self.assertTrue(any(i.severity == "ERROR" for i in HL7("MSH|bad").validate()))
        self.assertTrue(any("Multiple MSH" in i.description for i in HL7(message() + message()).validate()))

    def test_tokens_refresh(self):
        original = message()
        new = render_template(original, refresh=True)
        self.assertNotEqual(HL7(original).control_id, HL7(new).control_id)
        self.assertIn("TEST27", render_template(next(iter(SAMPLES.values())), 27))
        with self.assertRaisesRegex(ValueError, "Unknown template"):
            render_template("{{bad}}")

    def test_legacy_message_separators(self):
        msg = Message()
        msg.AddLine("MSH|test")
        msg.AddLine("PID|one")
        self.assertEqual(msg.Render(), "\x0bMSH|test\rPID|one\r\x1c\r")
        self.assertEqual(Message("<x/>", "XML").Render(), "\x00\x00<x/>\x00")


class TransportTest(unittest.TestCase):
    def test_fragmented_large_ack(self):
        def handler(sock):
            request = FrameReader(Endpoint()).receive(sock)
            ack = build_ack(request.decode(), "AA", "x" * 4000).encode() + b"Z" * 5000
            wire = frame(ack, Endpoint())
            for start in range(0, len(wire), 17):
                sock.sendall(wire[start:start + 17])
        peer = Peer(handler)
        try:
            with Client(Endpoint(port=peer.port)) as client:
                data = client.send(message())
                self.assertGreater(len(data), 5000)
                self.assertTrue(data.endswith(b"Z" * 5000))
        finally:
            peer.close()
        self.assertFalse(peer.errors)

    def test_coalesced_frames_retained(self):
        def handler(sock):
            sock.sendall(b"\x0bone\x1c\r\x0btwo\x1c\r")
        peer = Peer(handler)
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                reader = FrameReader(Endpoint())
                self.assertEqual(reader.receive(sock), b"one")
                self.assertEqual(reader.receive(sock), b"two")
        finally:
            peer.close()

    def test_partial_timeout(self):
        def handler(sock):
            sock.sendall(b"\x0bMSH|partial")
            time.sleep(.3)
        peer = Peer(handler)
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                with self.assertRaises(TransportError) as ctx:
                    FrameReader(Endpoint(response_timeout=.08)).receive(sock)
                self.assertEqual(ctx.exception.stage, "receive timeout")
                self.assertIn(b"partial", ctx.exception.partial)
        finally:
            peer.close()

    def test_malformed_framing(self):
        peer = Peer(lambda sock: sock.sendall(b"MSH|not framed\r"))
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                with self.assertRaisesRegex(TransportError, "framing"):
                    FrameReader(Endpoint()).receive(sock)
        finally:
            peer.close()

    def test_closed_before_ack(self):
        peer = Peer(lambda sock: sock.sendall(b"\x0bpartial"))
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                with self.assertRaisesRegex(TransportError, "closed"):
                    FrameReader(Endpoint()).receive(sock)
        finally:
            peer.close()

    def test_size_limit(self):
        peer = Peer(lambda sock: sock.sendall(b"\x0b" + b"x" * 500 + b"\x1c\r"))
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                with self.assertRaisesRegex(TransportError, "maximum"):
                    FrameReader(Endpoint(max_bytes=10)).receive(sock)
        finally:
            peer.close()

    def test_raw_waits_for_eof(self):
        def handler(sock):
            sock.sendall(b"one")
            time.sleep(.03)
            sock.sendall(b"two")
        peer = Peer(handler)
        try:
            with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                self.assertEqual(FrameReader(Endpoint(framing="raw")).receive(sock), b"onetwo")
        finally:
            peer.close()

    def test_xml_and_custom(self):
        for endpoint in (Endpoint(framing="xml"), Endpoint(framing="custom", prefix_hex="0202", suffix_hex="0304")):
            peer = Peer(lambda sock: sock.sendall(frame(b"hello", endpoint)))
            try:
                with socket.create_connection(("127.0.0.1", peer.port)) as sock:
                    self.assertEqual(FrameReader(endpoint).receive(sock), b"hello")
            finally:
                peer.close()

    def test_bad_endpoint(self):
        for endpoint in (Endpoint(port=0), Endpoint(response_timeout=float("nan")), Endpoint(max_bytes=0),
                         Endpoint(framing="bad"), Endpoint(framing="custom", suffix_hex="")):
            with self.assertRaises(ValueError):
                endpoint.validate()

    def test_cancellation_during_connect_sends_nothing(self):
        stop = threading.Event()
        client = Client(Endpoint())
        def opening():
            stop.set()
        with patch.object(client, "open", side_effect=opening):
            with self.assertRaisesRegex(TransportError, "no message sent"):
                client.send(message(), stop=stop)

    def test_encode_error(self):
        peer = Peer(lambda sock: sock.recv(4096))
        try:
            with Client(Endpoint(port=peer.port, encoding="ascii")) as client:
                with self.assertRaisesRegex(TransportError, "encode"):
                    client.send("€")
        finally:
            peer.close()

    def test_legacy_connection_large_response(self):
        def handler(sock):
            request = FrameReader(Endpoint()).receive(sock)
            sock.sendall(frame(build_ack(request.decode()).encode() + b"x" * 3000, Endpoint()))
        peer = Peer(handler)
        conn = Connection("127.0.0.1", peer.port)
        try:
            conn.Open()
            response = conn.Send(Message(message()))
            self.assertGreater(len(response), 3000)
            self.assertTrue(response.startswith(b"\x0b"))
            self.assertTrue(response.endswith(b"\x1c\r"))
        finally:
            conn.Close()
            peer.close()


class EngineTest(unittest.TestCase):
    def listener(self, **kwargs):
        server = Listener(Endpoint(port=free_port(), response_timeout=.2), ListenerOptions(**kwargs))
        server.start()
        self.addCleanup(lambda: (server.stop(), server.thread.join(3)))
        return server

    def run_test(self, listener, **kwargs):
        runner = LoadRunner(Endpoint(port=listener.endpoint.port, response_timeout=.3), RunOptions(**kwargs), next(iter(SAMPLES.values())))
        runner.start()
        runner.thread.join(5)
        self.assertFalse(runner.thread.is_alive())
        return runner

    def test_persistent_concurrent_load(self):
        listener = self.listener()
        runner = self.run_test(listener, count=20, rate=100, workers=4)
        summary = runner.capture.summary()
        self.assertEqual(summary["success"], 20)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["AA"], 20)
        self.assertEqual(len({r["control_id"] for r in runner.capture.records}), 20)
        self.assertEqual(listener.capture.summary()["success"], 20)

    def test_new_connections(self):
        runner = self.run_test(self.listener(), count=4, rate=100, persistent=False)
        self.assertEqual(runner.capture.summary()["success"], 4)

    def test_negative_ack_debug(self):
        runner = self.run_test(self.listener(ack_code="AR"), count=1)
        record = runner.capture.records[0]
        self.assertFalse(record["success"])
        self.assertEqual(record["stage"], "negative ACK")
        self.assertEqual(record["ack"]["code"], "AR")
        self.assertTrue(record["outbound"])
        self.assertTrue(record["inbound"])
        self.assertIn("Traceback", record["traceback"])

    def test_timeout_retry_count_and_id(self):
        listener = self.listener(ack_code="none")
        runner = self.run_test(listener, count=1, retries=1, retry_delay=.01)
        self.assertEqual(runner.capture.summary()["failed"], 1)
        self.assertEqual(runner.capture.summary()["attempts"], 2)
        self.assertEqual(len(listener.capture.records), 2)
        self.assertEqual(listener.capture.records[0]["control_id"], listener.capture.records[1]["control_id"])

    def test_invalid_inbound_rejected(self):
        listener = self.listener()
        with Client(Endpoint(port=listener.endpoint.port)) as client:
            ack = client.send("MSH|^~\\&|bad\r").decode()
            self.assertEqual(inspect_ack(ack, correlate=False)["code"], "AE")

    def test_stop_interrupts_receive(self):
        listener = self.listener(ack_code="none")
        runner = LoadRunner(Endpoint(port=listener.endpoint.port, response_timeout=30), RunOptions(count=0), message())
        runner.start()
        deadline = time.monotonic() + 2
        while not listener.capture.records and time.monotonic() < deadline:
            time.sleep(.01)
        start = time.monotonic()
        runner.stop()
        runner.thread.join(2)
        self.assertFalse(runner.thread.is_alive())
        self.assertLess(time.monotonic() - start, 1)

    def test_rate_and_duration(self):
        runner = self.run_test(self.listener(), count=0, rate=10, duration=.25)
        self.assertEqual(runner.capture.summary()["completed"], 3)
        self.assertGreaterEqual(runner.capture.summary()["elapsed_seconds"], .19)

    def test_stop_on_error(self):
        runner = self.run_test(self.listener(ack_code="AE"), count=10, rate=100, stop_on_error=True)
        self.assertEqual(runner.capture.summary()["completed"], 1)

    def test_disconnect_simulation(self):
        runner = self.run_test(self.listener(ack_code="close"), count=1)
        self.assertEqual(runner.capture.summary()["failed"], 1)
        self.assertIn("closed", runner.capture.records[0]["error"])

    def test_listener_client_limit(self):
        listener = self.listener(max_clients=1)
        with socket.create_connection(("127.0.0.1", listener.endpoint.port)) as first:
            deadline = time.monotonic() + 1
            while not listener.connections and time.monotonic() < deadline:
                time.sleep(.01)
            with socket.create_connection(("127.0.0.1", listener.endpoint.port)) as second:
                second.settimeout(1)
                self.assertEqual(second.recv(1), b"")

    def test_validation_before_network(self):
        with self.assertRaisesRegex(ValueError, "validation"):
            LoadRunner(Endpoint(), RunOptions(), "MSH|bad")
        with self.assertRaises(ValueError):
            RunOptions(rate=float("nan")).validate()
        with self.assertRaises(ValueError):
            Listener(Endpoint(framing="raw"))

    def test_encoding_failure_keeps_diagnostics(self):
        with self.assertRaisesRegex(TransportError, "encode"):
            LoadRunner(Endpoint(encoding="ascii"), RunOptions(payload_type="Raw"), "€")

    def test_listener_framing_error_retains_bytes(self):
        listener = self.listener()
        with socket.create_connection(("127.0.0.1", listener.endpoint.port)) as sock:
            sock.sendall(b"BAD FRAMING")
            deadline = time.monotonic() + 1
            while not listener.capture.records and time.monotonic() < deadline:
                time.sleep(.01)
        record = listener.capture.records[0]
        self.assertFalse(record["success"])
        self.assertEqual(record["stage"], "framing")
        self.assertEqual(record["partial"], b"BAD FRAMING")

    def test_tls_trust_and_hostname(self):
        openssl = shutil.which("openssl")
        if not openssl:
            candidate = Path(sys.executable).parent / "Library" / "bin" / "openssl.exe"
            openssl = str(candidate) if candidate.exists() else None
        if not openssl:
            self.skipTest("OpenSSL executable needed to generate an ephemeral TLS test certificate")
        with tempfile.TemporaryDirectory() as directory:
            cert, key = str(Path(directory) / "cert.pem"), str(Path(directory) / "key.pem")
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", key, "-out", cert],
                           check=True, capture_output=True)
            endpoint = Endpoint(port=free_port(), tls=True, cert_file=cert, key_file=key)
            listener = Listener(endpoint)
            listener.start()
            try:
                with Client(Endpoint(port=endpoint.port, tls=True, ca_file=cert, server_name="localhost")) as client:
                    self.assertTrue(inspect_ack(client.send(message()).decode())["accepted"])
                with self.assertRaisesRegex(TransportError, "TLS"):
                    with Client(Endpoint(port=endpoint.port, tls=True, ca_file=cert, server_name="incorrect.example")):
                        pass
            finally:
                listener.stop()
                listener.thread.join(3)

    def test_capture_bounded_and_report_redacted(self):
        capture = Capture(limit=2)
        for seq in range(5):
            capture.record({"sequence": seq, "success": True, "outbound": b"SECRET", "control_id": "SECRET", "elapsed_ms": seq})
        capture.finish()
        self.assertEqual(capture.summary()["completed"], 5)
        self.assertEqual(len(capture.records), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            capture.export(path)
            self.assertNotIn("SECRET", path.read_text())
            capture.export(path, True)
            self.assertIn(b"SECRET".hex(), path.read_text())

    def test_profile_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            save_profile(path, Endpoint(), RunOptions(), ListenerOptions(), message())
            endpoint, options, listener, template = load_profile(path)
            self.assertEqual(endpoint, Endpoint())
            self.assertEqual(options, RunOptions())
            self.assertEqual(listener, ListenerOptions())
            self.assertTrue(template.startswith("MSH"))

    def test_atomic_save_preserves_existing_file_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text("original", encoding="utf-8")
            with self.assertRaises(TypeError):
                write_json(path, {"bad": object()})
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_error_count_survives_event_eviction(self):
        capture = Capture(limit=1)
        capture.emit("error", error="failure")
        capture.finish()
        self.assertEqual(capture.events[0]["kind"], "finished")
        self.assertEqual(capture.summary()["diagnostic_errors"], 1)

    def test_cli_send_and_validate(self):
        self.assertEqual(cli_main(["validate", "--sample", "oru"]), 0)
        listener = self.listener()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "report.json")
            self.assertEqual(cli_main(["send", "--port", str(listener.endpoint.port), "--count", "2", "--rate", "100", "--report", path]), 0)
            self.assertEqual(json.loads(Path(path).read_text())["summary"]["success"], 2)

    def test_cli_help_and_error_exit(self):
        from quake.cli import parser
        for command in ("send", "listen", "validate"):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                parser().parse_args([command, "--help"])
            self.assertEqual(ctx.exception.code, 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["send", "--rate", "nan"]), 2)


if __name__ == "__main__":
    unittest.main()
