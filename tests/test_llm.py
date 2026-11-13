import dataclasses
from types import SimpleNamespace

import pytest

from repo_index import db, llm, sync
from repo_index.__main__ import main, run_update
from repo_index.config import load_repos
from tests.conftest import make_remote

REAL_COMPLETE, REAL_EMBED_TEXTS = llm.complete, llm.embed_texts


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    urls = {}
    for repo_id, fixture in (("owner/payments-api", "maven"), ("owner/address-ui", "codemap")):
        urls[repo_id], _ = make_remote(tmp_path, repo_id.split("/")[1], fixture)
    monkeypatch.setattr(sync, "remote_url", urls.__getitem__)


@pytest.fixture
def conn(settings):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, load_repos(settings.config_dir / "repos.yaml"))
    return conn


def test_one_sentence_keeps_only_the_first_sentence():
    assert llm.one_sentence("  Handles payments.\nAlso does refunds. ") == "Handles payments."
    assert llm.one_sentence("No full stop at all") == "No full stop at all"
    assert llm.one_sentence("v1.2 parser for addresses. More.") == "v1.2 parser for addresses."


def test_purpose_is_one_sentence_stored_only_as_search_aid_and_every_feature_is_embedded(settings, conn, remotes):
    run_update(settings)
    purpose = conn.execute("SELECT search_aid_purpose FROM repos WHERE repo_id = 'owner/address-ui'").fetchone()[0]
    assert purpose == "Serves the team's example service."
    rows = conn.execute("SELECT kind, text FROM features WHERE repo_id = 'owner/address-ui' AND kind = 'purpose'")
    assert [tuple(r) for r in rows] == [("purpose", purpose)]
    assert not conn.execute("SELECT 1 FROM facts WHERE value LIKE ?", (f"%{purpose}%",)).fetchall()
    unembedded = conn.execute("SELECT count(*) FROM features WHERE id NOT IN (SELECT rowid FROM vec_features)")
    assert unembedded.fetchone()[0] == 0


def test_second_update_and_unchanged_rebuild_make_zero_provider_calls(settings, conn, remotes, providers):
    run_update(settings)
    assert providers.calls["llm"] == 2 and providers.calls["embed"] >= 2
    providers.calls.clear()

    assert run_update(settings)["unchanged"] == ["owner/payments-api", "owner/address-ui"]
    assert providers.calls == {}

    path = settings.config_dir / "repos.yaml"
    path.write_text(path.read_text().replace("    owner: team-payments", "    owner: team-billing"))
    assert run_update(settings)["rebuilt"] == ["owner/payments-api", "owner/address-ui"]
    assert providers.calls == {}  # same README, libraries and feature text: every call is a cache hit


def test_exclude_from_llm_skips_the_llm_and_a_remote_embedder(settings, conn, remotes, providers):
    path = settings.config_dir / "repos.yaml"
    path.write_text(path.read_text().replace("  - repo: owner/address-ui", "  - repo: owner/address-ui\n"
                                                                          "    exclude_from_llm: true"))
    remote = dataclasses.replace(settings, ollama_url="https://embeddings.example.com")
    run_update(remote, "owner/address-ui")
    assert providers.calls == {}
    assert conn.execute("SELECT count(*) FROM vec_features").fetchone()[0] == 0
    assert conn.execute("SELECT search_aid_purpose FROM repos WHERE repo_id = 'owner/address-ui'").fetchone()[0] is None
    errors = [r[0] for r in conn.execute("SELECT message FROM errors")]
    assert any("embeddings skipped" in e for e in errors)

    path.write_text(path.read_text() + "\n")  # config change forces a rebuild, now with the local default embedder
    run_update(settings, "owner/address-ui")
    assert providers.calls["llm"] == 0 and providers.calls["embed"] >= 1
    assert not conn.execute("SELECT 1 FROM features WHERE kind = 'purpose'").fetchall()


def test_query_prefix_applies_to_queries_only(settings, conn, monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "embed_texts", lambda s, texts: sent.extend(texts) or [[1.0] * s.embed_dim] * len(texts))
    prefixed = dataclasses.replace(settings, embed_query_prefix="query: ")
    llm.embed(conn, prefixed, ["GET /addresses"])
    llm.embed_query(conn, prefixed, "address autocomplete")
    assert sent == ["GET /addresses", "query: address autocomplete"]


def test_wrong_embedding_dimension_fails_loudly(settings, conn, monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", lambda s, texts: [[1.0, 2.0]] * len(texts))
    with pytest.raises(llm.ProviderError, match="2 dimensions; EMBED_DIM is 8"):
        llm.embed(conn, settings, ["x"])


def test_changing_embed_model_refuses_to_start(settings, conn, capsys):
    env = settings.config_dir / "test.env"
    env.write_text(f"CONFIG_DIR={settings.config_dir}\nDATA_DIR={settings.data_dir}\nEMBED_DIM=8\n"
                   "EMBED_MODEL=another-model\n")
    assert main(["--env", str(env), "health"]) == 2
    assert "index was built with embed_model='fake-embed' but .env says 'another-model'" in capsys.readouterr().err


def test_anthropic_call_uses_the_configured_model(settings, monkeypatch):
    created = {}

    class FakeAnthropic:
        def __init__(self, api_key, timeout):
            created["api_key"] = api_key
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="A repo.")])

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)
    assert REAL_COMPLETE(settings, "system", "prompt") == "A repo."
    assert created["model"] == "fake-llm" and created["api_key"] == "test-key"
    assert created["messages"] == [{"role": "user", "content": "prompt"}] and created["system"] == "system"


def test_ollama_request_and_response_shape(settings, monkeypatch):
    seen = {}

    def post(url, json, timeout):
        seen.update(url=url, body=json)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"embeddings": [[0.5] * 8]})

    monkeypatch.setattr(llm.httpx, "post", post)
    assert REAL_EMBED_TEXTS(settings, ["hello"]) == [[0.5] * 8]
    assert seen == {"url": "http://localhost:11434/api/embed", "body": {"model": "fake-embed", "input": ["hello"]}}
