# Forecast Lambda image (arm64): CPU PyTorch + NeuralHydrology + flowcast_model/pipeline/eval + flowcast_serve.
# Build context is the repo root (infra-v2/lib/serving.ts); dependencies are pinned by model/uv.lock plus the
# serving extras below.
FROM public.ecr.aws/lambda/python:3.12-arm64 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /src
COPY model/pyproject.toml model/uv.lock model/
COPY pipeline pipeline
COPY evaluation evaluation
RUN cd model && uv export --frozen --no-dev --no-emit-project --no-hashes --no-header --no-annotate \
      | grep -v -E '^(-e|\.|nvidia-|triton|cuda-)' | sed -E 's/^torch==([0-9.]+)/torch==\1+cpu/' > /tmp/requirements.txt \
 && printf '%s\n' icechunk==2.2.2 pyproj==3.8.0 zstandard==0.25.0 py7zr==1.1.3 geopandas==1.2.0 pyogrio==0.13.0 \
      rasterio==1.5.2 shapely==2.1.2 pillow==12.3.0 google-crc32c==1.9.0 msgspec==0.21.1 fsspec==2026.9.0 s3fs==2026.9.0 aiobotocore==3.9.2 >> /tmp/requirements.txt \
 && uv pip install --no-cache --target /opt/deps --python /var/lang/bin/python3.12 \
      --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/requirements.txt \
 && python3 -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for p in [*pathlib.Path('/opt/deps').rglob('__pycache__'), pathlib.Path('/opt/deps/torch/test'), pathlib.Path('/opt/deps/torch/include')]]"

FROM public.ecr.aws/lambda/python:3.12-arm64
# rasterio's GDAL needs expat, which the base image lacks
RUN (dnf install -y expat || microdnf install -y expat) && (dnf clean all || microdnf clean all)
ENV PYTHONPATH=/opt/deps:${LAMBDA_TASK_ROOT} \
    NUMBA_CACHE_DIR=/tmp/numba \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp \
    FLOWCAST_WORK_DIR=/tmp/flowcast \
    FLOWCAST_SITES=${LAMBDA_TASK_ROOT}/sites.yaml \
    OMP_NUM_THREADS=2
COPY --from=build /opt/deps /opt/deps
COPY pipeline/src/flowcast_pipeline ${LAMBDA_TASK_ROOT}/flowcast_pipeline
COPY evaluation/src/flowcast_eval ${LAMBDA_TASK_ROOT}/flowcast_eval
COPY model/src/flowcast_model ${LAMBDA_TASK_ROOT}/flowcast_model
COPY serving/src/flowcast_serve ${LAMBDA_TASK_ROOT}/flowcast_serve
COPY pipeline/sites.yaml ${LAMBDA_TASK_ROOT}/sites.yaml
RUN python -c "import flowcast_serve.forecast_handler"
CMD ["flowcast_serve.forecast_handler.handler"]
