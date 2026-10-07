import json

import pytest

from desktop_tracker.categorize import ollama_classifier
from desktop_tracker.categorize.ollama_classifier import OllamaClassifier, assert_local_host

SIX = ["Work", "Learning", "Communication", "Social", "Entertainment", "Other"]


class FakeClient:
    def __init__(self, content=None, raises=None):
        self.content, self.raises, self.calls = content, raises, []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {"message": {"content": self.content}}


def classify(content, name="Foo", kind="app"):
    return OllamaClassifier(client=FakeClient(content)).classify(name, kind)


def test_valid_json_answer():
    assert classify(json.dumps({"category": "Learning"})) == "Learning"


def test_answer_is_matched_case_insensitively_and_trimmed():
    assert classify(json.dumps({"category": "  social "})) == "Social"
    assert classify("entertainment") == "Entertainment"


def test_invalid_answers_return_none_so_the_caller_uses_other():
    for bad in ("", "I think it is Work.", json.dumps({"category": "Gaming"}),
                json.dumps({"x": 1}), "[]", json.dumps({"category": None})):
        assert classify(bad) is None, bad


def test_only_the_name_is_sent_never_a_title():
    client = FakeClient(json.dumps({"category": "Work"}))
    OllamaClassifier(client=client).classify("www.example.com", "site")
    sent = json.dumps(client.calls[0]["messages"])
    assert "www.example.com" in sent
    assert len(client.calls[0]["messages"]) == 2  # system + the one name


def test_request_turns_thinking_off_and_constrains_the_answer():
    client = FakeClient(json.dumps({"category": "Work"}))
    OllamaClassifier(client=client).classify("Foo", "app")
    call = client.calls[0]
    assert call["think"] is False
    assert call["format"]["properties"]["category"]["enum"] == SIX


def test_client_failure_propagates_so_the_caller_can_skip_and_not_cache():
    with pytest.raises(ConnectionError):
        OllamaClassifier(client=FakeClient(raises=ConnectionError("off"))).classify("Foo", "app")


def test_first_call_gets_about_thirty_seconds(monkeypatch):
    seen = {}

    class Capture:
        def __init__(self, host=None, timeout=None, **kw):
            seen.update(host=host, timeout=timeout)

    monkeypatch.setattr(ollama_classifier.ollama, "Client", Capture)
    OllamaClassifier()._get_client()
    assert seen["timeout"] == 30


@pytest.mark.parametrize("host", [
    "http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434", "localhost:11434",
])
def test_local_hosts_are_allowed(host):
    assert_local_host(host)


@pytest.mark.parametrize("host", [
    "http://example.com:11434", "http://192.168.1.5:11434", "http://localhost.evil.com",
    "http://localhost@evil.com", "http://evil.com/localhost", "", "https://api.openai.com",
])
def test_remote_hosts_are_refused(host):
    with pytest.raises(ValueError):
        assert_local_host(host)


def test_constructor_refuses_a_remote_host():
    with pytest.raises(ValueError):
        OllamaClassifier(host="http://evil.example")


def test_ollama_host_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://evil.example:11434")
    seen = {}

    class Capture:
        def __init__(self, host=None, timeout=None, **kw):
            seen["host"] = host

    monkeypatch.setattr(ollama_classifier.ollama, "Client", Capture)
    OllamaClassifier()._get_client()
    assert "evil" not in seen["host"] and "localhost" in seen["host"]
