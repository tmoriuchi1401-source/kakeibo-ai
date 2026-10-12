FROM python:3.12.15-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UNIFIED_STAGING_ENABLED=true UNIFIED_RECEIPT_WRITE_ENABLED=false
WORKDIR /service
COPY requirements.lock /tmp/requirements.lock
COPY wheels /tmp/wheels
RUN python -m pip install --no-index --find-links=/tmp/wheels --require-hashes -r /tmp/requirements.lock && rm -rf /tmp/wheels /tmp/requirements.lock
COPY bundle /service
ARG SOURCE_REVISION
ARG CREATED
LABEL org.opencontainers.image.revision=$SOURCE_REVISION org.opencontainers.image.source="https://github.com/tmoriuchi1401-source/kakeibo-ai" org.opencontainers.image.created=$CREATED
RUN test -n "$SOURCE_REVISION" && python -m compileall -q app services staging_shadow
USER 10001:10001
CMD ["python", "-m", "staging_shadow.serve"]
