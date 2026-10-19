"""Only two external data adapters: Snowflake metadata and S3 PDF bytes."""
from __future__ import annotations
import configparser
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from . import config

class PermanentError(Exception):
    """Input is invalid, forbidden, or not recoverable by an automatic retry."""

class DemoResolver:
    def lookup(self, ids: list[str]) -> dict[str, dict | None]:
        sample = {"0901bacc84b20d46","0901bacc8283f1de","0901bacc802bcfa8","0901bacc802bcfaa"}
        return {i: (dict(artifact_name="Periodic Benefit Risk Evaluation Report (PBRER)",
                         a_status="Approved",ingestion_status="SUCCESS",file_path="demo://"+i) if i in sample else None) for i in ids}

class SnowflakeResolver:
    def __init__(self):
        self.conn = None
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}",config.SF_TABLE):
            raise ValueError("SF_METADATA_TABLE must be a fully qualified static table name")

    def _connect(self):
        import snowflake.connector
        p=configparser.ConfigParser()
        p.read(os.getenv("JNJ_CONFIG_PATH","config.ini"))
        cfg=p["SNOWFLAKE"] if "SNOWFLAKE" in p else {}
        opts={k:os.getenv("SNOWFLAKE_"+k.upper(),cfg.get(k,"")) for k in ("user","account","role","warehouse","database","schema")}
        opts={k:v for k,v in opts.items() if v}
        key_path=os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH",p.get("PATHS","private_key_path",fallback=""))
        pem=os.getenv('SNOWFLAKE_PRIVATE_KEY_PEM','').replace('\\n','\n').encode()
        if key_path or pem:
            from cryptography.hazmat.primitives import serialization
            password=os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSWORD",cfg.get("private_key_password",""))
            if not pem:
                with open(key_path,"rb") as f:pem=f.read()
            key=serialization.load_pem_private_key(pem,password=password.encode() if password else None)
            opts["private_key"]=key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        else:
            raise PermanentError("Configure Snowflake private-key authentication before starting live workers")
        return snowflake.connector.connect(**opts)

    def lookup(self, ids: list[str]) -> dict[str, dict | None]:
        if not ids:return {}
        assert len(ids)<=500, "Worker batches must remain bounded"
        sql=(f"SELECT R_OBJECT_ID, ARTIFACT_NAME, FILE_PATH, A_STATUS, STATUS "
             f"FROM {config.SF_TABLE} WHERE R_OBJECT_ID IN ({','.join(['%s']*len(ids))})")
        if self.conn is None or self.conn.is_closed():
            self.conn=self._connect()
        try:
            cur=self.conn.cursor()
            try:
                cur.execute(sql,tuple(ids))
                rows=cur.fetchall()
            finally:cur.close()
        except Exception:
            try:self.conn.close()
            except Exception:pass
            self.conn=None
            raise
        grouped:dict[str,list[dict]]={i:[] for i in ids}
        for rid,artifact,path,approval,ingestion in rows:
            if str(rid) in grouped:
                record=dict(artifact_name=artifact,file_path=path,a_status=approval,ingestion_status=ingestion)
                if record not in grouped[str(rid)]:grouped[str(rid)].append(record)
        result={}
        for rid,matches in grouped.items():
            result[rid] = ({"ambiguous":True} if len(matches)>1 else matches[0]) if matches else None
        return result

class S3Reader:
    def __init__(self):
        import boto3
        self.client=boto3.client("s3")  # Credentials from role / default chain; no embedded secrets

    def read(self,uri:str):
        if not uri.startswith("s3://"):raise PermanentError("Document has no valid S3 URI")
        parsed=urlsplit(uri)
        bucket=parsed.netloc;key=parsed.path.lstrip("/")
        if not bucket or not key:raise PermanentError("Invalid S3 bucket or key")
        if not config.ALLOWED_S3_BUCKETS or bucket not in config.ALLOWED_S3_BUCKETS:
            raise PermanentError("S3 bucket is not included in the configured allowlist")
        if config.ALLOWED_S3_PREFIXES and not key.startswith(config.ALLOWED_S3_PREFIXES):
            raise PermanentError("S3 object path is outside permitted prefixes")
        head=self.client.head_object(Bucket=bucket,Key=key)
        if head["ContentLength"]>config.MAX_FILE_MB*1024*1024:
            raise PermanentError("PDF exceeds configured maximum document size")
        version=head.get("VersionId")
        args=dict(Bucket=bucket,Key=key)
        if version:args["VersionId"]=version
        else:args["IfMatch"]=head["ETag"]  # Prevent silent overwrite between HEAD and GET
        obj=self.client.get_object(**args)
        content=obj["Body"].read(config.MAX_FILE_MB*1024*1024+1)
        obj["Body"].close()
        if len(content)>config.MAX_FILE_MB*1024*1024:raise PermanentError("PDF exceeds configured maximum document size")
        if not content.startswith(b"%PDF-"):raise PermanentError("S3 object is not a PDF")
        return content, (version or head.get("ETag",""))


class SnowflakeEntitlements:
    """Pluggable security adapter. Required unless user has signed read-all scope.
    Provision ACL table columns (TENANT_ID, PRINCIPAL_ID, R_OBJECT_ID) and index/cluster
    appropriately. Authorization is evaluated by background service on behalf of submitter.
    """
    def __init__(self):
        self.resolver=SnowflakeResolver()
        if not config.SF_ENTITLEMENTS_TABLE:
            raise PermanentError('Entitlement table not configured; deny by default')
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}',config.SF_ENTITLEMENTS_TABLE):
            raise PermanentError('Invalid entitlement table name')

    def allowed(self,ids,owner,tenant):
        if not ids:return set()
        if self.resolver.conn is None or self.resolver.conn.is_closed():
            self.resolver.conn=self.resolver._connect()
        conn=self.resolver.conn
        try:
            sql=(f'SELECT DISTINCT R_OBJECT_ID FROM {config.SF_ENTITLEMENTS_TABLE} '
                 f'WHERE PRINCIPAL_ID=%s AND TENANT_ID=%s AND R_OBJECT_ID IN ({",".join(["%s"]*len(ids))})')
            cur=conn.cursor()
            try:
                cur.execute(sql,tuple([owner,tenant]+ids))
                return {str(x[0]) for x in cur.fetchall()}
            finally:cur.close()
        except Exception:
            try:conn.close()
            except Exception:pass
            self.resolver.conn=None
            raise
