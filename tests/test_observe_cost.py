"""Cost metering: instrument_client must feed llm_cost_usd for every call shape."""
from types import SimpleNamespace

import observe


def _fresh_metrics():
    observe._reset_metrics()
    return observe.get_metrics


class _StubClient:
    """Mimics the OpenAI client surface instrument_client wraps."""

    def __init__(self, chat_response, responses_response):
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: chat_response))
        self.responses = SimpleNamespace(create=lambda **kw: responses_response)


def _chat_resp(model="gpt-4o", prompt=1000, completion=500):
    return SimpleNamespace(model=model,
                           usage=SimpleNamespace(prompt_tokens=prompt,
                                                 completion_tokens=completion))


def _responses_resp(model="gpt-4o", inp=2000, out=100):
    return SimpleNamespace(model=model,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def test_chat_completion_call_is_metered():
    _fresh_metrics()
    client = observe.instrument_client(_StubClient(_chat_resp(), _responses_resp()))
    client.chat.completions.create(model="gpt-4o", messages=[])
    # 1000 in @ $2.50/M + 500 out @ $10/M
    assert abs(observe.get_metrics()["llm_cost_usd"] - (0.0025 + 0.005)) < 1e-9


def test_responses_call_with_web_search_adds_flat_fee():
    _fresh_metrics()
    client = observe.instrument_client(_StubClient(_chat_resp(), _responses_resp()))
    client.responses.create(model="gpt-4o", tools=[{"type": "web_search_preview"}], input="q")
    expected = 2000 / 1e6 * 2.50 + 100 / 1e6 * 10.00 + observe.COST_PER_CALL["openai_responses_web_search"]
    assert abs(observe.get_metrics()["llm_cost_usd"] - expected) < 1e-9


def test_mini_model_uses_mini_rates():
    _fresh_metrics()
    client = observe.instrument_client(
        _StubClient(_chat_resp(model="gpt-4o-mini-2024-07-18"), _responses_resp()))
    client.chat.completions.create(model="gpt-4o-mini-2024-07-18", messages=[])
    assert abs(observe.get_metrics()["llm_cost_usd"] - (1000 / 1e6 * 0.150 + 500 / 1e6 * 0.600)) < 1e-9


def test_unknown_model_and_missing_usage_never_raise():
    _fresh_metrics()
    no_usage = SimpleNamespace(model="gpt-4o")  # no .usage at all
    client = observe.instrument_client(_StubClient(no_usage, _responses_resp(model="future-model")))
    client.chat.completions.create(model="gpt-4o", messages=[])
    client.responses.create(model="future-model", input="q")
    assert observe.get_metrics()["llm_cost_usd"] == 0.0


def test_cost_threshold_warns_once_midrun(capsys):
    _fresh_metrics()
    threshold = observe.ALERT_THRESHOLDS["max_cost_usd"]
    per_call = observe.add_usage("gpt-4o", 1_000_000, 0)  # $2.50 per call
    calls = 1
    while calls * per_call <= threshold + per_call:
        observe.add_usage("gpt-4o", 1_000_000, 0)
        calls += 1
    out = capsys.readouterr().out
    assert out.count("COST_EXCEEDED") == 1  # fires exactly once, mid-run
