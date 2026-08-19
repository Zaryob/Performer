# syntax=docker/dockerfile:1

FROM ubuntu:24.04 AS demo-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY tests/target/contention.cc ./contention.cc
# Keeping the demo binary self-contained lets bpftrace resolve the complete
# user stack from the target executable across the container filesystem
# boundary. Frame pointers alone are not enough when shared objects differ.
RUN g++ \
    -O2 \
    -g \
    -static \
    -fno-omit-frame-pointer \
    -pthread \
    -std=c++17 \
    -Wall \
    -Wextra \
    -o contention \
    contention.cc


FROM ubuntu:24.04 AS demo-target

COPY --from=demo-build /build/contention /usr/local/bin/contention
COPY tests/docker/run-demo-target.sh /usr/local/bin/run-demo-target
RUN chmod 0755 /usr/local/bin/run-demo-target

ENTRYPOINT ["/usr/local/bin/run-demo-target"]


FROM ubuntu:24.04 AS performer-collector

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PERFORMER_PROBES_DIR=/opt/performer/probes \
    PERFORMER_PROFILES_DIR=/opt/performer/collector/profiles \
    PERFORMER_SCHEMA_DIR=/opt/performer/schema

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bpftrace \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY collector /opt/performer/collector
COPY probes /opt/performer/probes
COPY schema /opt/performer/schema
COPY tests/docker/collect-demo.sh /usr/local/bin/collect-demo
RUN chmod 0755 \
    /opt/performer/collector/bin/performer \
    /usr/local/bin/collect-demo

ENTRYPOINT ["/usr/local/bin/collect-demo"]
