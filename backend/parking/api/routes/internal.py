"""`/internal/*`: worker -> API (api.md §5.1). Worker token only; no rate limit, no CORS.

Bodies are parsed after the token check (not by FastAPI's body binding), so a request without
the token always gets 401, whatever it sends. A bad payload is a 422, logged and counted.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError

from parking.api.deps import ApiError, Runtime, RuntimeDep, require_worker_token
from parking.core.fusion import UnknownCameraError
from parking.messages import CameraHealthMsg, FlowEventAck, FlowEventBatch, Observation

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", dependencies=[Depends(require_worker_token)])


def _reject(rt: Runtime, path: str, message: str, details: list | None = None) -> ApiError:
    rt.ingestor.stats.rejected += 1
    log.warning("rejected %s: %s", path, message)
    return ApiError(422, "bad_request", message, details)


async def _parse[M: BaseModel](request: Request, rt: Runtime, model: type[M]) -> M:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as e:
        details = e.errors(include_url=False, include_context=False, include_input=False)
        first = details[0] if details else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        msg = f"invalid {model.__name__}: {where}: {first.get('msg', 'invalid')}"
        raise _reject(rt, request.url.path, msg, details) from None


def _schema(model: type[BaseModel]) -> dict:
    content = {"application/json": {"schema": model.model_json_schema()}}
    return {"requestBody": {"required": True, "content": content}}


@router.post("/observations", status_code=204, openapi_extra=_schema(Observation))
async def post_observation(request: Request, rt: RuntimeDep) -> Response:
    obs = await _parse(request, rt, Observation)
    try:
        await rt.ingestor.observation(obs)
    except UnknownCameraError as e:
        raise _reject(rt, request.url.path, str(e)) from None
    return Response(status_code=204)


@router.post("/flow-events", response_model=FlowEventAck, openapi_extra=_schema(FlowEventBatch))
async def post_flow_events(request: Request, rt: RuntimeDep) -> FlowEventAck:
    batch = await _parse(request, rt, FlowEventBatch)
    try:
        return await rt.ingestor.flow_events(batch)
    except UnknownCameraError as e:
        raise _reject(rt, request.url.path, str(e)) from None


@router.post("/health", status_code=204, openapi_extra=_schema(CameraHealthMsg))
async def post_health(request: Request, rt: RuntimeDep) -> Response:
    msg = await _parse(request, rt, CameraHealthMsg)
    try:
        await rt.ingestor.health(msg)
    except UnknownCameraError as e:
        raise _reject(rt, request.url.path, str(e)) from None
    return Response(status_code=204)
