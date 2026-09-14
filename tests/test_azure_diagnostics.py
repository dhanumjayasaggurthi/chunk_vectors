import requests, pytest
from mir_ai.azure_gateway import AzureGateway,AzureRequestError
from mir_ai.diagnostics import classify_exception

class FakeSession:
    def __init__(self,responses):self.responses=list(responses);self.calls=0
    def post(self,*a,**k):
        self.calls+=1;v=self.responses.pop(0)
        if isinstance(v,Exception):raise v
        return v

def response(status,body='{}',headers=None):
    r=requests.Response();r.status_code=status;r._content=body.encode();r.headers.update(headers or {});r.url='https://example.invalid/test';r.request=requests.Request('POST',r.url).prepare();return r

def test_permanent_400_preserves_gateway_code_message_request_id_without_retry():
    fake=FakeSession([response(400,'{"error":{"code":"BadRequest","message":"deployment rejected request"}}',{'apim-request-id':'req-123'})])
    g=AzureGateway(max_retries=5);g._session=lambda:fake
    with pytest.raises(AzureRequestError) as captured:
        g._post('https://example.invalid','secret-key',json_body={'input':['probe']},service='azure_embedding',operation='embeddings')
    e=captured.value;d=classify_exception(e)
    assert e.status_code==400 and e.retryable is False and e.request_id=='req-123' and fake.calls==1
    assert d.error_code=='BadRequest' and 'deployment rejected request' in d.observed_error
    assert d.root_cause_status=='dependency_reported' and 'secret-key' not in str(e)
