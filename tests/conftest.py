"""Isolated disposable SQLite database for test runs; never share demo state."""
import os
import tempfile
from uuid import uuid4
os.environ.setdefault('APP_MODE','demo')
os.environ['DATABASE_URL']=f'sqlite:///{tempfile.gettempdir()}/evidenceflow-test-{uuid4().hex}.db'
