"""Tk desktop client. Workers only publish events; all Tk access is on the UI thread."""

from dataclasses import asdict, replace
import json
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__
from .engine import Capture, Listener, ListenerOptions, LoadRunner, RunOptions, load_profile, save_profile
from .messages import HL7, SAMPLES, render_template
from .transport import Endpoint, frame, hex_dump


GUIDE = """QUICK START · SEND TO MIRTH
1. Create/deploy an HL7 v2 channel with a TCP Listener source.
2. Set its transmission mode to MLLP (start 0B, end 1C 0D).
3. Enter the server hostname and source port here. Use a synthetic sample or open your own message.
4. Click Validate / preview, then Send once. Inspect the transaction and its ACK below.
5. Only after the single-message test passes, set the rate/count and run a load test.

TEST MIRTH'S OUTBOUND TCP SENDER
1. In Inbound simulator, choose a local bind address and an unused port.
2. Start listening, then configure Mirth's TCP Sender destination to this computer's reachable IP and that port.
3. Match framing/encoding/TLS on both sides. 127.0.0.1 is only reachable on this computer.
4. Select AA for normal ACKs; AE/AR for negative ACKs; delay, no ACK, or close for failure simulation.
5. Configure Mirth's sender response timeout above the ACK delay unless deliberately testing timeout handling.

WHAT AN ACK MEANS
AA/CA = acceptance at the configured application/commit acknowledgement layer.
AE/AR/CE/CR = error/rejection. Read MSA-3 and ERR, then inspect Mirth's message/channel error view.
An ACK is not evidence of downstream delivery or clinical correctness. Enhanced-mode two-stage ACK workflows
need separate testing; this tool expects one response per outbound message.

TROUBLESHOOTING
Connect refused: channel may be stopped, undeployed, on another port, or bound to a different address.
Connect timeout: check DNS, routing, firewall and server availability.
TLS: verify CA trust, certificate hostname, server name override, and mutual TLS requirements.
No complete ACK: check Mirth's source response settings, response generation, delimiter and channel errors.
ACK ID mismatch: MSA-2 must echo sent MSH-10. Check response scripts and duplicate/out-of-order responses.
Encoding: match both sides and MSH-18. MirthQuake does not silently rewrite MSH-18.
Raw TCP: no universal message boundary; responses are collected until EOF or the response deadline.
XML mode: legacy two-leading/one-trailing NUL framing, not a universal HL7 XML standard.

SAFETY & LIMITS
Use non-production endpoints and synthetic data. These tests can overwhelm real interfaces.
Retries are OFF by default: a timeout does not mean the receiver failed to process the message.
Templates support {{timestamp}}, {{control_id}}, {{sequence}}. Refresh replaces MSH-7/MSH-10 per message.
Basic validation checks HL7 structure, not full version-specific schemas, required clinical fields or terminology.
Persistent mode uses one sequential connection per worker; rate is an aggregate target, not guaranteed throughput.
Fire-and-forget always closes each connection to avoid accumulating unread ACKs.
History is bounded. Counters cover the whole run; percentile metrics use the last 10,000 completions.
Diagnostics on screen/in memory contain full message data. Profiles store the message template.
Reports omit sensitive diagnostics by default. Only include them when you can securely handle the data.
"""


class MirthQuakeApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"MirthQuake {__version__} · HL7 Interface Workbench")
        self.root.geometry("1250x900")
        self.root.minsize(1000, 720)
        self.events = queue.Queue(maxsize=4000)
        self.dropped = 0
        self.active = None
        self.capture = Capture(limit=250, callback=self._publish)
        self.vars = {}
        self.listener_vars = {}
        self.run_vars = {}
        self.records = {}
        self.closing = False
        self.status = tk.StringVar(value="Ready · use synthetic data and a test endpoint")
        self.metrics = tk.StringVar(value="No transactions yet")
        self.failures_only = tk.BooleanVar(value=False)
        self.include_sensitive = tk.BooleanVar(value=False)
        self._build()
        self._sample()
        self.notebook.select(0)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._drain)

    def _build(self):
        shell = ttk.Frame(self.root, padding=12)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="MirthQuake", font=("Segoe UI", 22, "bold")).pack(side="left")
        ttk.Label(header, text="  HL7 interface workbench  /  Mirth Connect + generic TCP", foreground="#52657a").pack(side="left")
        ttk.Button(header, text="Load profile…", command=self._load_profile).pack(side="right", padx=4)
        ttk.Button(header, text="Save profile…", command=self._save_profile).pack(side="right", padx=4)

        controls = ttk.Frame(shell)
        controls.pack(fill="x", pady=6)
        self.send_button = ttk.Button(controls, text="Send once", command=lambda: self._send(True))
        self.send_button.pack(side="left", padx=(0, 6))
        self.run_button = ttk.Button(controls, text="Run load test", command=lambda: self._send(False))
        self.run_button.pack(side="left", padx=6)
        self.listen_button = ttk.Button(controls, text="Start listener", command=self._listen)
        self.listen_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(controls, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Label(controls, textvariable=self.status).pack(side="left", padx=12)

        panes = ttk.Panedwindow(shell, orient="vertical")
        panes.pack(fill="both", expand=True)
        config = ttk.Notebook(panes)
        panes.add(config, weight=3)
        send = ttk.Frame(config, padding=12)
        advanced = ttk.Frame(config, padding=12)
        message = ttk.Frame(config, padding=8)
        inbound = ttk.Frame(config, padding=12)
        help_tab = ttk.Frame(config)
        for page, title in ((send, "Connection & load"), (message, "Message editor & inspector"),
                            (advanced, "Advanced transport"), (inbound, "Inbound simulator"), (help_tab, "Mirth setup & help")):
            config.add(page, text=title)
        self.notebook = config
        self.message_tab = message

        left = ttk.LabelFrame(send, text="Target interface", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.LabelFrame(send, text="Load test · aggregate rate", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        send.columnconfigure((0, 1), weight=1)
        self._field(left, self.vars, "host", "Host / IP", "127.0.0.1", 0)
        self._field(left, self.vars, "port", "Port", "6661", 1)
        self._field(left, self.vars, "framing", "Framing", "mllp", 2, ("mllp", "xml", "raw", "custom"))
        self._field(left, self.vars, "encoding", "Wire encoding", "utf-8", 3, ("utf-8", "windows-1252", "ascii", "iso-8859-1"), editable=True)
        self._field(left, self.run_vars, "payload_type", "Payload type", "HL7", 4, ("HL7", "XML", "Raw"))
        self._check(left, self.run_vars, "refresh", "Refresh MSH timestamp & control ID per message", True, 5)
        self._check(left, self.run_vars, "validate_hl7", "Check basic HL7 structure before sending", True, 6)
        self._field(right, self.run_vars, "count", "Message count (0 = continuous)", "100", 0)
        self._field(right, self.run_vars, "rate", "Messages / second", "5", 1)
        self._field(right, self.run_vars, "workers", "Concurrent connections / workers", "1", 2)
        self._field(right, self.run_vars, "duration", "Duration cap in seconds (0 = none)", "0", 3)
        self._check(right, self.run_vars, "persistent", "Reuse connections (one per worker)", True, 4)
        self._check(right, self.run_vars, "stop_on_error", "Stop all workers on first failure", False, 5)
        ttk.Label(send, text="Start with Send once. Load testing can disrupt interfaces; verify the target is safe to test.",
                  foreground="#9a4c08").grid(row=1, column=0, columnspan=2, sticky="w", pady=10)

        transport = ttk.LabelFrame(advanced, text="Timeouts, framing & response handling", padding=8)
        transport.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tls = ttk.LabelFrame(advanced, text="TLS · shared by sender and listener", padding=8)
        tls.grid(row=0, column=1, sticky="nsew")
        advanced.columnconfigure((0, 1), weight=1)
        for row, (key, label, default) in enumerate([
            ("connect_timeout", "Connect / TLS timeout (seconds)", "5"),
            ("response_timeout", "Response timeout (seconds)", "10"),
            ("max_bytes", "Maximum payload bytes", "4194304"),
            ("prefix_hex", "Custom start bytes (hex)", "0b"),
            ("suffix_hex", "Custom end bytes (hex)", "1c0d"),
        ]):
            self._field(transport, self.vars, key, label, default, row)
        self._check(transport, self.run_vars, "wait_response", "Wait for response (otherwise delivery unconfirmed)", True, 5)
        self._check(transport, self.run_vars, "check_ack", "Check HL7 ACK status (MSA / ERR)", True, 6)
        self._check(transport, self.run_vars, "correlate", "Require MSA-2 to match sent MSH-10", True, 7)
        self._field(transport, self.run_vars, "retries", "Retries (duplicates possible!)", "0", 8)
        self._field(transport, self.run_vars, "retry_delay", "Retry delay (seconds)", "1", 9)
        self._check(tls, self.vars, "tls", "Enable TLS", False, 0)
        self._check(tls, self.vars, "verify_tls", "Verify server certificate & hostname", True, 1)
        for row, (key, label) in enumerate([("ca_file", "CA certificate file"), ("cert_file", "Client / listener certificate"),
                                            ("key_file", "Private key file"), ("server_name", "Server name override")], 2):
            self._field(tls, self.vars, key, label, "", row, browse=key != "server_name")
        ttk.Label(tls, text="Listener TLS requires certificate + private key.\nClient verification uses system trust or the CA file.\nKeep verification enabled except for controlled tests.",
                  wraplength=420, foreground="#52657a").grid(row=7, column=0, columnspan=3, sticky="w", pady=10)

        tools = ttk.Frame(message)
        tools.pack(fill="x", pady=(0, 6))
        self.sample_var = tk.StringVar(value=next(iter(SAMPLES)))
        ttk.Combobox(tools, textvariable=self.sample_var, values=list(SAMPLES), state="readonly", width=30).pack(side="left")
        ttk.Button(tools, text="Use sample", command=self._sample).pack(side="left", padx=4)
        ttk.Button(tools, text="Open message…", command=self._open_message).pack(side="left", padx=4)
        ttk.Button(tools, text="Save message…", command=self._save_message).pack(side="left", padx=4)
        ttk.Button(tools, text="Validate / preview", command=self._inspect).pack(side="left", padx=4)
        editor_panes = ttk.Panedwindow(message, orient="horizontal")
        editor_panes.pack(fill="both", expand=True)
        self.editor = scrolledtext.ScrolledText(editor_panes, wrap="none", font=("Consolas", 10), height=12, undo=True)
        self.inspector = scrolledtext.ScrolledText(editor_panes, wrap="none", font=("Consolas", 10), height=12, state="disabled")
        editor_panes.add(self.editor, weight=3)
        editor_panes.add(self.inspector, weight=2)
        ttk.Label(message, text="Tokens: {{timestamp}}  {{control_id}}  {{sequence}}   ·   One HL7 message per editor; newlines become CR on the wire.",
                  foreground="#52657a").pack(anchor="w", pady=4)

        settings = ttk.LabelFrame(inbound, text="Receive messages from Mirth / another interface", padding=12)
        settings.pack(fill="x")
        self._field(settings, self.listener_vars, "host", "Local bind address", "127.0.0.1", 0)
        self._field(settings, self.listener_vars, "port", "Listen port", "6662", 1)
        self._field(settings, self.listener_vars, "ack_code", "Response mode", "AA", 2,
                    ("AA", "AE", "AR", "CA", "CE", "CR", "none", "close"))
        self._field(settings, self.listener_vars, "delay", "Response delay (seconds)", "0", 3)
        self._field(settings, self.listener_vars, "error_text", "Negative ACK description", "Synthetic test rejection", 4)
        self._field(settings, self.listener_vars, "max_clients", "Maximum concurrent clients", "32", 5)
        self._check(settings, self.listener_vars, "validate_hl7", "Validate inbound HL7; reject malformed messages with AE", True, 6)
        ttk.Label(inbound, text="Uses framing, encoding, payload limit and TLS from the connection / advanced tabs.\n"
                  "Use 0.0.0.0 (IPv4) or :: (IPv6) only if you intend to accept remote connections.\n"
                  "For XML/custom payloads, disable inbound HL7 validation and select no ACK unless testing an HL7 ACK response.",
                  foreground="#52657a", wraplength=1000).pack(anchor="w", pady=12)

        guide = scrolledtext.ScrolledText(help_tab, wrap="word", font=("Segoe UI", 10))
        guide.pack(fill="both", expand=True)
        guide.insert("1.0", GUIDE)
        guide.configure(state="disabled")

        diagnostics = ttk.Frame(panes, padding=(0, 8, 0, 0))
        panes.add(diagnostics, weight=3)
        bar = ttk.Frame(diagnostics)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, textvariable=self.metrics, font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(bar, text="Export report…", command=self._export).pack(side="right", padx=4)
        ttk.Checkbutton(bar, text="Include sensitive diagnostics", variable=self.include_sensitive).pack(side="right", padx=4)
        notebook = ttk.Notebook(diagnostics)
        notebook.pack(fill="both", expand=True)
        transactions = ttk.Frame(notebook)
        notebook.add(transactions, text="Transactions · select for full details")
        log_page = ttk.Frame(notebook)
        notebook.add(log_page, text="Event log")
        ttk.Checkbutton(transactions, text="Failures only", variable=self.failures_only, command=self._rebuild_records).pack(anchor="w")
        record_tools = ttk.Frame(transactions)
        record_tools.pack(fill="x", pady=(0, 4))
        ttk.Button(record_tools, text="Replay request in editor", command=self._replay).pack(side="left", padx=(0, 4))
        ttk.Button(record_tools, text="Copy diagnostics", command=self._copy_details).pack(side="left", padx=4)
        ttk.Button(record_tools, text="Save captured payload…", command=self._save_payload).pack(side="left", padx=4)
        record_panes = ttk.Panedwindow(transactions, orient="horizontal")
        record_panes.pack(fill="both", expand=True)
        table_frame = ttk.Frame(record_panes)
        self.table = ttk.Treeview(table_frame, columns=("seq", "result", "ack", "ms", "id"), show="headings", height=8)
        for name, label, width in (("seq", "#", 45), ("result", "Result", 80), ("ack", "ACK", 50),
                                   ("ms", "ms", 70), ("id", "Control ID", 190)):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, minwidth=30, stretch=name == "id")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.tag_configure("failed", foreground="#b42318")
        self.table.bind("<<TreeviewSelect>>", self._show_record)
        self.details = scrolledtext.ScrolledText(record_panes, wrap="none", font=("Consolas", 9), state="disabled", height=10)
        record_panes.add(table_frame, weight=2)
        record_panes.add(self.details, weight=3)
        self.log = scrolledtext.ScrolledText(log_page, wrap="word", font=("Consolas", 9), state="disabled", height=10)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("error", foreground="#b42318")
        self.log.tag_configure("warning", foreground="#9a4c08")

    def _field(self, parent, collection, key, label, default, row, choices=None, editable=False, browse=False):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        var = tk.StringVar(value=default)
        collection[key] = var
        widget = ttk.Combobox(parent, textvariable=var, values=choices, state="normal" if editable else "readonly") if choices else ttk.Entry(parent, textvariable=var)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        if browse:
            def pick():
                path = filedialog.askopenfilename(parent=self.root)
                if path:
                    var.set(path)
            ttk.Button(parent, text="…", width=3, command=pick).grid(row=row, column=2, padx=(4, 0))

    def _check(self, parent, collection, key, label, default, row):
        collection[key] = tk.BooleanVar(value=default)
        ttk.Checkbutton(parent, text=label, variable=collection[key]).grid(row=row, column=0, columnspan=3, sticky="w", pady=3)

    def _endpoint(self, listening=False):
        values = {k: v.get() for k, v in self.vars.items()}
        for key in ("port", "max_bytes"):
            values[key] = int(values[key])
        for key in ("connect_timeout", "response_timeout"):
            values[key] = float(values[key])
        values["host"] = values["host"].strip()
        if listening:
            values.update(host=self.listener_vars["host"].get().strip(), port=int(self.listener_vars["port"].get()))
        endpoint = Endpoint(**values)
        endpoint.validate()
        return endpoint

    def _options(self):
        values = {k: v.get() for k, v in self.run_vars.items()}
        for key in ("count", "workers", "retries"):
            values[key] = int(values[key])
        for key in ("rate", "duration", "retry_delay"):
            values[key] = float(values[key])
        options = RunOptions(**values)
        options.validate()
        return options

    def _listener_options(self):
        values = {k: v.get() for k, v in self.listener_vars.items() if k not in ("host", "port")}
        values["delay"] = float(values["delay"])
        values["max_clients"] = int(values["max_clients"])
        options = ListenerOptions(**values)
        options.validate()
        return options

    def _text(self):
        return self.editor.get("1.0", "end-1c")

    def _replace_text(self, widget, text, readonly=False):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if readonly:
            widget.configure(state="disabled")

    def _sample(self):
        name = self.sample_var.get()
        if self._text().strip() and not messagebox.askyesno("Replace editor?", "Replace the current message with the selected sample?", parent=self.root):
            return
        self._replace_text(self.editor, SAMPLES[name].replace("\r", "\n"))
        xml = name.startswith("XML")
        self.run_vars["payload_type"].set("XML" if xml else "HL7")
        self.vars["framing"].set("xml" if xml else "mllp")
        self.notebook.select(self.message_tab)

    def _open_message(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Message files", "*.hl7 *.txt *.xml"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding=self.vars["encoding"].get(), newline="") as handle:
                text = handle.read(64 * 1024 * 1024 + 1)
            if len(text) > 64 * 1024 * 1024:
                raise ValueError("Message file is too large (64 MiB maximum).")
            if self._text().strip() and not messagebox.askyesno("Replace editor?", "Replace the current message with this file?", parent=self.root):
                return
            self._replace_text(self.editor, text.replace("\r\n", "\n").replace("\r", "\n"))
            self.notebook.select(self.message_tab)
            self.status.set(f"Loaded {path}")
        except Exception as exc:
            self._error(exc)

    def _save_message(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".hl7")
        if path:
            try:
                text = self._text()
                if self.run_vars["payload_type"].get() == "HL7":
                    text = text.replace("\n", "\r")
                with open(path, "w", encoding=self.vars["encoding"].get(), newline="") as handle:
                    handle.write(text)
            except Exception as exc:
                self._error(exc)

    def _inspect(self):
        try:
            endpoint, options = self._endpoint(), self._options()
            text = render_template(self._text(), refresh=options.refresh, hl7=options.payload_type == "HL7")
            payload = text.encode(endpoint.encoding)
            lines = [f"Payload: {len(payload)} bytes · {endpoint.encoding} · {endpoint.framing}"]
            if options.payload_type == "HL7":
                msg = HL7(text)
                issues = msg.validate()
                lines += [f"{i.severity} {i.location}: {i.description}" for i in issues] or ["Basic structure checks passed (not full HL7 conformance)."]
                lines += ["", "FIELD INSPECTOR (MSH numbering accounts for MSH-1)"]
                lines += [f"{key:14} {value}" for key, value in msg.fields()]
            lines += ["", "WIRE PREVIEW (tokens regenerate when sending)", hex_dump(frame(payload, endpoint))]
            self._replace_text(self.inspector, "\n".join(lines), True)
            self.notebook.select(self.message_tab)
        except Exception as exc:
            self._error(exc)

    def _new_capture(self):
        # The previous report is replaced only once the new operation is validated.
        self._clear_pending_events()
        self.capture = Capture(limit=250, callback=self._publish)
        self.records.clear()
        self.table.delete(*self.table.get_children())
        self._replace_text(self.details, "", True)
        self._replace_text(self.log, "", True)

    def _send(self, single):
        if self.active:
            return
        try:
            endpoint, options = self._endpoint(), self._options()
            if single:
                options = replace(options, count=1, workers=1, duration=0)
            if endpoint.tls and not endpoint.verify_tls and not messagebox.askyesno("Unverified TLS", "Server certificate verification is disabled. Continue with this controlled test?", parent=self.root):
                return
            if options.retries and not messagebox.askyesno("Duplicate delivery risk", "Retries can resend messages already processed by the receiver. Continue?", parent=self.root):
                return
            runner = LoadRunner(endpoint, options, self._text())
            self._new_capture()
            runner.capture = self.capture
            self.capture.metadata = {"mode": "send", "endpoint": asdict(endpoint), "options": asdict(options)}
            self.active = runner
            self._busy(True)
            self.status.set("Sending…" if single else "Load test running…")
            runner.start()
        except Exception as exc:
            self.active = None
            self._busy(False)
            self._error(exc)

    def _listen(self):
        if self.active:
            return
        try:
            endpoint, options = self._endpoint(True), self._listener_options()
            capture = Capture(limit=250, callback=self._publish)
            listener = Listener(endpoint, options, capture)
            self._clear_pending_events()
            listener.start()
            self.capture = capture
            self.records.clear()
            self.table.delete(*self.table.get_children())
            self._replace_text(self.details, "", True)
            self._replace_text(self.log, "", True)
            self.active = listener
            self._busy(True)
            self.status.set(f"Listening · {endpoint.host}:{endpoint.port}")
        except Exception as exc:
            self._error(exc)

    def _stop(self):
        if self.active:
            self.active.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("Stopping… waiting for worker cleanup")

    def _busy(self, busy):
        for button in (self.send_button, self.run_button, self.listen_button):
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _publish(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _clear_pending_events(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.dropped = 0

    def _drain(self):
        if self.closing:
            return
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["kind"]
            if kind == "record":
                record = event["record"]
                self.records[str(record["sequence"])] = record
                if len(self.records) > 250:
                    oldest = next(iter(self.records))
                    self.records.pop(oldest)
                    if self.table.exists(oldest):
                        self.table.delete(oldest)
                self._insert_record(record)
            elif kind == "finished":
                self.status.set("Stopped" if self.active and self.active.stop_event.is_set() else "Completed")
                # Runner also sets its stop flag during normal cleanup; report completion based on counts.
                if isinstance(self.active, LoadRunner) and self.active.options.count and event["summary"].get("completed", 0) >= self.active.options.count:
                    self.status.set("Completed · inspect failures below" if event["summary"].get("failed", 0) else "Completed")
                self.active = None
                self._busy(False)
                self._append_log(event)
            elif kind not in ("inbound", "outbound"):
                self._append_log(event)
        if self.dropped:
            self._append_log({"time": "", "kind": "warning", "message": f"UI event queue skipped {self.dropped} events. Aggregate metrics and bounded capture remain available in the report."})
            self.dropped = 0
        # Polling thread liveness is also a safety net if a finished event was dropped under heavy load.
        if self.active and self.active.thread and not self.active.thread.is_alive():
            self.active = None
            self._busy(False)
            self.status.set("Finished · report available")
        summary = self.capture.summary()
        self.metrics.set(f"Total {summary.get('completed', 0)}   OK {summary.get('success', 0)}   Failed {summary.get('failed', 0)}   "
                         f"{summary['completed_per_second']:.1f}/s   p95 {summary['p95_ms']:.1f} ms   · last 250 records")
        self.root.after(100, self._drain)

    def _append_log(self, event):
        data = {k: v for k, v in event.items() if k not in ("time", "kind", "partial", "traceback", "payload", "wire")}
        line = f"{event['time']} [{event['kind']}] " + json.dumps(data, ensure_ascii=False, default=str) + "\n"
        self.log.configure(state="normal")
        self.log.insert("end", line, "error" if "error" in event["kind"] else event["kind"])
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log.delete("1.0", f"{lines - 1200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _insert_record(self, record):
        if self.failures_only.get() and record["success"]:
            return
        key = str(record["sequence"])
        values = (key, "OK" if record["success"] else "FAILED", record.get("ack", {}).get("code", "—"),
                  record.get("elapsed_ms", ""), record.get("control_id", ""))
        if self.table.exists(key):
            self.table.item(key, values=values)
        else:
            self.table.insert("", "end", iid=key, values=values, tags=() if record["success"] else ("failed",))

    def _rebuild_records(self):
        self.table.delete(*self.table.get_children())
        for record in self.records.values():
            self._insert_record(record)

    def _show_record(self, _event=None):
        selected = self.table.selection()
        if not selected or selected[0] not in self.records:
            return
        record = self.records[selected[0]]
        metadata = {k: v for k, v in record.items() if not isinstance(v, bytes) and k != "traceback"}
        lines = [json.dumps(metadata, indent=2, ensure_ascii=False)]
        if record.get("traceback"):
            lines += ["", "EXCEPTION TRACEBACK", record["traceback"]]
        encoding = self.capture.metadata.get("endpoint", {}).get("encoding", "utf-8")
        for key in ("outbound", "inbound", "partial"):
            data = record.get(key, b"")
            if data:
                lines += ["", f"{key.upper()} · {len(data)} bytes", "TEXT (control characters escaped except segment newlines)",
                          self._visible(data[:65536].decode(encoding, errors="backslashreplace")), "HEX", hex_dump(data)]
                if len(data) > 65536:
                    lines += ["Text preview limited to 64 KiB. Export with sensitive diagnostics to retain all bytes."]
                if key != "partial":
                    endpoint = Endpoint(**self.capture.metadata["endpoint"])
                    lines += ["WIRE HEX (configured framing)", hex_dump(frame(data, endpoint))]
                if key == "inbound" and data.startswith(b"MSH"):
                    lines += ["FIELDS (first 64 KiB)"] + [f"{k:14} {v}" for k, v in HL7(data[:65536].decode(encoding, errors="replace")).fields()]
        self._replace_text(self.details, "\n".join(lines), True)

    @staticmethod
    def _visible(text):
        return "".join("\n" if c == "\r" else (f"\\x{ord(c):02x}" if ord(c) < 32 and c != "\n" else c) for c in text)

    def _save_profile(self):
        try:
            endpoint, options, listener = self._endpoint(), self._options(), self._listener_options()
            bind = self._endpoint(True)
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", title="Save profile (includes message template)")
            if path:
                save_profile(path, endpoint, options, listener, self._text(), {"host": bind.host, "port": bind.port})
                self.status.set("Profile saved · contains message data")
        except Exception as exc:
            self._error(exc)

    def _load_profile(self):
        if self.active:
            messagebox.showinfo("Test active", "Stop the current operation before loading a profile.", parent=self.root)
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON profile", "*.json")])
        if not path:
            return
        try:
            endpoint, options, listener, text = load_profile(path)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if self._text().strip() and not messagebox.askyesno("Load profile?", "Replace current settings and message with this profile?", parent=self.root):
                return
            for source, dest in ((asdict(endpoint), self.vars), (asdict(options), self.run_vars), (asdict(listener), self.listener_vars)):
                for key, value in source.items():
                    dest[key].set(value)
            for key, value in data.get("listener_endpoint", {}).items():
                if key in ("host", "port"):
                    self.listener_vars[key].set(value)
            self._replace_text(self.editor, text.replace("\r", "\n"))
            self.status.set("Profile loaded")
        except Exception as exc:
            self._error(exc)

    def _export(self):
        include = self.include_sensitive.get()
        if include and not messagebox.askyesno("Sensitive data", "This report will include message payloads, IDs, errors and tracebacks. Save it only to a secure location. Continue?", parent=self.root):
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", title="Export test report")
        if path:
            try:
                self.capture.export(path, include)
                self.status.set("Report exported")
            except Exception as exc:
                self._error(exc)

    def _error(self, exc):
        self.status.set("Action failed · see error")
        messagebox.showerror("MirthQuake", str(exc), parent=self.root)

    def _selected_record(self):
        selected = self.table.selection()
        if not selected or selected[0] not in self.records:
            messagebox.showinfo("Select a transaction", "Select a transaction in the table first.", parent=self.root)
            return None
        return self.records[selected[0]]

    def _request_bytes(self, record):
        return record.get("inbound" if self.capture.metadata.get("mode") == "listen" else "outbound", b"")

    def _replay(self):
        record = self._selected_record()
        if record is None:
            return
        try:
            encoding = self.capture.metadata["endpoint"]["encoding"]
            payload = self._request_bytes(record)
            if not payload:
                raise ValueError("This record has no complete request payload to replay.")
            text = payload.decode(encoding, errors="strict")
            if self._text().strip() and not messagebox.askyesno("Replay request", "Replace the editor with the selected request? Nothing is sent automatically. Timestamp/ID refresh still applies when you send.", parent=self.root):
                return
            self._replace_text(self.editor, text.replace("\r", "\n"))
            self.vars["encoding"].set(encoding)
            self.run_vars["payload_type"].set("HL7" if text.startswith("MSH") else ("XML" if text.lstrip().startswith("<") else "Raw"))
            self.notebook.select(self.message_tab)
        except Exception as exc:
            self._error(exc)

    def _copy_details(self):
        if self._selected_record() is not None:
            self._show_record()
            self.root.clipboard_clear()
            self.root.clipboard_append(self.details.get("1.0", "end-1c"))
            self.status.set("Diagnostics copied · clipboard contains message data")

    def _save_payload(self):
        record = self._selected_record()
        if record is None:
            return
        payload = self._request_bytes(record) or record.get("partial", b"")
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".hl7", title="Save request payload bytes (may contain sensitive data; no transport framing)")
        if path:
            try:
                with open(path, "wb") as handle:
                    handle.write(payload)
                self.status.set("Captured payload saved · contains message data")
            except Exception as exc:
                self._error(exc)

    def _close(self):
        if self.active and not messagebox.askyesno("Close MirthQuake?", "Stop the active operation and close? Export a report first if you need to retain diagnostics.", parent=self.root):
            return
        self.closing = True
        if self.active:
            self.active.stop()
        self.root.destroy()


def launch():
    root = tk.Tk()
    MirthQuakeApp(root)
    root.mainloop()
