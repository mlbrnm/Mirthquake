"""Practical HL7 v2 inspection, templates and acknowledgements (not a schema validator)."""

from dataclasses import dataclass
from datetime import datetime
import re
import uuid


SAMPLES = {
    "ADT · admission": (
        "MSH|^~\\&|MIRTHQUAKE|TEST|MIRTH|TEST|{{timestamp}}||ADT^A01|{{control_id}}|T|2.5\r"
        "EVN|A01|{{timestamp}}\r"
        "PID|1||TEST{{sequence}}^^^TEST^MR||PATIENT^SYNTHETIC||19800101|U\r"
        "PV1|1|I|TEST^101^1||||1234^PROVIDER^TEST\r"
    ),
    "ORU · observation": (
        "MSH|^~\\&|MIRTHQUAKE|TEST|MIRTH|TEST|{{timestamp}}||ORU^R01|{{control_id}}|T|2.5\r"
        "PID|1||TEST{{sequence}}^^^TEST^MR||PATIENT^SYNTHETIC\r"
        "OBR|1||ORDER{{sequence}}|TEST^Synthetic panel^L|||{{timestamp}}\r"
        "OBX|1|NM|1234^Synthetic result^L||42|mg/dL|0-100|N|||F\r"
    ),
    "ORM · order": (
        "MSH|^~\\&|MIRTHQUAKE|TEST|MIRTH|TEST|{{timestamp}}||ORM^O01|{{control_id}}|T|2.5\r"
        "PID|1||TEST{{sequence}}^^^TEST^MR||PATIENT^SYNTHETIC\r"
        "ORC|NW|ORDER{{sequence}}\r"
        "OBR|1|ORDER{{sequence}}||TEST^Synthetic order^L\r"
    ),
    "XML · legacy NUL framed": "<test><id>{{control_id}}</id><sequence>{{sequence}}</sequence></test>",
}


def normalize(text):
    """Convert editor/file newlines to HL7 segment terminators, without trimming fields."""
    text = text.removeprefix("\ufeff")
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    if text.startswith("\x0b") and text.endswith("\x1c\r"):
        text = text[1:-2]
    return text.rstrip("\r") + "\r" if text else ""


@dataclass(frozen=True)
class Issue:
    severity: str
    location: str
    description: str


class HL7:
    def __init__(self, text):
        self.text = normalize(text)
        self.segments = [s for s in self.text.split("\r") if s]
        self.separator = self.text[3] if self.text.startswith("MSH") and len(self.text) > 3 else "|"
        self.encoding_chars = self.field("MSH", 2) or "^~\\&"

    def field(self, segment, number, occurrence=1):
        found = [s for s in self.segments if s[:3] == segment]
        if occurrence < 1 or len(found) < occurrence:
            return ""
        parts = found[occurrence - 1].split(self.separator)
        if segment == "MSH" and number == 1:
            return self.separator
        index = number - 1 if segment == "MSH" else number
        return parts[index] if 0 <= index < len(parts) else ""

    @property
    def control_id(self):
        return self.field("MSH", 10)

    def fields(self):
        occurrences = {}
        for segment in self.segments:
            name = segment[:3]
            occurrences[name] = occurrences.get(name, 0) + 1
            prefix = f"{name}[{occurrences[name]}]"
            if name == "MSH":
                yield prefix + "-1", self.separator
            for i, value in enumerate(segment.split(self.separator)[1:], 2 if name == "MSH" else 1):
                yield f"{prefix}-{i}", value

    def validate(self):
        issues = []
        if not self.segments or not self.text.startswith("MSH"):
            return [Issue("ERROR", "MSH", "Message must start with an MSH segment.")]
        if len(self.text) < 4 or self.separator.isalnum() or self.separator.isspace():
            issues.append(Issue("ERROR", "MSH-1", "Field separator must be a non-alphanumeric delimiter."))
        enc = self.field("MSH", 2)
        if len(enc) not in (4, 5) or len(set(enc + self.separator)) != len(enc) + 1:
            issues.append(Issue("ERROR", "MSH-2", "Expected four (or five) distinct encoding characters, distinct from MSH-1."))
        for number, label in ((7, "timestamp"), (9, "message type"), (10, "control ID"), (11, "processing ID"), (12, "version")):
            if not self.field("MSH", number):
                issues.append(Issue("ERROR", f"MSH-{number}", f"Missing {label}."))
        if self.field("MSH", 11) not in ("T", "D"):
            issues.append(Issue("WARNING", "MSH-11", "Not marked T (test) or D (debug). Verify this is not a production endpoint."))
        for i, segment in enumerate(self.segments, 1):
            if not re.match(r"^[A-Z0-9]{3}" + re.escape(self.separator), segment):
                issues.append(Issue("ERROR", f"segment {i}", "Expected a three-character segment ID followed by the field separator."))
            if i > 1 and segment.startswith("MSH" + self.separator):
                issues.append(Issue("ERROR", f"segment {i}", "Multiple MSH segments: split the file into individual messages before sending."))
        if any(ord(c) < 32 and c != "\r" for c in self.text):
            issues.append(Issue("ERROR", "payload", "Unexpected control characters inside HL7 payload (possibly double framing)."))
        return issues


def render_template(text, sequence=1, refresh=False, hl7=True):
    values = {"timestamp": datetime.now().strftime("%Y%m%d%H%M%S"),
              "control_id": uuid.uuid4().hex, "sequence": str(sequence)}
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    if "{{" in text or "}}" in text:
        raise ValueError("Unknown template token. Supported: {{timestamp}}, {{control_id}}, {{sequence}}.")
    if hl7:
        text = normalize(text)
        if refresh and text.startswith("MSH") and len(text) > 3:
            parts = text.split("\r")
            fields = parts[0].split(text[3])
            if len(fields) < 12:
                raise ValueError("Cannot refresh MSH-7/MSH-10: MSH has too few fields.")
            fields[6], fields[9] = values["timestamp"], values["control_id"]
            parts[0] = text[3].join(fields)
            text = "\r".join(parts)
    return text


def build_ack(text, code="AA", error="", application="MIRTHQUAKE"):
    if code not in ("AA", "AE", "AR", "CA", "CE", "CR"):
        raise ValueError("Invalid acknowledgement code.")
    msg = HL7(text)
    sep = msg.separator
    enc = msg.field("MSH", 2) or "^~\\&"
    # Keep error descriptions from injecting fields/segments into the acknowledgement.
    safe_error = "".join(" " if c in sep + enc or ord(c) < 32 else c for c in error)[:512]
    trigger = msg.field("MSH", 9).split(enc[0])
    msg_type = "ACK" + (enc[0] + trigger[1] if len(trigger) > 1 else "")
    msh = ["MSH", enc, application, msg.field("MSH", 6), msg.field("MSH", 3),
           msg.field("MSH", 4), datetime.now().strftime("%Y%m%d%H%M%S"), "", msg_type,
           uuid.uuid4().hex, msg.field("MSH", 11) or "T", msg.field("MSH", 12) or "2.5"]
    return sep.join(msh) + "\r" + sep.join(["MSA", code, msg.control_id, safe_error]) + "\r"


def inspect_ack(text, expected_id="", correlate=True):
    msg = HL7(text)
    code, control_id = msg.field("MSA", 1), msg.field("MSA", 2)
    errors = [s for s in msg.segments if s.startswith("ERR" + msg.separator)]
    detail = msg.field("MSA", 3)
    if code not in ("AA", "AE", "AR", "CA", "CE", "CR"):
        raise ValueError("Response is not a recognised HL7 ACK: missing/invalid MSA-1. Check Mirth response settings and framing.")
    if correlate and expected_id and control_id != expected_id:
        raise ValueError(f"ACK correlation mismatch: sent MSH-10={expected_id!r}, received MSA-2={control_id!r}.")
    return {"code": code, "control_id": control_id, "accepted": code in ("AA", "CA"),
            "detail": detail, "errors": errors}
