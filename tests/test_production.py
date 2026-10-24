"""Production behaviour: no server keys, shared state in Redis (faked here), read-only code folder."""

import json

from backend import config
from backend.memory import Memory, Turn
from backend.tools.cart import CartStore


class FakeKV:
    """Same interface as backend.kv.RedisKV, backed by a dict shared like one Redis database."""

    def __init__(self):
        self.data = {}

    def get_json(self, key):
        raw = self.data.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key, value, ttl_s):
        self.data[key] = json.dumps(value)

    def push_json(self, key, value, ttl_s):
        self.data.setdefault(key + ":list", []).append(value)

    def delete(self, key):
        self.data.pop(key, None)


def test_server_keys_are_never_used_in_production(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    monkeypatch.setattr(config, "PRODUCTION", False)
    assert config.api_key("openai") == "sk-server"
    monkeypatch.setattr(config, "PRODUCTION", True)
    assert config.api_key("openai") is None
    monkeypatch.setenv("NIMBUS_ALLOW_SERVER_KEYS", "1")  # explicit opt-in for a private deployment
    assert config.api_key("openai") == "sk-server"


def test_two_instances_share_one_cart(tmp_path):
    redis = FakeKV()
    a, b = CartStore(tmp_path / "a.json", kv=redis), CartStore(tmp_path / "b.json", kv=redis)
    a.add("cart-1", "crm", "professional", seats=2)
    assert b.view("cart-1")["lines"][0]["seats"] == 2      # the other instance sees the add
    b.add("cart-1", "crm", "professional", seats=1)
    assert a.view("cart-1")["totals"]["seats"] == 3
    order = a.checkout("cart-1")["order"]
    assert b.view("cart-1")["lines"] == [] and redis.data["orders:cart-1:list"][0]["order_id"] == order["order_id"]
    assert not (tmp_path / "a.json").exists()                # nothing written to disk


def test_two_instances_share_one_conversation():
    redis = FakeKV()
    a, b = Memory(kv=redis), Memory(kv=redis)
    conv = a.get("sess-1")
    conv.turns.append(Turn(user="add crm", assistant="Added.", actions=["add_to_cart(product=crm) -> ok"]))
    conv.last_tools = {"add_to_cart"}
    a.save(conv)
    other = b.get("sess-1")                                  # e.g. a voice reconnect on another instance
    assert [t.user for t in other.turns] == ["add crm"] and other.last_tools == {"add_to_cart"}
    b.record_interrupted("sess-1", "tell me more", "CRM is", [], already_recorded=False)
    assert a.get("sess-1").turns[-1].cancelled
    a.reset("sess-1")
    assert b.get("sess-1").turns[-1].user == "tell me more"  # b's local copy remains, but Redis is empty
    assert "conv:sess-1" not in redis.data


def test_index_is_read_from_the_shipped_folder_but_built_in_the_writable_one(monkeypatch, tmp_path):
    from backend.rag import build_index

    shipped, writable = tmp_path / "shipped", tmp_path / "tmp"
    monkeypatch.setattr(build_index, "SHIPPED_STORE_DIR", shipped)
    monkeypatch.setattr(build_index, "STORE_DIR", writable)
    assert build_index.store_path("bge-small") == writable / "bge-small"   # nothing shipped yet
    (shipped / "bge-small").mkdir(parents=True)
    (shipped / "bge-small" / "manifest.json").write_text(json.dumps(
        {"chunks_hash": build_index.chunks_hash(), "umap": build_index.UMAP_PARAMS}))
    assert build_index.store_path("bge-small") == shipped / "bge-small"
