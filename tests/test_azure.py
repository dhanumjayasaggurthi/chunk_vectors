from unittest.mock import patch
from backend.azure_llm import JnJAzureLLM

class MockResponse:
    def raise_for_status(self):pass
    def json(self):return {"choices":[{"message":{"content":"{}"}}],"usage":{}}

CONF={"api_base":"https://example.azure.test","api_key":"fake","api_version":"2025-01-01","deployment":"jnj-deploy"}

def test_gpt5_payload_does_not_send_unsupported_temperature():
    with patch("backend.azure_llm.deployment_config",return_value=CONF),patch("backend.azure_llm.requests.post",return_value=MockResponse()) as post:
        JnJAzureLLM("gpt5").complete("extract","report")
        payload=post.call_args.kwargs["json"]
        assert payload["max_completion_tokens"]==2200
        assert "temperature" not in payload and "max_tokens" not in payload


def test_gpt4o_payload_uses_chat_max_tokens():
    with patch("backend.azure_llm.deployment_config",return_value=CONF),patch("backend.azure_llm.requests.post",return_value=MockResponse()) as post:
        JnJAzureLLM("gpt4o").complete("extract","report")
        payload=post.call_args.kwargs["json"]
        assert payload["max_tokens"]==2200 and payload["temperature"]==0
        assert "max_completion_tokens" not in payload
