"""One worker image, two roles: --role dispatcher and --role executor.

Dispatcher: resolves bounded metadata batches, reconciles leases and publishes SQS task IDs.
Executor: long-polls SQS, claims fenced DB stage tasks, checkpoints and acknowledges.
"""
from __future__ import annotations
import argparse, json, logging, os, signal, threading, time
from botocore.exceptions import ClientError
from . import config,store
from .enterprise_state import StageTask,claim,heartbeat,done,defer,error,due_to_publish,unstick_expired,queue_prepared_documents
from .enterprise_pipeline import prepare,ocr_start,ocr_poll,finalize
from .integrations import PermanentError
log=logging.getLogger('evidenceflow.enterprise')
_stop=threading.Event()

def client():
    import boto3
    if not config.SQS_TASK_URL:raise RuntimeError('SQS_TASK_URL required in enterprise mode')
    return boto3.client('sqs')

def dispatch_once(sqs=None):
    from .worker import resolve
    sqs=sqs or client()
    unstick_expired(200)
    resolved=resolve(200)
    queued=queue_prepared_documents(200)
    published=0
    ids=due_to_publish(100)
    if ids:
        from sqlalchemy import select
        with store.Session() as s:
            rows=s.execute(select(StageTask.id,store.Job.total).join(store.Job,StageTask.job_id==store.Job.id)
                           .where(StageTask.id.in_(ids))).all()
            routes={task_id:(config.SQS_INTERACTIVE_URL if config.SQS_INTERACTIVE_URL and total<=5000 else config.SQS_TASK_URL)
                    for task_id,total in rows}
        for url in set(routes.values()):
            routed=[x for x in ids if routes.get(x)==url]
            for start in range(0,len(routed),10):
                group=routed[start:start+10]
                try:
                    response=sqs.send_message_batch(QueueUrl=url,Entries=[{'Id':str(n),'MessageBody':json.dumps({'task_id':tid})}
                                            for n,tid in enumerate(group)])
                    published+=len(response.get('Successful',[]))
                    if response.get('Failed'):log.error('SQS publish partial failure: %s',len(response['Failed']))
                except Exception:
                    log.exception('SQS batch publish failed; outbox will retry')
    return resolved,queued,published

def run_stage(task):
    stage=task['stage']
    if stage=='INGEST':
        from .bulk import ingest_upload
        ingest_upload(task)
        return 'DONE'
    if stage=='EXPORT':
        from .bulk import generate_export
        generate_export(task)
        return 'DONE'
    if stage=='PREPARE':
        # Move document into PROCESSING once, accounting for aggregated job counters.
        with store.Session.begin() as s:
            doc=s.get(store.Document,(task['job_id'],task['document_id']))
            if not doc or doc.status in ('FAILED','COMPLETED','REVIEW_REQUIRED','CANCELLED'):return 'DONE'
            if doc.status=='QUEUED':store._transition(s,doc,'PROCESSING')
        prepare(task)
        return 'DONE'
    if stage=='OCR_START':
        ocr_start(task)
        return 'OCR_POLL'
    if stage=='OCR_POLL':
        return 'DONE' if ocr_poll(task) else 'OCR_POLL'
    if stage=='FINALIZE':
        finalize(task)
        return 'DONE'
    raise PermanentError('Unsupported processing stage')

def execute_task(task_id):
    task=claim(task_id)
    if not task:return False
    stop=threading.Event()
    def alive():
        while not stop.wait(max(3,min(30,config.TASK_LEASE_SECONDS//3))):
            try:
                if not heartbeat(task):return
            except Exception:log.exception('Task heartbeat failed')
    th=threading.Thread(target=alive,daemon=True);th.start()
    try:
        outcome=run_stage(task)
        if outcome=='DONE':done(task)
        elif outcome=='OCR_POLL':defer(task,seconds=45,next_stage='OCR_POLL')
        else:raise RuntimeError('Invalid stage outcome')
    except PermanentError as exc:
        log.warning('Permanent task failure: task=%s type=%s',task_id,type(exc).__name__)
        error(task,type(exc).__name__,retryable=False)
    except Exception as exc:
        log.exception('Retryable task failure: task=%s',task_id)
        error(task,type(exc).__name__,retryable=True)
    finally:
        stop.set();th.join(timeout=1)
    return True

def consume_once(sqs=None):
    sqs=sqs or client()
    urls=[config.SQS_INTERACTIVE_URL,config.SQS_TASK_URL] if config.SQS_INTERACTIVE_URL else [config.SQS_TASK_URL]
    messages=[];current_url=urls[-1]
    for url in urls:
        # Interactive queue first; short-poll it to avoid starving background work.
        wait=1 if url==config.SQS_INTERACTIVE_URL else 20
        received=sqs.receive_message(QueueUrl=url,MaxNumberOfMessages=1,WaitTimeSeconds=wait,
                                     VisibilityTimeout=max(60,config.TASK_LEASE_SECONDS))
        if received.get('Messages'):
            messages=received['Messages'];current_url=url;break
    for msg in messages:
        try:
            task_id=json.loads(msg['Body'])['task_id']
            if not isinstance(task_id,str):raise ValueError('Invalid task ID')
            execute_task(task_id)
            # ACK only after DB transition; retry/deferral lives in durable outbox.
            sqs.delete_message(QueueUrl=current_url,ReceiptHandle=msg['ReceiptHandle'])
        except Exception:
            log.exception('Message handling failed: SQS will redeliver')
    return len(messages)

def main():
    p=argparse.ArgumentParser();p.add_argument('--role',choices=('dispatcher','executor'),required=True)
    args=p.parse_args()
    if config.MODE!='live' or not config.DATABASE_URL.startswith('postgresql'):
        raise RuntimeError('Enterprise workers require live mode and PostgreSQL')
    if not (config.SQS_TASK_URL and config.CHECKPOINT_BUCKET):raise RuntimeError('Missing SQS_TASK_URL or CHECKPOINT_BUCKET')
    # In enterprise production, DB schema is migrated by a separate deployment job.
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM,lambda *_:_stop.set())
    signal.signal(signal.SIGINT,lambda *_:_stop.set())
    sqs=client()
    while not _stop.is_set():
        try:
            if args.role=='dispatcher':
                summary=dispatch_once(sqs);log.info('dispatch cycle resolved=%d queued=%d published=%d',*summary)
                _stop.wait(0.2 if any(summary) else 4)
            else:consume_once(sqs)
        except Exception:
            log.exception('Worker loop error; retaining durable work')
            _stop.wait(5)
if __name__=='__main__':main()
