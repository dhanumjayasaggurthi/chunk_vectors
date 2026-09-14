from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .azure_gateway import AzureGateway
from .diagnostics import classify_exception
from .logging_utils import get_logger
from .vision import VisionOCR

log=get_logger("mir_ai.preflight")

@dataclass
class PreflightCheck:
    name:str
    ok:bool
    detail:str=""
    error_category:str=""
    error_code:str=""
    status_code:int|None=None
    request_id:str=""
    resolution_hint:str=""
    def to_dict(self):return asdict(self)


def run_preflight(settings,store,*,source_mode:str,source_value=None,s3_source=None):
    checks=[]
    def check(name,fn,service="",operation=""):
        try:
            fn(); item=PreflightCheck(name,True,"ok"); log.info("Preflight check passed",extra={"service":service,"operation":operation or name})
        except Exception as exc:
            d=classify_exception(exc,service=service,operation=operation or name)
            item=PreflightCheck(name,False,d.observed_error,d.error_category,d.error_code,d.status_code,d.request_id,d.resolution_hint)
            log.error("Preflight check failed",extra={"service":d.service or service,"operation":d.operation or operation or name,
                      "error_class":d.error_class,"error_category":d.error_category,"error_code":d.error_code,
                      "status_code":d.status_code,"request_id":d.request_id,"error_detail":d.observed_error,
                      "resolution_hint":d.resolution_hint,"retryable":d.retryable})
        checks.append(item)

    check("postgres",store.ping,"postgres","ping")
    check("database_schema",store.verify_schema,"postgres","verify_schema")
    if source_mode=="nas":
        root=Path(source_value or settings.docs_root)
        def nas_check():
            if not root.exists():raise FileNotFoundError(str(root))
            if not root.is_dir():raise ValueError(f"NAS root is not a directory: {root}")
            # Access check without walking the full tree.
            next(root.iterdir(),None)
        check("nas_source",nas_check,"nas","source_access")
    elif source_mode=="s3" and s3_source is not None:
        check("s3_source",s3_source.preflight,"s3","list_objects_v2")
    elif source_mode=="file":
        p=Path(source_value)
        check("file_source",lambda: p.exists() or (_ for _ in ()).throw(FileNotFoundError(str(p))),"file","exists")

    gateway=AzureGateway(settings.config_path,max_retries=settings.api_max_retries,timeout_s=settings.api_timeout_s,
                         retry_base_s=settings.api_retry_base_seconds,embedding_model=settings.embedding_model,
                         embedding_dim=settings.embedding_dim,embedding_api_version=settings.embedding_api_version)
    if settings.enable_embeddings and settings.preflight_embedding:
        check("azure_embedding",gateway.preflight_embeddings,"azure_embedding","preflight")
    if settings.preflight_chat:
        check("azure_chat",gateway.preflight_chat,"azure_chat","preflight")
    if settings.preflight_vision:
        vision=VisionOCR(settings.config_path,max_retries=settings.api_max_retries,timeout_s=settings.api_timeout_s,
                         retry_base_s=settings.api_retry_base_seconds)
        check("google_vision_client",vision.preflight,"google_vision","client_initialization")
    return checks
