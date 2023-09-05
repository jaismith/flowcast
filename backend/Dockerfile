ARG FUNCTION_DIR='/function'

FROM python:3.10-buster as builder

RUN pip install poetry

ENV POETRY_NO_INTERACTION=1 \
  POETRY_VIRTUALENVS_IN_PROJECT=1 \
  POETRY_VIRTUALENVS_CREATE=1 \
  POETRY_CACHE_DIR=/tmp/poetry_cache

ARG FUNCTION_DIR
RUN mkdir -p ${FUNCTION_DIR}
WORKDIR ${FUNCTION_DIR}

COPY pyproject.toml poetry.lock ./

RUN poetry install --no-root \
  && rm -rf ${POETRY_CACHE_DIR}

FROM python:3.10-slim-buster as runtime

ARG FUNCTION_DIR
WORKDIR ${FUNCTION_DIR}

ENV VIRTUAL_ENV=/${FUNCTION_DIR}/.venv \
    PATH="/${FUNCTION_DIR}/.venv/bin:$PATH"

COPY --from=builder ${FUNCTION_DIR} ./

COPY src ./

ENTRYPOINT ["python", "-m", "awslambdaric"]
