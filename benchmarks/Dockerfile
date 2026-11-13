ARG PG_IMAGE=public.ecr.aws/docker/library/postgres:17
FROM ${PG_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends python3 git \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /results && chown postgres:postgres /results
WORKDIR /workspace
COPY --chown=postgres:postgres benchmarks /workspace/benchmarks
USER postgres

FROM runtime AS cli
ENTRYPOINT ["python3", "-m", "benchmarks.benchmark"]

FROM runtime AS native
ENTRYPOINT ["python3", "-m", "benchmarks.native_baseline"]
