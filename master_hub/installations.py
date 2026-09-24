"""Authenticated installation diagnostics. Distribution is blocked pending real artifacts."""
from collections import OrderedDict
from functools import lru_cache
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .installation_store import InstallationStore


RELEASE_BLOCKERS = [
    'A native Windows downloader has not been built and verified.',
    'A verified per-user agent package and signed release manifest are not available.',
    'An authenticated agent enrollment/online handshake has not been implemented.'
]


class Report(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    attempt_id: str = Field(min_length=1, max_length=64)
    report_token: str = Field(min_length=1, max_length=256)
    seq: StrictInt = Field(ge=1, le=1_000_000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    stage: str = Field(min_length=1, max_length=32)
    payload: dict = Field(default_factory=dict)


class RequestLimit:
    """Bounded per-process limiter; never trusts forwarded client-IP headers."""
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def check(self, key):
        now = time.monotonic()
        with self.lock:
            started, count = self.entries.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            if count >= 90:
                raise HTTPException(429, 'Too many requests; retry later')
            self.entries[key] = (started, count + 1)
            self.entries.move_to_end(key)
            while len(self.entries) > 4096:
                self.entries.popitem(last=False)


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request: Request):
            if request.method in ('POST', 'PUT', 'PATCH'):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 8192:
                        raise HTTPException(413, 'Request exceeds size limit')
                    body.extend(chunk)
                request._body = bytes(body)
            return await handler(request)
        return bounded


def create_installations_router(authenticate, store_factory=InstallationStore):
    router = APIRouter(prefix='/api/installations', tags=['installations'], route_class=BoundedRoute)
    store = lru_cache(maxsize=1)(store_factory)
    limiter = RequestLimit()

    def admin(request: Request):
        if not authenticate(request):
            raise HTTPException(401, 'Administrator authentication required')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            # Custom header cannot be sent cross-origin without a successful preflight.
            if request.headers.get('X-TRMM-Request') != 'installations':
                raise HTTPException(403, 'Missing same-origin request header')
            if request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none'):
                raise HTTPException(403, 'Cross-origin request denied')
            origin = request.headers.get('origin')
            if origin and (urlsplit(origin).netloc != request.headers.get('host') or urlsplit(origin).scheme != request.url.scheme):
                raise HTTPException(403, 'Cross-origin request denied')

    async def report_limit(request: Request):
        if len(await request.body()) > 8192:
            raise HTTPException(413, 'Report exceeds size limit')
        limiter.check(request.client.host if request.client else 'unknown')

    @router.get('/status', dependencies=[Depends(admin)])
    def status():
        return {'ready': False, 'blockers': RELEASE_BLOCKERS, 'distribution_enabled': False}

    @router.post('/invitations', dependencies=[Depends(admin)])
    def create_invitation():
        # No placeholder payload, env-variable bypass, or fictitious successful release.
        raise HTTPException(503, {'code': 'RELEASE_NOT_READY', 'blockers': RELEASE_BLOCKERS})

    @router.get('/invitations', dependencies=[Depends(admin)])
    def invitations():
        return {'invitations': store().list_invitations()}

    @router.delete('/invitations/{inv_id}', dependencies=[Depends(admin)])
    def revoke(inv_id: str):
        store().revoke_invitation(inv_id)
        return {'status': 'REVOKED'}

    @router.get('/attempts', dependencies=[Depends(admin)])
    def attempts():
        return {'attempts': store().list_attempts()}

    @router.get('/attempts/{attempt_id}/events', dependencies=[Depends(admin)])
    def events(attempt_id: str):
        return {'events': store().get_attempt_events(attempt_id)}

    @router.post('/redeem', dependencies=[Depends(report_limit)])
    def redeem():
        raise HTTPException(503, {'code': 'RELEASE_NOT_READY'})

    @router.post('/report', dependencies=[Depends(report_limit)])
    def report(data: Report):
        ok, reason = store().record_event(**data.model_dump())
        if not ok:
            raise HTTPException(401 if reason == 'UNAUTHORIZED_ATTEMPT_TOKEN' else 409, reason)
        return {'status': 'ACCEPTED', 'result': reason}

    return router
