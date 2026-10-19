import time
from fastapi.testclient import TestClient
from backend.app import app


def test_demo_app_job_and_export():
    with TestClient(app) as c:
        health=c.get('/api/v1/health').json()
        assert c.get('/api/v1/health/ready').status_code==200
        assert health['mode']=='demo' and health['live_connectors'] is False
        r=c.post('/api/v1/jobs',json={'ids':['0901bacc84b20d46','0901bacc84b20d46','UNKNOWN-REF-XYZ'],'label':'Proof run'})
        assert r.status_code==202
        j=r.json();assert j['counts']['total']==2
        job=j['job_id']
        for _ in range(40):
            info=c.get(f'/api/v1/jobs/{job}').json()
            if info['progress_percent']==100:break
            time.sleep(.1)
        assert info['progress_percent']==100
        assert c.get(f'/api/v1/jobs/{job}/results').json()['total']==2
        assert c.get(f'/api/v1/jobs/{job}/export?format=csv').status_code==200
        assert c.get(f'/api/v1/jobs/{job}/export?format=json').status_code==200
        assert c.get('/').status_code==200


def test_bad_id_rejected():
    with TestClient(app) as c:
        assert c.post('/api/v1/jobs',json={'ids':['s3://unauthorized']}).status_code==422


def test_bulk_csv_upload_deduplicates():
    with TestClient(app) as c:
        r=c.post('/api/v1/jobs/upload',files={'file':('docs.csv',b'r_object_id,notes\nAAA,first\nBBB,second\nAAA,duplicate\nBAD ID,bad\n','text/csv')},data={'label':'Bulk'} )
        assert r.status_code==202,r.text
        assert r.json()['counts']['total']==2
        assert r.json()['invalid_rows_skipped']==1


def test_chunked_csv_upload_1200_ids_without_duplicates():
    lines='r_object_id\n'+'\n'.join(f'DOC-{i:06d}' for i in range(1200))+'\nDOC-000010\n'
    with TestClient(app) as c:
        r=c.post('/api/v1/jobs/upload',files={'file':('large.csv',lines.encode(),'text/csv')},data={'label':'Chunked CSV'})
        assert r.status_code==202,r.text
        assert r.json()['counts']['total']==1200
        assert r.json()['invalid_rows_skipped']==0
        assert c.get('/api/v1/jobs/'+r.json()['job_id']).json()['counts']['total']==1200
