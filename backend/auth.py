"""OIDC resource-server auth. Never trust unverified X-User headers or shared API tokens."""
from __future__ import annotations
from dataclasses import dataclass
from fastapi import HTTPException,Request
from . import config

@dataclass(frozen=True)
class Identity:
    subject:str
    tenant:str
    scopes:frozenset[str]

_client=None

def identity(request:Request)->Identity:
    global _client
    if config.MODE=='demo':return Identity(subject='demo',tenant='demo',scopes=frozenset({'rimdocs:read'}))
    if not all([config.OIDC_ISSUER,config.OIDC_AUDIENCE,config.OIDC_JWKS_URL]):
        raise HTTPException(status_code=503,detail='Corporate identity provider not configured')
    header=request.headers.get('Authorization','')
    if not header.startswith('Bearer '):raise HTTPException(401,'Authentication required')
    import jwt
    try:
        if _client is None:_client=jwt.PyJWKClient(config.OIDC_JWKS_URL,cache_jwk_set=True,lifespan=300)
        token=header[7:]
        key=_client.get_signing_key_from_jwt(token)
        claims=jwt.decode(token,key.key,algorithms=['RS256'],issuer=config.OIDC_ISSUER,
                          audience=config.OIDC_AUDIENCE,options={'require':['exp','iss','aud','sub']})
        subject=str(claims['sub'])
        tenant=str(claims.get('tid') or claims.get('tenant_id') or '')
        scopes=frozenset(str(claims.get('scp') or claims.get('scope') or '').split())
        roles=claims.get('roles',[])
        if any(x in ('rimdocs.read_all','rimdocs:read_all') for x in roles if isinstance(x,str)):
            scopes=scopes|{'rimdocs:read_all'}
        if not tenant or 'rimdocs:read' not in scopes:raise HTTPException(403,'Document access not authorized')
        return Identity(subject,tenant,scopes)
    except HTTPException:raise
    except Exception as exc:raise HTTPException(401,'Invalid or expired corporate session') from exc
