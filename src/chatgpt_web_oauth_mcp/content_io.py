"""Lossless text decoding metadata shared by read and mechanical replace."""

from __future__ import annotations

import codecs
from dataclasses import dataclass


class TextDecodingError(ValueError):
    def __init__(self, message: str, *, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    bom: str | None
    bom_bytes: bytes
    newline: str


_BOMS: tuple[tuple[bytes, str, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32le", "utf-32le"),
    (codecs.BOM_UTF32_BE, "utf-32be", "utf-32be"),
    (codecs.BOM_UTF8, "utf-8", "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16le", "utf-16le"),
    (codecs.BOM_UTF16_BE, "utf-16be", "utf-16be"),
)


def newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    kinds = sum(bool(value) for value in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    if cr:
        return "cr"
    return "none"


def _canonical_encoding(value: str) -> str:
    try:
        return codecs.lookup(value).name
    except LookupError:
        raise TextDecodingError(f"Unknown text encoding: {value!r}.") from None


def decode_text_bytes(raw: bytes, *, encoding: str | None = None) -> DecodedText:
    bom_bytes = b""
    bom_name: str | None = None
    bom_encoding: str | None = None
    for marker, marker_encoding, marker_name in _BOMS:
        if raw.startswith(marker):
            bom_bytes = marker
            bom_encoding = marker_encoding
            bom_name = marker_name
            break

    if encoding is None:
        effective_encoding = bom_encoding or "utf-8"
    else:
        effective_encoding = _canonical_encoding(encoding)
        if bom_encoding is not None:
            compatible = {
                bom_encoding,
                "utf-8-sig" if bom_encoding == "utf-8" else "",
                "utf-16" if bom_encoding.startswith("utf-16") else "",
                "utf-32" if bom_encoding.startswith("utf-32") else "",
            }
            if effective_encoding not in compatible:
                raise TextDecodingError(
                    f"Explicit encoding {encoding!r} conflicts with the {bom_name} BOM."
                )
            effective_encoding = bom_encoding

    body = raw[len(bom_bytes) :] if bom_bytes else raw
    try:
        text = body.decode(effective_encoding, errors="strict")
    except UnicodeDecodeError as exc:
        if encoding is None:
            raise TextDecodingError(
                (
                    "Text encoding could not be detected conservatively. "
                    "Provide an explicit encoding."
                ),
                candidates=["windows-1252", "latin-1", "shift_jis", "gbk"],
            ) from exc
        raise TextDecodingError(
            f"File is not valid {encoding}: {exc}.",
        ) from exc

    if "\x00" in text[:1024] and bom_encoding is None and encoding is None:
        raise TextDecodingError(
            "NUL bytes indicate binary data; use mode='hex' or provide an explicit encoding."
        )
    return DecodedText(
        text=text,
        encoding=effective_encoding,
        bom=bom_name,
        bom_bytes=bom_bytes,
        newline=newline_style(text),
    )


def encode_text_bytes(decoded: DecodedText, text: str) -> bytes:
    return decoded.bom_bytes + text.encode(decoded.encoding, errors="strict")


def normalize_newlines(text: str, style: str) -> str:
    if style not in {"crlf", "cr"}:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n" if style == "crlf" else "\r")
