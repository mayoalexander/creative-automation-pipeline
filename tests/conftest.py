import pytest


@pytest.fixture(autouse=True)
def spend_ledger(tmp_path, monkeypatch):
    """Runs in tests write their spend lines to the test's own folder, never to the repo's spend.jsonl."""
    monkeypatch.setenv("PIPELINE_SPEND_LEDGER", str(tmp_path / "spend.jsonl"))
    return tmp_path / "spend.jsonl"
