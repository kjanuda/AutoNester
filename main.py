"""AutoNester v0.4 API: DXF preview and guarded constraint-aware nesting."""
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
import json
import os
from starlette.responses import JSONResponse
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from nesting_engine import generate_nesting, MAX_PIECES
from optimization_compare import compare_layouts
from geometric_optimizer import optimize_geometry
from layout_audit import replay_benchmark
from cad_preflight import inspect_import
from phase8_compactor import refine_geometry
from dxf_import import parse_dxf_patterns, DxfImportError, MAX_FILE_BYTES
from marker_export import build_preview_bundle
from cad_source_audit import audit_cad_source
from cad_metadata import inspect_cad_metadata, build_metadata_preview_bundle
from cad_aware_export import build_cad_aware_nested_preview
from phase13_nfp import optimize_nfp_hybrid
from tuka_compatibility import inspect_tuka_exchange

# Keep the public preview stateless; never store user uploads permanently.
PRODUCTION = os.getenv('APP_ENV', 'development').lower() == 'production'
DEFAULT_ORIGINS = 'http://localhost:3000,http://127.0.0.1:3000'
allowed_origins = [origin.strip().rstrip('/') for origin in
                   os.getenv('ALLOWED_ORIGINS', DEFAULT_ORIGINS).split(',') if origin.strip()]
if PRODUCTION and (not os.getenv('ALLOWED_ORIGINS') or '*' in allowed_origins):
    raise RuntimeError('Set ALLOWED_ORIGINS to the exact Vercel origin for production')

app = FastAPI(title='AutoNester API', version='0.14.0',
              description='Garment nesting research prototype. NOT FOR CUTTING.',
              docs_url=None if PRODUCTION else '/docs',
              redoc_url=None if PRODUCTION else '/redoc',
              openapi_url=None if PRODUCTION else '/openapi.json')

class RequestBodyLimitMiddleware:
    """Bound incoming POST request bodies before FastAPI multipart parsing.

    Multipart uploads are parsed by Starlette before endpoint-specific DXF limits,
    so cap the entire HTTP body as well, including chunked requests.
    """
    def __init__(self, wrapped_app, max_body_bytes=10 * 1024 * 1024):
        self.wrapped_app = wrapped_app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return await self.wrapped_app(scope, receive, send)
        headers = dict(scope.get('headers') or [])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)(scope, receive, send)
        if length > self.max_body_bytes:
            return await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            if message['type'] != 'http.request':
                continue
            size += len(message.get('body', b''))
            if size > self.max_body_bytes:
                return await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
            chunks.append(message)
            if not message.get('more_body', False):
                break
        index = 0
        async def replay_receive():
            nonlocal index
            if index < len(chunks):
                message = chunks[index]
                index += 1
                return message
            return await receive()
        return await self.wrapped_app(scope, replay_receive, send)

app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_credentials=False, allow_methods=['GET', 'POST'],
                   allow_headers=['Content-Type'], max_age=600)


class Pattern(BaseModel):
    name: str
    points: list[tuple[float, float]] = Field(min_length=3, max_length=80)
    quantity: int = Field(default=1, ge=1, le=MAX_PIECES)
    allowed_rotations: list[int] = Field(default_factory=lambda: [0], min_length=1, max_length=12)
    fold_required: bool = False
    fold_edge: str | None = None
    fold_verified: bool = False
    grainline_verified: bool = False
    fold_status_verified: bool = False

class NestingRequest(BaseModel):
    fabric_width: float = Field(gt=0)
    gap: float = Field(default=2.0, ge=0)
    strategy: str = 'fast'
    fabric_mode: str = 'flat'
    fabric_fold_side: str = 'left'
    enforce_constraints: bool = False
    patterns: list[Pattern] = Field(min_length=1)

@app.get('/')
def home():
    return {'project': 'AutoNester', 'status': 'running', 'version': '0.14.0',
            'production_ready': False}

@app.get('/health')
def health():
    return {'status': 'ok', 'version': '0.14.0', 'production_ready': False}

@app.post('/api/nesting/generate')
def create_nesting(request: NestingRequest):
    try:
        return generate_nesting(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width, gap=request.gap,
            strategy=request.strategy, fabric_mode=request.fabric_mode,
            fabric_fold_side=request.fabric_fold_side,
            enforce_constraints=request.enforce_constraints)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


class CompareRequest(NestingRequest):
    max_trials: int = Field(default=7, ge=1, le=9)

@app.post('/api/nesting/compare')
def compare_nesting(request: CompareRequest):
    try:
        return compare_layouts(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width,
            gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode,
            fabric_fold_side=request.fabric_fold_side,
            max_trials=request.max_trials,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

class GeometricOptimizeRequest(NestingRequest):
    effort: str = 'standard'
    seed: int = Field(default=42, ge=0, le=999999)


@app.post('/api/nesting/optimize')
def optimize_nesting(request: GeometricOptimizeRequest):
    try:
        return optimize_geometry(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width,
            gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode,
            fabric_fold_side=request.fabric_fold_side,
            effort=request.effort,
            seed=request.seed,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


class ReplayBenchmarkRequest(NestingRequest):
    effort: str = 'quick'
    seed: int = Field(default=42, ge=0, le=999998)


@app.post('/api/nesting/optimize-nfp')
def optimize_partial_nfp(request: GeometricOptimizeRequest):
    try:
        return optimize_nfp_hybrid(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width, gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode,
            fabric_fold_side=request.fabric_fold_side,
            effort=request.effort, seed=request.seed)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post('/api/nesting/benchmark')
def run_replay_benchmark(request: ReplayBenchmarkRequest):
    try:
        return replay_benchmark(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width,
            gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode,
            fabric_fold_side=request.fabric_fold_side,
            effort=request.effort,
            seed=request.seed,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post('/api/nesting/refine')
def refine_nesting(request: GeometricOptimizeRequest):
    try:
        return refine_geometry(
            patterns=[p.model_dump() for p in request.patterns],
            fabric_width=request.fabric_width, gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode, fabric_fold_side=request.fabric_fold_side,
            effort=request.effort, seed=request.seed,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post('/api/patterns/import-dxf')
async def import_dxf(file: UploadFile = File(...), unitless_units: str = Form('mm'),
                     source_units_override: str = Form('auto')):
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        parsed = parse_dxf_patterns(raw, file.filename or 'pattern.dxf',
                                    unitless_units, source_units_override)
        parsed['preflight'] = inspect_import(parsed)
        return parsed
    except DxfImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()


class PreviewBundleRequest(NestingRequest):
    layout: dict
    source_file_name: str | None = Field(default=None, max_length=120)
    source_dxf_sha256: str | None = Field(default=None, max_length=64)


@app.post('/api/nesting/export-preview-bundle')
def export_preview_bundle(request: PreviewBundleRequest):
    try:
        raw, _ = build_preview_bundle(
            patterns=[p.model_dump() for p in request.patterns],
            layout=request.layout, fabric_width=request.fabric_width, gap=request.gap,
            enforce_constraints=request.enforce_constraints,
            fabric_mode=request.fabric_mode, fabric_fold_side=request.fabric_fold_side,
            source_file_name=request.source_file_name,
            source_dxf_sha256=request.source_dxf_sha256)
        return Response(content=raw, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="AutoNester_PHASE9_PREVIEW_NOT_FOR_CUTTING.zip"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        })
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post('/api/cad/source-audit')
async def source_cad_audit(file: UploadFile = File(...),
                           unitless_units: str = Form('mm'),
                           source_units_override: str = Form('auto'),
                           reference_pattern_index: int | None = Form(default=None),
                           reference_axis: str = Form('width'),
                           reference_mm: float | None = Form(default=None),
                           tolerance_mm: float = Form(default=1.0)):
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        return audit_cad_source(
            raw=raw, filename=file.filename or 'pattern.dxf',
            unitless_units=unitless_units, source_units_override=source_units_override,
            reference_pattern_index=reference_pattern_index, reference_axis=reference_axis,
            reference_mm=reference_mm, tolerance_mm=tolerance_mm)
    except DxfImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()


@app.post('/api/cad/metadata-inspect')
async def cad_metadata_inspect(file: UploadFile = File(...),
                               unitless_units: str = Form('mm'),
                               source_units_override: str = Form('auto')):
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        return inspect_cad_metadata(
            raw=raw, filename=file.filename or 'pattern.dxf',
            unitless_units=unitless_units,
            source_units_override=source_units_override)
    except DxfImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()


@app.post('/api/cad/metadata-preview-bundle')
async def cad_metadata_preview_bundle(file: UploadFile = File(...),
                                      unitless_units: str = Form('mm'),
                                      source_units_override: str = Form('auto')):
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        bundle, _ = build_metadata_preview_bundle(
            raw=raw, filename=file.filename or 'pattern.dxf',
            unitless_units=unitless_units, source_units_override=source_units_override)
        return Response(content=bundle, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="AutoNester_PHASE11_CAD_METADATA_PREVIEW_NOT_FOR_CUTTING.zip"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })
    except DxfImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()


@app.post('/api/cad/export-nested-metadata-preview')
async def export_cad_aware_nested_preview(
    file: UploadFile = File(...),
    unitless_units: str = Form('mm'),
    source_units_override: str = Form('auto'),
    selected_patterns_json: str = Form(...),
    layout_json: str = Form(...),
    fabric_width: float = Form(...),
    gap: float = Form(default=2.0),
    fabric_mode: str = Form('flat'),
    enforce_constraints: bool = Form(False),
):
    try:
        if len(selected_patterns_json) > 250000 or len(layout_json) > 1250000:
            raise ValueError('CAD export inputs exceed size limit')
        raw = await file.read(MAX_FILE_BYTES + 1)
        selected = json.loads(selected_patterns_json)
        layout = json.loads(layout_json)
        bundle, _ = build_cad_aware_nested_preview(
            raw=raw, filename=file.filename or 'pattern.dxf',
            selected_patterns=selected, layout=layout,
            fabric_width=fabric_width, gap=gap,
            unitless_units=unitless_units,
            source_units_override=source_units_override,
            fabric_mode=fabric_mode, enforce_constraints=enforce_constraints,
        )
        return Response(content=bundle, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="AutoNester_PHASE12_NESTED_CAD_PREVIEW_NOT_FOR_CUTTING.zip"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })
    except (ValueError, TypeError, json.JSONDecodeError, DxfImportError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()


@app.post('/api/cad/tuka-compatibility-preflight')
async def tuka_compatibility_preflight(file: UploadFile = File(...),
                                      unitless_units: str = Form('mm'),
                                      source_units_override: str = Form('auto')):
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        return inspect_tuka_exchange(raw=raw, filename=file.filename or 'source.dxf',
                                     unitless_units=unitless_units,
                                     source_units_override=source_units_override)
    except DxfImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await file.close()
