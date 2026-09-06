from investment_panel.core.contextual_assistant import build_contextual_packet, validate_contextual_response


def test_packet_citations_are_bound_and_missing_evidence_is_explicit():
    payload = {
        "ticker_decision": {
            "ticker": "AAA",
            "decision_revision": "rev-1",
            "as_of": "2026-09-06T12:00:00Z",
            "input_manifest": {"input_hash": "inputs-1"},
        },
        "dossier": {"sources": {"coverage": {"status": "missing", "sources": []}}},
    }
    packet = build_contextual_packet(payload, "AAA")
    decision_ref = "decision:AAA:rev-1"
    assert packet["packet_id"].startswith("packet:")
    assert "source_evidence" in packet["missing_evidence"]
    valid = validate_contextual_response(packet, [decision_ref])
    assert valid["citations"] == [decision_ref]
    invalid = validate_contextual_response(packet, ["invented-ref"], requested_calculation=True)
    assert invalid["citations"] == []
    assert any("outside the immutable packet" in item for item in invalid["limitations"])
    assert any("cannot calculate" in item for item in invalid["limitations"])

    changed_sources = {
        **payload,
        "dossier": {"sources": {"coverage": {"status": "available", "sources": ["sec"]}}},
    }
    assert build_contextual_packet(changed_sources, "AAA")["packet_id"] != packet["packet_id"]
