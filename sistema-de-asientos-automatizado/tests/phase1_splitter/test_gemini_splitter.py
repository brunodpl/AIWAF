"""Tests del cliente Gemini y su parser de respuesta."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.phase1_splitter.gemini_splitter import detect_splits


def _fake_client(response_text: str):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text=response_text)
    return client


def test_detect_splits_single_factura():
    client = _fake_client(json.dumps({
        "facturas": [
            {"paginas": [1, 2], "emisor_cif_aparente": "B12345678", "confidence": 0.9}
        ]
    }))
    decision = detect_splits(client, b"%PDF", model="gemini-2.5-flash", n_pages_total=2)
    assert len(decision.groups) == 1
    assert decision.groups[0].pages == (1, 2)
    assert decision.groups[0].emisor_cif_aparente == "B12345678"
    assert decision.groups[0].confidence == pytest.approx(0.9)


def test_detect_splits_three_facturas():
    client = _fake_client(json.dumps({
        "facturas": [
            {"paginas": [1], "emisor_cif_aparente": "A", "confidence": 0.95},
            {"paginas": [2], "emisor_cif_aparente": "B", "confidence": 0.88},
            {"paginas": [3], "emisor_cif_aparente": "C", "confidence": 0.91},
        ]
    }))
    decision = detect_splits(client, b"%PDF", model="gemini-2.5-flash", n_pages_total=3)
    assert [g.pages for g in decision.groups] == [(1,), (2,), (3,)]


def test_detect_splits_strips_markdown_fence():
    client = _fake_client(
        "```json\n" + json.dumps({"facturas": [{"paginas": [1]}]}) + "\n```"
    )
    decision = detect_splits(client, b"%PDF", model="m", n_pages_total=1)
    assert decision.groups[0].pages == (1,)


def test_detect_splits_rejects_missing_pages():
    client = _fake_client(json.dumps({
        "facturas": [{"paginas": [1]}]
    }))
    with pytest.raises(ValueError, match="missing"):
        detect_splits(client, b"%PDF", model="m", n_pages_total=3)


def test_detect_splits_rejects_overlap():
    client = _fake_client(json.dumps({
        "facturas": [
            {"paginas": [1, 2]},
            {"paginas": [2, 3]},
        ]
    }))
    with pytest.raises(ValueError, match="more than one factura"):
        detect_splits(client, b"%PDF", model="m", n_pages_total=3)


def test_detect_splits_rejects_out_of_range():
    client = _fake_client(json.dumps({
        "facturas": [{"paginas": [5]}]
    }))
    with pytest.raises(ValueError, match="out of range"):
        detect_splits(client, b"%PDF", model="m", n_pages_total=2)


def test_detect_splits_rejects_empty_array():
    client = _fake_client(json.dumps({"facturas": []}))
    with pytest.raises(ValueError, match="non-empty 'facturas'"):
        detect_splits(client, b"%PDF", model="m", n_pages_total=1)


def test_detect_splits_rejects_invalid_json():
    client = _fake_client("not json {")
    with pytest.raises(ValueError, match="not valid JSON"):
        detect_splits(client, b"%PDF", model="m", n_pages_total=1)


def test_detect_splits_handles_missing_optional_fields():
    client = _fake_client(json.dumps({
        "facturas": [{"paginas": [1]}]
    }))
    decision = detect_splits(client, b"%PDF", model="m", n_pages_total=1)
    assert decision.groups[0].emisor_cif_aparente is None
    assert decision.groups[0].confidence is None
