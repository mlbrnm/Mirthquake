"""Headless interface tests suitable for scripts and CI."""

import argparse
from dataclasses import replace
import json
import math
import signal
import sys
import threading

from . import __version__
from .engine import Capture, Listener, ListenerOptions, LoadRunner, RunOptions, load_profile
from .messages import HL7, SAMPLES, render_template
from .transport import Endpoint


def parser():
    app = argparse.ArgumentParser(description="MirthQuake: HL7 v2 / Mirth Connect TCP interface testing. No arguments opens the desktop app.")
    app.add_argument("--version", action="version", version=__version__)
    sub = app.add_subparsers(dest="command", required=True)
    for name in ("send", "listen", "validate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--profile", help="JSON profile saved from the desktop app")
        cmd.add_argument("--file", help="Message/template file (send or validate)")
        cmd.add_argument("--sample", choices=("adt", "oru", "orm", "xml"), help="Synthetic sample (default: adt)")
        cmd.add_argument("--encoding", default=None)
        if name == "validate":
            continue
        cmd.add_argument("--host", help="Target host, or local bind address for listen")
        cmd.add_argument("--port", type=int)
        cmd.add_argument("--framing", choices=("mllp", "xml", "raw", "custom"))
        cmd.add_argument("--prefix-hex")
        cmd.add_argument("--suffix-hex")
        cmd.add_argument("--connect-timeout", type=float)
        cmd.add_argument("--response-timeout", type=float)
        cmd.add_argument("--max-bytes", type=int)
        cmd.add_argument("--tls", action="store_true", default=None)
        cmd.add_argument("--insecure", action="store_true", help="Disable server certificate verification (controlled tests only)")
        cmd.add_argument("--ca-file")
        cmd.add_argument("--cert-file")
        cmd.add_argument("--key-file")
        cmd.add_argument("--server-name")
        cmd.add_argument("--report", help="Write JSON report on completion / Ctrl+C")
        cmd.add_argument("--include-sensitive", action="store_true", help="Include full messages, identifiers, errors and tracebacks in report")
        cmd.add_argument("--verbose", action="store_true", help="Print exchange failures (may contain sensitive values)")
        cmd.add_argument("--history", type=int, default=250, help="Number of detailed records/events to retain (1-10000)")
        if name == "send":
            cmd.add_argument("--count", type=int, help="Logical messages; 0 means continuous")
            cmd.add_argument("--rate", type=float, help="Aggregate messages/second")
            cmd.add_argument("--workers", type=int)
            cmd.add_argument("--duration", type=float, help="Duration cap in seconds")
            cmd.add_argument("--new-connection", action="store_true")
            cmd.add_argument("--no-response", action="store_true", help="Delivery unconfirmed; close socket after each send")
            cmd.add_argument("--no-ack-check", action="store_true")
            cmd.add_argument("--no-correlation", action="store_true")
            cmd.add_argument("--keep-ids", action="store_true", help="Do not refresh literal MSH-7/MSH-10 values (template tokens still expand)")
            cmd.add_argument("--no-validation", action="store_true")
            cmd.add_argument("--payload-type", choices=("HL7", "XML", "Raw"))
            cmd.add_argument("--retries", type=int)
            cmd.add_argument("--retry-delay", type=float)
            cmd.add_argument("--stop-on-error", action="store_true")
        else:
            cmd.add_argument("--ack", choices=("AA", "AE", "AR", "CA", "CE", "CR", "none", "close"))
            cmd.add_argument("--delay", type=float)
            cmd.add_argument("--error-text")
            cmd.add_argument("--max-clients", type=int)
            cmd.add_argument("--no-validation", action="store_true")
            cmd.add_argument("--duration", type=float, default=0, help="Stop listener after N seconds; 0 = until Ctrl+C")
    return app


def main(argv=None):
    # Some Windows consoles use legacy code pages; do not crash while printing
    # non-ASCII interface errors or message identifiers.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parser().parse_args(argv)
    operation = None
    try:
        endpoint, options, listener_options, template = Endpoint(), RunOptions(), ListenerOptions(), next(iter(SAMPLES.values()))
        if args.profile:
            endpoint, options, listener_options, template = load_profile(args.profile)
            if args.command == "listen":
                with open(args.profile, encoding="utf-8") as handle:
                    listener_endpoint = json.load(handle).get("listener_endpoint", {})
                endpoint = replace(endpoint, **{k: v for k, v in listener_endpoint.items() if k in ("host", "port")})
        elif args.command == "listen":
            endpoint = replace(endpoint, port=6662)
        if args.sample:
            template = list(SAMPLES.values())[("adt", "oru", "orm", "xml").index(args.sample)]
            options = replace(options, payload_type="XML" if args.sample == "xml" else "HL7")
            endpoint = replace(endpoint, framing="xml" if args.sample == "xml" else "mllp")
        if args.encoding:
            endpoint = replace(endpoint, encoding=args.encoding)
        if args.file:
            with open(args.file, encoding=endpoint.encoding, newline="") as handle:
                template = handle.read(64 * 1024 * 1024 + 1)
            if len(template) > 64 * 1024 * 1024:
                raise ValueError("Message file exceeds 64 MiB limit.")
        if args.command == "validate":
            text = render_template(template, hl7=options.payload_type == "HL7")
            issues = HL7(text).validate() if options.payload_type == "HL7" else []
            for issue in issues:
                print(f"{issue.severity} {issue.location}: {issue.description}")
            if not issues:
                print("Basic structure checks passed (not full HL7 conformance)." if options.payload_type == "HL7" else "Template tokens render successfully; no XML/raw schema validation performed.")
            return 1 if any(i.severity == "ERROR" for i in issues) else 0
        endpoint_updates = {key: getattr(args, key) for key in (
            "host", "port", "framing", "prefix_hex", "suffix_hex", "connect_timeout", "response_timeout",
            "max_bytes", "tls", "ca_file", "cert_file", "key_file", "server_name") if getattr(args, key) is not None}
        if args.insecure:
            endpoint_updates["verify_tls"] = False
            print("WARNING: server certificate verification disabled.", file=sys.stderr)
        endpoint = replace(endpoint, **endpoint_updates)
        if not 1 <= args.history <= 10000:
            raise ValueError("History limit must be between 1 and 10000.")
        def event(event):
            if event["kind"] in ("warning", "listening"):
                print(event.get("message", ""), file=sys.stderr, flush=True)
            elif args.verbose and event["kind"] in ("attempt_error", "error"):
                print(f"{event['time']} [{event['kind']}] {event.get('stage', '')}: {event.get('error', '')}", file=sys.stderr, flush=True)
        capture = Capture(args.history, event)
        if args.command == "send":
            updates = {key: getattr(args, key) for key in ("count", "rate", "workers", "duration", "payload_type", "retries", "retry_delay") if getattr(args, key) is not None}
            for flag, key in (("new_connection", "persistent"), ("no_response", "wait_response"),
                              ("no_ack_check", "check_ack"), ("no_correlation", "correlate"),
                              ("keep_ids", "refresh"), ("no_validation", "validate_hl7")):
                if getattr(args, flag):
                    updates[key] = False
            if args.stop_on_error:
                updates["stop_on_error"] = True
            options = replace(options, **updates)
            operation = LoadRunner(endpoint, options, template, capture)
        else:
            if args.duration < 0 or not math.isfinite(args.duration):
                raise ValueError("Listener duration must be finite and non-negative.")
            updates = {key: getattr(args, name) for name, key in (("ack", "ack_code"), ("delay", "delay"),
                       ("error_text", "error_text"), ("max_clients", "max_clients")) if getattr(args, name) is not None}
            if args.no_validation:
                updates["validate_hl7"] = False
            operation = Listener(endpoint, replace(listener_options, **updates), capture)
        interrupted = threading.Event()
        previous = signal.getsignal(signal.SIGINT)
        def interrupt(*_):
            interrupted.set()
            operation.stop()
        signal.signal(signal.SIGINT, interrupt)
        timer = None
        try:
            operation.start()
            if args.command == "listen" and args.duration:
                timer = threading.Timer(args.duration, operation.stop)
                timer.daemon = True
                timer.start()
            while operation.thread.is_alive():
                operation.thread.join(.2)
        finally:
            if timer:
                timer.cancel()
            operation.stop()
            signal.signal(signal.SIGINT, previous)
        summary = capture.summary()
        print(json.dumps(summary, indent=2))
        if args.report:
            capture.export(args.report, args.include_sensitive)
        if interrupted.is_set():
            return 130
        return 1 if summary.get("failed", 0) or summary.get("diagnostic_errors", 0) else 0
    except (ValueError, OSError, LookupError, TypeError, KeyError) as exc:
        print(f"MirthQuake: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        from .transport import TransportError
        if isinstance(exc, TransportError):
            print(f"MirthQuake: {exc}", file=sys.stderr)
            return 2
        raise
    finally:
        if operation:
            operation.stop()


if __name__ == "__main__":
    raise SystemExit(main())
