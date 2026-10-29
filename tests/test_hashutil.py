import hashlib

from agenttrace.hashutil import canonical_json, estimate_cost_usd, mean, percentile, sha256_hex


def test_canonical_hash_is_full_sha256_of_sorted_json():
    payload = {"model": "orders_daily"}
    assert canonical_json(payload) == '{"model":"orders_daily"}'
    digest = hashlib.sha256(b'{"model":"orders_daily"}').hexdigest()
    assert sha256_hex(payload) == digest
    assert digest.startswith("aacc0ee36ca5cf91")


def test_missing_state_hashes_json_null():
    assert sha256_hex(None) == hashlib.sha256(b"null").hexdigest()
    assert sha256_hex(None).startswith("74234e98afe7498f")


def test_cost_card_and_unknown_model():
    card = {"mock-triage-v1": {"input_usd_per_million": 0.15, "output_usd_per_million": 0.60}}
    assert estimate_cost_usd("mock-triage-v1", 1000, 1000, card) == 0.00075
    assert estimate_cost_usd("qwen3.5:9b", 1000, 1000, card) == 0.0


def test_linear_percentile():
    assert percentile([], 50) is None
    assert percentile([10], 95) == 10
    assert percentile([10, 20, 30, 40, 50], 50) == 30
    assert percentile([10, 20, 30, 40, 50], 95) == 48
    assert mean([10, 20]) == 15
