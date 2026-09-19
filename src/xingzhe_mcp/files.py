"""Small inline files for remote MCP clients; no local paths or arbitrary URL fetching."""

import base64
import binascii
import re
from xml.etree import ElementTree

MAX_FILE_BYTES = 1_000_000
MAX_BASE64_LENGTH = 4 * ((MAX_FILE_BYTES + 2) // 3)


def gpx_bytes(content: str) -> bytes:
    raw = content.encode("utf-8")
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"GPX must contain 1–{MAX_FILE_BYTES} UTF-8 bytes")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", content, re.IGNORECASE):
        raise ValueError("GPX must not contain DTD or entity declarations")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("GPX must be valid XML") from exc
    if root.tag not in (
        "gpx",
        "{http://www.topografix.com/GPX/1/0}gpx",
        "{http://www.topografix.com/GPX/1/1}gpx",
    ):
        raise ValueError("Expected a GPX document")
    return raw


def fit_bytes(content: str) -> bytes:
    if not content or len(content) > MAX_BASE64_LENGTH:
        raise ValueError(f"FIT must contain 1–{MAX_FILE_BYTES} bytes, encoded as base64")
    try:
        raw = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("FIT content must be standard base64 without a data URL prefix") from exc
    if len(raw) > MAX_FILE_BYTES or len(raw) < 14 or raw[8:12] != b".FIT":
        raise ValueError("Invalid FIT header or file exceeds the upload limit")
    return raw
