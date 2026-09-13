from __future__ import annotations
import hashlib,json,sqlite3
from pathlib import Path
from typing import Any,Protocol
class RimDocsProvider(Protocol):
    def get(self,canonical_path:str)->dict[str,Any]:...
class EmptyRimDocsProvider:
    def get(self,canonical_path):return {}
class JSONLRimDocsProvider:
    def __init__(self,path,cache_dir=".mirai_scratch"):
        self.path=Path(path);cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True);st=self.path.stat();fp=hashlib.sha256(f"{self.path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:20];self.db_path=cache_dir/f"rimdocs-{fp}.sqlite3";create=not self.db_path.exists();self.conn=sqlite3.connect(self.db_path,check_same_thread=False);self.conn.execute("PRAGMA journal_mode=WAL");self.conn.execute("CREATE TABLE IF NOT EXISTS metadata(canonical_path TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        if create:
            rows=[]
            with self.path.open("r",encoding="utf-8") as f:
                for i,line in enumerate(f,1):
                    if not line.strip():continue
                    row=json.loads(line);canonical=row.get("canonical_path");meta=row.get("metadata")
                    if not isinstance(canonical,str) or not isinstance(meta,dict):raise ValueError(f"Invalid RimDocs JSONL row {i}: expected canonical_path + metadata object")
                    rows.append((canonical,json.dumps(meta,ensure_ascii=False)))
                    if len(rows)>=1000:self.conn.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)",rows);self.conn.commit();rows=[]
                if rows:self.conn.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)",rows);self.conn.commit()
    def get(self,canonical_path):
        row=self.conn.execute("SELECT payload FROM metadata WHERE canonical_path=?",(canonical_path,)).fetchone();return json.loads(row[0]) if row else {}
    def close(self):self.conn.close()
