# MirthQuake
<img width="1587" height="1180" alt="Screenshot 2026-10-13 224033" src="https://github.com/user-attachments/assets/4d5a9d4a-fc10-48fc-bae4-58cbadbf2df1" />

**A dependency-free HL7 v2 interface testing workbench for Mirth Connect and generic TCP interfaces.** Send a single message, run a paced concurrent load test, or act as the receiving interface to exercise an outbound sender—including its failure handling.

## Start

Requires **Python 3.10+** with Tk for the desktop interface. No pip packages are required. Standard Python installers on Windows include Tk; on Linux it may be packaged separately as `python3-tk`. Headless commands do not require Tk.

```powershell
python mirthquake.py
```

Use a full Python installation, not an application's embedded/isolated Python runtime. If `python` resolves to one, invoke your Python installation by its full path.

### Quick send to Mirth

1. Deploy/start a Mirth channel with an **HL7 v2 TCP Listener source**.
2. Set transmission mode to **MLLP**: start `0B`, end `1C 0D`.
3. Enter the server host and source port in **Connection & load**.
4. Use the synthetic ADT/ORU/ORM sample, paste a message, or open a file in **Message editor & inspector**.
5. Click **Validate / preview**, then **Send once**.
6. Select the transaction to inspect the response, ACK fields, bytes, timings, and any errors. Only then increase count/rate and start a load test.

The default target is `127.0.0.1:6661`; there are no company-specific endpoint defaults or automatically opened file dialogs.

### Receive from Mirth

1. In **Inbound simulator**, select a local bind address and a free port (default `127.0.0.1:6662`).
2. Click **Start listener**.
3. Configure Mirth's **TCP Sender destination** to the workstation's reachable IP and that port. Remote Mirth servers cannot reach this listener via their own `localhost`.
4. Match framing, encoding and TLS. Allow the port through the workstation firewall if needed.
5. Use `AA` for acceptance, `AE`/`AR` for rejection, or test delay, `none` (no ACK), and `close` (disconnect without ACK).

The listener handles multiple messages per connection and concurrent clients. Basic validation failures produce an `AE` instead of a configured positive ACK. Listener TLS requires a server certificate and private key. Listener-side client-certificate verification is not currently implemented.

The shared response timeout is also the listener's deadline for completing an inbound frame; idle connections without partial data remain open. Truncated or incorrectly framed input is captured with its partial bytes and closes that client connection.

## Features

### Message workbench

- Editable messages, open/save files, and synthetic ADT admission, ORU observation and ORM order samples.
- Template tokens: `{{timestamp}}`, `{{control_id}}`, `{{sequence}}`. They expand for every logical message; a retry reuses exactly the same rendered message and control ID.
- Optional automatic refresh of literal **MSH-7/MSH-10**. Turn it off to test replay/duplicate behavior. Tokens still expand even when refresh is off.
- Newline normalization to HL7 carriage-return segment separators; no silent removal of trailing field spaces.
- Basic structure checks and field inspection, with correct MSH field numbering and support for custom HL7 field delimiters.
- Framed-wire hex preview, raw/escaped text, MSA status, correlation, MSA-3 and ERR inspection.
- Replay a captured request into the editor without automatically sending it; copy diagnostics or save captured request bytes.

### Outbound transport & load testing

- IPv4/IPv6/DNS TCP targets; proper `sendall` and buffered, complete response reads across fragmented TCP packets. Coalesced frames are retained for the next read.
- MLLP, legacy NUL-framed XML, raw TCP and configurable hex start/end delimiters.
- Configurable strict wire encoding, connection/response timeouts and maximum message size.
- TLS server verification, custom CA, server name override, and client certificate/key for mutual-TLS endpoints. Verification is on by default.
- Single send, fixed message count, duration cap, or continuous run until stopped.
- Aggregate target messages/second and 1–128 workers, persistent connections or new connections per message.
- Optional response reads, ACK status checks, control-ID correlation, retries/backoff and stop-on-first-error.
- Stop interrupts response reads and pacing waits, closes active sockets, and waits for worker cleanup before enabling a new operation. Connect/TLS setup may take its configured timeout to finish; DNS resolution follows operating-system resolver timing.

### Diagnostics & reproducibility

- Error stages distinguish connection, TLS, encoding/decoding, send, framing, response timeout, negative ACK and correlation problems.
- Failure records retain outbound bytes, received/partial bytes, ACK details, exception tracebacks, worker/sequence, control ID and elapsed time.
- Connection events retain peer/local addresses, connection timing and negotiated TLS cipher.
- Live totals, success/failure counts, ACK counts, attempts, throughput, mean and p50/p95/p99 latency.
- Bounded transaction/event capture (desktop: last 250), bounded desktop event queue/log and a failures-only transaction filter.
- Save/load JSON profiles; export JSON reports with **sensitive diagnostics omitted by default**.
- Headless CLI with script-friendly exit codes and the same transport/testing engine.

## Command line

```powershell
# One synthetic ADT with response and correlated ACK checking
python mirthquake.py send --host localhost --port 6661

# 1,000 messages at an aggregate target 50/sec using four connections
python mirthquake.py send --host mirth-test.example --port 6661 --count 1000 --rate 50 --workers 4 --report reports/load.json

# Test a file for 30 seconds, retaining its literal timestamp/control ID
python mirthquake.py send --file sample.hl7 --count 0 --duration 30 --keep-ids --verbose

# Inspect basic structure without a network connection
python mirthquake.py validate --file sample.hl7

# Receive outbound messages and return delayed negative ACKs
python mirthquake.py listen --host 0.0.0.0 --port 6662 --ack AE --delay 2 --duration 60 --report inbound.json

# Verified TLS (optionally add --cert-file / --key-file for client authentication)
python mirthquake.py send --host mirth-test.example --port 6661 --tls --ca-file test-ca.pem

# Use a desktop profile, overriding count and rate
python mirthquake.py send --profile test-profile.json --count 100 --rate 10

# Legacy XML protocol compatibility (not HL7 XML conformance)
python mirthquake.py send --sample xml --framing xml --no-ack-check

python mirthquake.py send --help
python mirthquake.py listen --help
```

Create the report directory before using it. `--report` exports on completion or Ctrl+C; use `--include-sensitive` only when storing full diagnostic data is appropriate. `--verbose` prints failure descriptions that may include message identifiers or other sensitive values.

Exit codes: **0** = test completed without recorded failures; **1** = message/validation/listener failure; **2** = invalid configuration, setup or file error; **130** = interrupted with Ctrl+C. A positive result with `--no-response` only confirms that the socket write completed.

## Important semantics and limits

- This is a **practical HL7 v2 TCP/MLLP testing tool**, not a full HL7 version-specific schema/terminology validator, clinical simulator, FHIR client, or Mirth administration API client.
- Outbound request/response mode expects **one response per message**. Enhanced-mode two-stage commit/application ACK workflows need separate testing. The simulator's ACK code is explicitly selected; it does not implement automatic MSH-15/MSH-16 acknowledgement choreography.
- `AA`/`CA` indicates acceptance at that ACK layer, **not downstream delivery**. Inspect Mirth's channel/message view for transformer and destination errors.
- Rates are **aggregate targets**, not guaranteed performance. Each worker has one outstanding exchange. Slow ACKs reduce achieved throughput; use the reported throughput, not only the requested rate. Latency includes connect time when a new connection is needed and retry delay/attempts when enabled.
- Counters and mean latency cover the whole run; percentiles use the most recent **10,000 completions**. Stopping an in-flight exchange can record it as a cancelled failure. Listener rejections/failure simulations appear as failed transactions intentionally.
- Fire-and-forget closes each connection regardless of the persistent setting, avoiding unread ACK accumulation.
- Raw TCP has **no universal message boundary**: outbound response reads collect until EOF or the response deadline. The inbound simulator requires a delimited framing mode. Custom delimiters must not occur within payloads; there is no delimiter escaping at the transport layer.
- Legacy XML framing is two leading NUL bytes and one trailing NUL byte, retained from the original tool; it is not a universal XML transport standard. For non-HL7 inbound payloads, disable HL7 validation and choose `none`/`close` unless an HL7 ACK is deliberately desired.
- The editor sends **one message at a time**. Multi-MSH files must be split before sending; batch envelopes/file-corpus replay are not implemented.
- Encoding selection does not rewrite MSH-18; make the HL7 metadata and actual encoding agree with the remote interface.
- Detailed captures are bounded by record/event count, not a total-byte budget. Large payloads multiply memory usage; lower the payload/history limits when necessary. Text previews are limited to 64 KiB, hex previews to 2 KiB; sensitive report export retains full retained payloads.

## Safety and privacy

Use **synthetic data and test endpoints**. A high-rate test can disrupt a real interface. No load test runs automatically.

**Retries are off by default.** A timeout may mean the message was processed but its ACK was lost. Retrying can cause duplicate delivery; retries deliberately reuse the same control ID. Coordinate duplicate handling with the receiving system.

Messages, identifiers, response text, errors and tracebacks are visible on-screen and in memory. Profiles include the message template and filesystem paths to certificate/key files (not the private key contents). Reports omit payloads, identifiers, ACK details, errors and tracebacks by default; they still contain endpoint/configuration metadata. Treat even metadata-only reports as potentially sensitive. Full reports and profiles are not encrypted. Do not commit real patient data, reports, profiles, or private keys.

## Development and tests

```powershell
python -m unittest discover -v
python -m compileall -q mirthquake.py hl7.py quake tests
```

The suite exercises fragmented/large/coalesced responses, framing mismatches, partial timeouts, disconnects, custom/XML/raw protocols, negative/mismatched ACKs, concurrent load, rate/duration, retries, cancellation, inbound validation, capture/redaction, profiles, CLI and desktop smoke behavior. Tk smoke tests skip where no display is available. TLS tests generate an ephemeral certificate with OpenSSL and skip if that executable is unavailable.

Structure: `quake/messages.py` (HL7/templates), `quake/transport.py` (TCP/framing/TLS), `quake/engine.py` (runner/listener/capture), `quake/gui.py` (Tk), `quake/cli.py` (headless). The original `hl7.py` compatibility API remains available, with complete writes/reads, configurable timeouts and corrected `AddLine` segment separators. Its `Connection.Send` still returns framed response bytes.

Optional standalone Windows executable:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --name MirthQuake mirthquake.py
```

Keep the console-enabled build if you also need CLI commands. Verify the generated executable on your target workstation before distribution.

## Attribution and license

Originally a small Mirth Connect stress tester, built on `hl7.py` from [Tremmors/pyMedicalSimulators](https://github.com/Tremmors/pyMedicalSimulators). MIT license; see [LICENSE](LICENSE).
