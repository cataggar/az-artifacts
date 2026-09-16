"""Reference-only instrumentation must never record credential/capability material."""

import json
from types import SimpleNamespace

import httpx
from interop.native_trace import NativeTrace, safe_shape

from az_artifacts._http import Response


def test_native_trace_redacts_capabilities_and_signed_urls(tmp_path):
    identifier = "A" * 64 + "01"
    response = Response(
        json.dumps({identifier: {"Signature": "never-record-receipt"}}).encode(),
        httpx.Headers({"set-cookie": "never-record-cookie"}),
        200,
    )
    http = SimpleNamespace(_request_once=lambda *args, **kwargs: response)
    path = tmp_path / "safe.jsonl"
    trace = NativeTrace(http, path)
    http._request_once(
        "PUT",
        "https://org.vsblob.visualstudio.com/A1/_apis/dedup/chunks",
        authenticated=True,
        content=b"abc",
        json_body=None,
        headers={
            "Authorization": "Bearer never-record-token",
            "X-MS-Signature": "never-record-summary",
            "X-ms-chunk-" + identifier: "3/false",
        },
    )
    http._request_once(
        "GET",
        "https://blob.example/never-record-path?sig=never-record-sas",
        authenticated=False,
        content=None,
        json_body=None,
        headers={},
    )
    assert trace.summary()["successful_chunk_body_bytes"] == 3
    trace.close()
    text = path.read_text()
    assert "never-record" not in text
    assert "?sig=" not in text
    assert "authorization" not in text.lower()
    assert "<signed-blob-download>" in text


def test_retention_shapes_preserve_structure_but_never_signature_values():
    identifier = "A" * 64 + "01"
    shape = safe_shape(
        {
            "Receipts": {
                identifier: {
                    "Signature": [1, 2, 3],
                    "KeepUntil": {"KeepUntil": "2030-01-02T03:04:05Z"},
                }
            },
            "Missing": [identifier],
            "other": "never-record-this",
        }
    )
    assert shape["Receipts"][identifier]["Signature"] == "<redacted>"
    assert shape["Receipts"][identifier]["KeepUntil"]["KeepUntil"] == "2030-01-02T03:04:05Z"
    assert shape["Missing"] == [identifier]
    assert "never-record-this" not in json.dumps(shape)
