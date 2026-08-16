"""Masking of customer-identifying values, applied at the collection boundary.

**Why here and not at the model call.** The specification asks for "redaction before model calls and
traces". Doing it *only* there would be too late. Graph state is checkpointed on every super-step
and every node logs; an unmasked client MAC that arrives from the CPE adapter is therefore already
in the checkpoint row, the log stream and the trace before any model is ever asked anything.
Masking at the point of collection -- inside the CPE adapter's KPI-extraction step, before the
payload becomes a `WifiRadioSnapshot` -- is what makes the later boundaries cheap:
`redact_for_model` and the structlog processor are then a second line of defence over data that
should already be clean, rather than the only line.

So there are three places this module is called, in order of importance:

1. the CPE adapter, on the raw TR-181 tree (collection boundary);
2. the structlog processor in `observability.logging`, over every event dict (never forgotten);
3. `redact_for_model`, immediately before a language-model call (strictest, and drops keys).

Masking is deterministic -- the same input always yields the same mask -- so two log lines about the
same device correlate. It is not reversible: there is no key and no lookup table here. Recovering
the original value requires the source system, which is the point.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

# --------------------------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------------------------

_MAC_COLON: Final = re.compile(
    r"^([0-9A-Fa-f]{2})([:-])([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})\2"
    r"([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})$"
)
_MAC_DOT: Final = re.compile(r"^([0-9A-Fa-f]{4})\.([0-9A-Fa-f]{4})\.([0-9A-Fa-f]{4})$")
_MAC_BARE: Final = re.compile(r"^[0-9A-Fa-f]{12}$")

_IPV4: Final = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# Deliberately conservative: 7-15 digits with optional +, spaces, dashes, dots, parens. A looser
# pattern matches serial numbers and firmware versions, and masking those destroys diagnosis.
_PHONE: Final = re.compile(r"^\+?[\d][\d\s().-]{5,17}\d$")
_PHONE_DIGITS: Final = re.compile(r"\D")

# Embedded occurrences, for free text and for values that carry an identifier inside a sentence.
_MAC_EMBEDDED: Final = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"
    r"|\b(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}\b"
)
_EMAIL_EMBEDDED: Final = re.compile(r"\b[^@\s]+@[^@\s]+\.[A-Za-z]{2,}\b")
_IPV4_EMBEDDED: Final = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# NANP shape only (3-3-4, optional +1). Deliberately narrower than `_PHONE`: a looser embedded
# pattern matches an ISO date, a codeword count or a firmware build number inside prose, and masking
# one of those in a technician note destroys the note's diagnostic value.
_PHONE_EMBEDDED: Final = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")

MASK: Final = "**"
REDACTED: Final = "[REDACTED]"
DROPPED: Final = "[DROPPED]"

# --------------------------------------------------------------------------------------------
# Key sets
# --------------------------------------------------------------------------------------------
#
# Keys are matched case-insensitively after stripping `_`, `-` and `.`, so `client_mac`,
# `clientMAC` and `client.mac` are one entry rather than three.
#
# Judgement calls, stated rather than left implicit:
#
# * `ssid` IS treated as PII. A residential SSID is very commonly the family surname or the street
#   address ("Casa Rivera", "Calle Luna 12"), and it is also a geolocation primitive through public
#   wardriving databases. The cost of masking it is that a Wi-Fi interference diagnosis loses a
#   human-friendly label; `bssid`-derived masks and the CPE ref carry the identity the diagnosis
#   actually needs, so that cost is low.
# * `lat`/`lon`/`latitude`/`longitude` are NOT masked. They are on `TopologyContext` and
#   `DispatchRequirement` because the dispatch optimizer computes travel time from them, and a
#   masked coordinate is a coordinate that routes a crew to the wrong mountain. They are premises
#   locations, so they are handled by access control and by not exporting them to third parties --
#   not by masking. `address` and `street` ARE masked: those are free-text and are not used for any
#   arithmetic.
# * `serial_number` is NOT masked. It identifies a device we own, appears on the work order the
#   technician reads, and is the join key to inventory.
_PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        # link-layer identity
        "mac",
        "macaddress",
        "macaddr",
        "physaddress",
        "bssid",
        "clientmac",
        "stationmac",
        "apmac",
        "cpemac",
        "cmmac",
        "onumac",
        "hostname",
        "ssid",
        # network identity
        "ip",
        "ipaddress",
        "ipv4",
        "ipv6",
        "ipaddr",
        "wanip",
        "lanip",
        "publicip",
        # human identity
        "email",
        "emailaddress",
        "phone",
        "phonenumber",
        "mobile",
        "msisdn",
        "contactnumber",
        "name",
        "firstname",
        "lastname",
        "surname",
        "fullname",
        "customername",
        "subscribername",
        "accountholder",
        "address",
        "street",
        "streetaddress",
        "addressline1",
        "addressline2",
        "postcode",
        "zipcode",
        "nationalid",
        "taxid",
        "dob",
        "dateofbirth",
    }
)

# Keys whose value is dropped entirely -- not masked -- before a model call. Three families:
#
# 1. raw telemetry blobs. `redact_for_model` is also the model input-size control the specification
#    asks for, and a TR-181 subtree or a spectrum capture is both large and useless to a narrative
#    model, which is fed the already-computed bands and verdict (D6).
# 2. customer identifiers. `customer_ref` and `subscriber_id` are pseudonymous, but they are the
#    join key back to a named person, and a model prompt is the one place we gain nothing by
#    carrying them.
# 3. credentials. Should never be in a payload at all; if one is, it must not become a prompt.
MODEL_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        # raw telemetry / blobs. `rawpayload` is `records.AssuranceEvent.raw_payload` and `cperaw`
        # is `detectors.base.DetectorContext.cpe_raw`; both are named here rather than left to a
        # shape heuristic, because a TR-181 subtree looks like an ordinary nested dict.
        "rawpayload",
        "raw",
        "rawtree",
        "rawtelemetry",
        "rawresponse",
        "cperaw",
        "spectrum",
        "spectrumcapture",
        "pnmcapture",
        "samples",
        "timeseries",
        "photo",
        "photos",
        "photobytes",
        "attachment",
        "attachments",
        "payloadbytes",
        "body",
        # customer identity
        "customerref",
        "customerid",
        "subscriberid",
        "subscriberref",
        "accountnumber",
        "accountid",
        "billingaccount",
        "premiseid",
        # credentials
        "password",
        "secret",
        "token",
        "apikey",
        "authorization",
        "credentials",
        "webhooksecret",
        "privatekey",
    }
)

# Free-text fields that a model legitimately needs to read. Kept, but they are untrusted input:
# `security.injection.assert_data_not_instruction` is what makes them safe to include, and
# `redact_for_model` routes them through it. Named here so the two modules cannot disagree about
# which fields are untrusted.
UNTRUSTED_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "techniciannote",
        "techniciannotes",
        "customerstatement",
        "customerresponse",
        "customerresponses",
        "note",
        "notes",
        "detail",
        "summary",
        "instructions",
        "accessnotes",
        "safetynotes",
        "rejectiondetail",
        "knowledgeexcerpt",
        "documentexcerpt",
    }
)

_IP_KEYS: Final[frozenset[str]] = frozenset(
    {"ip", "ipaddress", "ipv4", "ipv6", "ipaddr", "wanip", "lanip", "publicip"}
)

MAX_DEPTH: Final = 12
"""Recursion bound for `redact`.

A payload from an external adapter may be self-referential (a JSON graph deserialised into shared
dicts, or a hand-built structure in a test), and a masker that recurses without a bound turns that
into a `RecursionError` inside a log processor -- which is to say, into a lost log line at exactly
the moment something is going wrong. Twelve is deeper than any payload shape in this system;
anything beyond it is replaced with a marker rather than dropped, so truncation is visible.
"""

_TRUNCATED: Final = "[REDACTION_DEPTH_EXCEEDED]"


def _normalise_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").replace(".", "").lower()


# --------------------------------------------------------------------------------------------
# Field maskers
# --------------------------------------------------------------------------------------------


def mask_mac(value: str) -> str:
    """Mask a MAC keeping only the OUI: `AA:BB:CC:DD:EE:FF` -> `AA:BB:CC:**:**:**`.

    The OUI survives because it is the device *vendor*, not the device: "every affected CPE is a
    Technicolor" is a real diagnostic signal for a firmware-correlated fault, and it identifies no
    customer.

    `-` and `.` separators and the bare 12-hex form are all handled. **A string that is not a MAC is
    returned unchanged**: this function's job is masking, not sanitising arbitrary input, and a
    masker that mangles whatever it does not recognise would quietly corrupt a serial number, a
    firmware version or a plant reference the moment a caller pointed it at the wrong field.
    """
    text = value.strip()
    colon = _MAC_COLON.match(text)
    if colon:
        sep = colon.group(2)
        oui = sep.join(colon.group(1, 3, 4))
        return f"{oui}{sep}{MASK}{sep}{MASK}{sep}{MASK}"
    dot = _MAC_DOT.match(text)
    if dot:
        # Cisco three-group form: the OUI is the first group plus half the second.
        return f"{dot.group(1)}.{dot.group(2)[:2]}{MASK}.{MASK}{MASK}"
    if _MAC_BARE.match(text):
        return f"{text[:6]}{MASK * 3}"
    return value


def mask_ip(value: str) -> str:
    """Keep the first two octets of an IPv4 address, mask the host part of an IPv6 one.

    Two octets rather than none because the /16 is the operator's address block, which is how a
    provisioning fault that affects one CGNAT pool is recognised. A non-IP string is returned
    unchanged, for the same reason as `mask_mac`.
    """
    text = value.strip()
    v4 = _IPV4.match(text)
    if v4:
        octets = [int(g) for g in v4.groups()]
        if all(0 <= o <= 255 for o in octets):
            return f"{octets[0]}.{octets[1]}.{MASK}.{MASK}"
        return value
    if ":" in text and len(text) >= 3:
        head = text.split(":")
        # An IPv6 /32 is the routing prefix; everything after it can identify an interface.
        if len(head) >= 3:
            return ":".join(head[:2]) + f":{MASK}:{MASK}"
    return value


def mask_email(value: str) -> str:
    """`maria.rivera@example.com` -> `m****a@example.com`.

    The domain survives because "all the affected customers are on one ISP's mail" is occasionally
    the explanation, and a domain identifies no individual. First and last characters of the local
    part survive so two log lines about the same customer correlate without naming them.
    """
    text = value.strip()
    if not _EMAIL.match(text):
        return value
    local, _, domain = text.partition("@")
    if len(local) <= 2:
        return f"{MASK * 2}@{domain}"
    return f"{local[0]}{'*' * 4}{local[-1]}@{domain}"


def mask_phone(value: str) -> str:
    """Keep the last two digits: `+1 787 555 0142` -> `+**-**-**42`.

    Last two rather than the country code: a Puerto Rico number is `+1 787` or `+1 939` for
    essentially every customer, so the prefix carries no information and the suffix is what a
    service desk agent uses to confirm they are looking at the right record.
    """
    text = value.strip()
    if not _PHONE.match(text):
        return value
    digits = _PHONE_DIGITS.sub("", text)
    if len(digits) < 7:
        # Too short to be a subscriber number; likelier a code or a count. Leave it alone.
        return value
    return f"{MASK}-{MASK}-{MASK}{digits[-2:]}"


def mask_name(value: str) -> str:
    """Initials only: `Maria Rivera` -> `M. R.`.

    Enough to tell two people apart in a timeline, not enough to identify either. An empty or
    whitespace-only value returns `[REDACTED]` rather than an empty string, so a masked field never
    looks like an absent one.
    """
    text = value.strip()
    if not text:
        return REDACTED
    parts = [p for p in re.split(r"\s+", text) if p]
    return " ".join(f"{p[0].upper()}." for p in parts)


def looks_like_pii(value: str) -> bool:
    """Whether a value carries a MAC, e-mail or phone shape regardless of the key it arrived under.

    Used by `redact` for the case the key set cannot cover: a vendor that returns
    `{"associatedDevice": "AA:BB:CC:DD:EE:FF"}` under a key nobody predicted.
    """
    text = value.strip()
    return bool(
        _MAC_COLON.match(text) or _MAC_DOT.match(text) or _EMAIL.match(text) or _PHONE.match(text)
    )


def mask_by_shape(value: str) -> str:
    """Mask a value by what it looks like. Order matters: MAC before phone.

    A bare `787555014212` would satisfy both the phone and (were it hex) the MAC pattern; MAC is
    checked first because a mis-masked MAC still hides the device, whereas a MAC treated as a phone
    number leaks four octets.
    """
    text = value.strip()
    if _MAC_COLON.match(text) or _MAC_DOT.match(text):
        return mask_mac(value)
    if _EMAIL.match(text):
        return mask_email(value)
    if _PHONE.match(text):
        return mask_phone(value)
    return value


def mask_free_text(value: str) -> str:
    """Mask identifiers embedded inside prose.

    A technician note reading "swapped the ONT, new MAC AA:BB:CC:DD:EE:FF, called 787-555-0142" is
    the commonest way a MAC reaches a log line without ever appearing under a MAC-shaped key.
    """
    out = _MAC_EMBEDDED.sub(lambda m: mask_mac(m.group(0)), value)
    out = _EMAIL_EMBEDDED.sub(lambda m: mask_email(m.group(0)), out)
    out = _IPV4_EMBEDDED.sub(lambda m: mask_ip(m.group(0)), out)
    return _PHONE_EMBEDDED.sub(lambda m: f"{MASK}-{MASK}-{MASK}{m.group(0)[-2:]}", out)


def _mask_for_key(normalised_key: str, value: str) -> str:
    """Mask a string knowing the key it arrived under. The key wins over the shape.

    `{"customer_name": "AA:BB:CC:DD:EE:FF"}` is masked as a name, because whatever that value is,
    the key says it is a name and the shape check would leave three octets of it in place.
    """
    if normalised_key in {"email", "emailaddress"}:
        return mask_email(value)
    if normalised_key in {"phone", "phonenumber", "mobile", "msisdn", "contactnumber"}:
        return mask_phone(value)
    if "mac" in normalised_key or normalised_key in {"bssid", "physaddress"}:
        masked = mask_mac(value)
        # `mask_mac` returns its input unchanged when the value is not MAC-shaped. Under a MAC key
        # that means the field holds something else, and whatever it is, it does not get to pass.
        return masked if masked != value else REDACTED
    if normalised_key in _IP_KEYS:
        return mask_ip(value)
    if "name" in normalised_key or normalised_key == "surname":
        return mask_name(value)
    # Everything else in the PII key set -- addresses, SSIDs, hostnames, identifiers -- has no
    # partial form worth keeping.
    return REDACTED


# --------------------------------------------------------------------------------------------
# The walkers
# --------------------------------------------------------------------------------------------


def redact(payload: Any, *, depth: int = 0) -> Any:
    """Return a copy of `payload` with customer-identifying values masked.

    Does not mutate its argument: log processors and audit builders hand this the live object they
    are about to file, and a masker that edited in place would silently change what the incident
    state holds. Every container is rebuilt.

    Depth-bounded at `MAX_DEPTH`. Values deeper than that are replaced by a visible marker rather
    than dropped, because a cyclic or pathologically nested payload is itself a fact worth seeing.

    Masking happens for two independent reasons, either sufficient: the KEY is in `_PII_KEYS`, or
    the VALUE has the shape of a MAC, e-mail or phone number.
    """
    if depth > MAX_DEPTH:
        return _TRUNCATED

    if isinstance(payload, str):
        return mask_free_text(mask_by_shape(payload))

    # bool before int: bool is a subclass of int and both are pass-through, but being explicit here
    # documents that no scalar is masked. A measurement is never PII.
    if isinstance(payload, bool | int | float | bytes | type(None)):
        return payload

    if isinstance(payload, Mapping):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                out[key] = redact(value, depth=depth + 1)
                continue
            normalised = _normalise_key(key)
            if normalised in _PII_KEYS and isinstance(value, str):
                out[key] = _mask_for_key(normalised, value)
            elif (
                normalised in _PII_KEYS
                and isinstance(value, Sequence)
                and not isinstance(value, str | bytes)
            ):
                # `associated_macs: [...]` -- the key applies to every element.
                out[key] = [
                    _mask_for_key(normalised, v)
                    if isinstance(v, str)
                    else redact(v, depth=depth + 1)
                    for v in value
                ]
            elif normalised in _PII_KEYS:
                out[key] = REDACTED if value is not None else None
            else:
                out[key] = redact(value, depth=depth + 1)
        return out

    if isinstance(payload, tuple):
        return tuple(redact(v, depth=depth + 1) for v in payload)

    if isinstance(payload, set | frozenset):
        return {redact(v, depth=depth + 1) for v in payload}

    if isinstance(payload, Sequence):
        return [redact(v, depth=depth + 1) for v in payload]

    # A Pydantic model, a dataclass, an enum, a datetime. Rendered through `str()` and masked by
    # shape rather than reached into: a model's fields are validated and its `__str__` is what a log
    # line would have shown anyway. `mask_free_text` catches an identifier inside that rendering.
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump(mode="json")
        return redact(dumped, depth=depth + 1)
    return mask_free_text(mask_by_shape(str(payload)))


def redact_for_model(payload: Any) -> Any:
    """The strict boundary applied immediately before any language-model call.

    Three differences from `redact`:

    1. keys in `MODEL_FORBIDDEN_KEYS` are **dropped**, not masked. A masked raw-telemetry blob is
       still a large blob, and the specification's model input-size limit is easier to hold by not
       sending it. The key is replaced by a `[DROPPED]` marker so a prompt-review test can see that
       a drop happened rather than guessing whether the field was ever present.
    2. free text in `UNTRUSTED_TEXT_KEYS` is kept -- a technician note is often the only account of
       what was found -- but routed through `injection.assert_data_not_instruction`, which wraps it
       in a delimiter block. See that module for what this does and does not buy.
    3. everything else is masked exactly as `redact` masks it.

    Called by the model provider wrapper, not by each prompt builder: a boundary that every caller
    must remember to cross is a boundary that will be walked around.
    """
    # Imported here rather than at module scope: `injection` imports nothing from this module today,
    # but the two are a natural pair and a top-level import would make the cycle a landmine for
    # whoever adds the first cross-reference.
    from lpr_cpe.security.injection import assert_data_not_instruction

    def _walk(value: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            return _TRUNCATED
        if isinstance(value, Mapping):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    out[key] = _walk(item, depth + 1)
                    continue
                normalised = _normalise_key(key)
                if normalised in MODEL_FORBIDDEN_KEYS:
                    out[key] = DROPPED
                elif normalised in UNTRUSTED_TEXT_KEYS and isinstance(item, str) and item:
                    out[key] = assert_data_not_instruction(key, str(redact(item)))
                else:
                    out[key] = _walk(item, depth + 1)
            return out
        if isinstance(value, tuple):
            return tuple(_walk(v, depth + 1) for v in value)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            return [_walk(v, depth + 1) for v in value]
        if hasattr(value, "model_dump") and not isinstance(value, str | bytes):
            return _walk(value.model_dump(mode="json"), depth + 1)
        return redact(value, depth=depth)

    return _walk(payload, 0)
