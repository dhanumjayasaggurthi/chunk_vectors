import unittest
from unittest.mock import patch

import requests

from mir_ai.azure_gateway import AzureGateway


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(status, body="{}", headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = body.encode("utf-8")
    value.headers.update(headers or {})
    value.url = "https://example.invalid/test"
    value.request = requests.Request("POST", value.url).prepare()
    return value


class TestAzureGatewayRetry(unittest.TestCase):
    def gateway_with(self, fake, retries=3):
        gateway = AzureGateway(max_retries=retries)
        gateway._session = lambda: fake
        return gateway

    @patch("mir_ai.azure_gateway.random.random", return_value=0.0)
    @patch("mir_ai.azure_gateway.time.sleep", return_value=None)
    def test_permanent_400_is_not_retried(self, _sleep, _random):
        fake = FakeSession([response(400)])
        gateway = self.gateway_with(fake, retries=5)
        with self.assertRaises(requests.HTTPError):
            gateway._post("https://example.invalid", "key", json_body={})
        self.assertEqual(fake.calls, 1)

    @patch("mir_ai.azure_gateway.random.random", return_value=0.0)
    @patch("mir_ai.azure_gateway.time.sleep", return_value=None)
    def test_500_is_retried_and_can_recover(self, _sleep, _random):
        fake = FakeSession([response(500), response(200)])
        gateway = self.gateway_with(fake, retries=3)
        result = gateway._post("https://example.invalid", "key", json_body={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(fake.calls, 2)

    @patch("mir_ai.azure_gateway.random.random", return_value=0.0)
    @patch("mir_ai.azure_gateway.time.sleep", return_value=None)
    def test_transport_error_is_retried(self, _sleep, _random):
        fake = FakeSession([requests.Timeout("timeout"), response(200)])
        gateway = self.gateway_with(fake, retries=3)
        result = gateway._post("https://example.invalid", "key", json_body={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(fake.calls, 2)

    def test_visual_mime_detection_does_not_mislabel_jpeg(self):
        self.assertEqual(AzureGateway._image_mime(b"\xff\xd8\xff\xe0payload"), "image/jpeg")
        self.assertEqual(
            AzureGateway._image_mime(b"\x89PNG\r\n\x1a\npayload"), "image/png"
        )
        self.assertEqual(
            AzureGateway._image_mime(b"RIFF\x00\x00\x00\x00WEBPpayload"),
            "image/webp",
        )
        with self.assertRaises(ValueError):
            AzureGateway._image_mime(b"unknown")


if __name__ == "__main__":
    unittest.main()
