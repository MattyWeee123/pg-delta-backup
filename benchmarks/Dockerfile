ARG PG_IMAGE=postgres:17
FROM ${PG_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY --chown=postgres:postgres benchmarks /workspace/benchmarks
USER postgres
ENTRYPOINT ["python3", "-m", "benchmarks.native_baseline"]
