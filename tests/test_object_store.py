import datetime as dt
import json
from pathlib import Path

from app.services.object_store import ObjectStore


def test_put_json_serializes_datetime_payload(tmp_path: Path):
    store = ObjectStore()
    store._client = None
    store.local_dir = tmp_path

    payload = {
        "ts": dt.datetime(2026, 3, 23, 10, 0, tzinfo=dt.UTC),
        "nested": {"day": dt.date(2026, 3, 23)},
    }
    stored = store.put_json(parser_type="telegram", target_id=1, payload=payload, external_id="msg-1")

    assert stored.storage_type == "local"
    assert stored.payload_ref is not None

    saved = json.loads(Path(stored.payload_ref).read_text(encoding="utf-8"))
    assert saved["ts"] == "2026-03-23T10:00:00+00:00"
    assert saved["nested"]["day"] == "2026-03-23"
