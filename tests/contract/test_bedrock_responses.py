import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from tg_discovery.llm.bedrock_responses import BedrockResponsesClient, FunctionCaller, LlmError, TokenPrice
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError

BASE_URL = "https://bedrock.test/v1"
SCHEMA: dict[str, object] = {"type": "object", "properties": {"venues": {"type": "array"}}}
PRICE = TokenPrice(input_usd_per_1m=0.15, output_usd_per_1m=0.60)


def _response(
    arguments: str = '{"venues": []}', *, status: str = "completed", usage: dict[str, int] | None = None
) -> dict[str, object]:
    return {
        "id": "resp_1",
        "status": status,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "emit_candidates",
                "arguments": arguments,
            },
        ],
        "usage": usage if usage is not None else {"input_tokens": 2000, "output_tokens": 500},
    }


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _client(
    http: httpx.AsyncClient, budget: Budget, *, reasoning_effort: str | None = "low", forced_tool_choice: bool = False
) -> BedrockResponsesClient:
    return BedrockResponsesClient(
        base_url=BASE_URL,
        api_token="token-123",
        model="openai.gpt-oss-120b",
        price=PRICE,
        client=http,
        guard=budget,
        reasoning_effort=reasoning_effort,
        forced_tool_choice=forced_tool_choice,
        retry_wait_s=0.0,
    )


async def _call(client: BedrockResponsesClient) -> object:
    return await client.call_function(
        system="be strict", prompt="extract", tool_name="emit_candidates", parameters=SCHEMA, max_output_tokens=4096
    )


@respx.mock
async def test_the_request_declares_a_flat_function_and_tells_the_model_to_call_it(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response()))

    await _call(_client(http, Budget(1.0)))

    request = route.calls.last.request
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer token-123"
    assert body["model"] == "openai.gpt-oss-120b"
    assert body["instructions"].startswith("be strict\n\n")
    assert "calling the function emit_candidates exactly once" in body["instructions"]
    assert body["input"] == "extract"
    assert body["max_output_tokens"] == 4096
    assert body["tools"] == [{"type": "function", "name": "emit_candidates", "parameters": SCHEMA}]
    assert body["tool_choice"] == "auto"  # gpt-oss on Bedrock rejects a named tool
    assert body["reasoning"] == {"effort": "low"}


@respx.mock
async def test_a_model_that_accepts_it_can_be_made_to_name_the_tool(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response()))

    await _call(_client(http, Budget(1.0), forced_tool_choice=True))

    assert json.loads(route.calls.last.request.content)["tool_choice"] == {
        "type": "function",
        "name": "emit_candidates",
    }


@respx.mock
async def test_the_reasoning_parameter_can_be_left_out(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response()))

    await _call(_client(http, Budget(1.0), reasoning_effort=None))

    assert "reasoning" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_the_function_call_is_found_after_reasoning_items(http: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/responses").mock(
        return_value=httpx.Response(200, json=_response('{"venues": [{"name": "Heaven"}]}'))
    )

    result = await _client(http, Budget(1.0)).call_function(
        system="s", prompt="p", tool_name="emit_candidates", parameters=SCHEMA, max_output_tokens=100
    )

    assert result.arguments == {"venues": [{"name": "Heaven"}]}
    assert result.model == "openai.gpt-oss-120b"


@respx.mock
async def test_cost_is_computed_from_the_reported_usage(http: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response()))
    budget = Budget(1.0)

    result = await _client(http, budget).call_function(
        system="s", prompt="p", tool_name="emit_candidates", parameters=SCHEMA, max_output_tokens=100
    )

    assert (result.usage.input_tokens, result.usage.output_tokens) == (2000, 500)
    assert result.call.units == 2500
    assert result.call.cost_usd == pytest.approx((2000 * 0.15 + 500 * 0.60) / 1_000_000)
    assert result.call.provider == "llm:openai.gpt-oss-120b"
    assert budget.spent_usd == pytest.approx(result.call.cost_usd)


@respx.mock
async def test_an_incomplete_response_is_an_error_but_still_billed(http: httpx.AsyncClient) -> None:
    body = _response('{"venues": [', status="incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=body))
    budget = Budget(1.0)

    with pytest.raises(LlmError, match="max_output_tokens") as caught:
        await _call(_client(http, budget))

    assert caught.value.usage.output_tokens == 500
    assert caught.value.call.cost_usd > 0
    assert budget.spent_usd == pytest.approx(caught.value.call.cost_usd)


@respx.mock
async def test_a_response_that_failed_inside_a_200_is_an_error_that_carries_the_reason(
    http: httpx.AsyncClient,
) -> None:
    body: dict[str, object] = {
        "status": "failed",
        "error": {"code": "invalid_prompt", "message": "Only 'auto' tool_choice is supported"},
        "output": [],
    }
    respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmError, match="Only 'auto' tool_choice is supported") as caught:
        await _call(_client(http, Budget(1.0)))

    assert caught.value.call.status == "error"
    assert "Only 'auto'" in (caught.value.call.error or "")


@respx.mock
async def test_arguments_that_are_not_json_are_an_error(http: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response("not json")))

    with pytest.raises(LlmError, match="not valid JSON"):
        await _call(_client(http, Budget(1.0)))


@respx.mock
async def test_a_response_without_the_function_call_is_an_error(http: httpx.AsyncClient) -> None:
    body: dict[str, object] = {"status": "completed", "output": [{"type": "message", "content": []}], "usage": {}}
    respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmError, match="holds no call to emit_candidates"):
        await _call(_client(http, Budget(1.0)))


@respx.mock
async def test_rate_limiting_is_retried_four_times_in_total(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(429, text="slow down"))

    with pytest.raises(ProviderError) as caught:
        await _call(_client(http, Budget(1.0)))

    assert route.call_count == 4
    assert caught.value.call.attempts == 4


@respx.mock
async def test_authentication_errors_are_not_retried(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(401, text="bad token"))

    with pytest.raises(ProviderError):
        await _call(_client(http, Budget(1.0)))

    assert route.call_count == 1


@respx.mock
async def test_a_prompt_that_cannot_fit_the_budget_is_never_sent(http: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/responses").mock(return_value=httpx.Response(200, json=_response()))
    client = _client(http, Budget(0.0001))

    with pytest.raises(BudgetExceededError):
        await client.call_function(
            system="s", prompt="x" * 100_000, tool_name="emit_candidates", parameters=SCHEMA, max_output_tokens=8192
        )

    assert route.call_count == 0


def test_the_client_satisfies_the_function_caller_protocol(http: httpx.AsyncClient) -> None:
    _: FunctionCaller = _client(http, Budget(1.0))
