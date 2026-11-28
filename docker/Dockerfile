FROM python:3.12.16-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64 AS browser

RUN groupadd --gid 10001 textfold \
    && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home --shell /usr/sbin/nologin textfold \
    && install -d -m 0755 /opt/textfold-docker \
    && install -d -o textfold -g textfold -m 0700 /run/textfold-relay
COPY --chmod=0644 docker/transport.py /opt/textfold-docker/transport.py

USER textfold
ENTRYPOINT ["python", "-I", "-B", "/opt/textfold-docker/transport.py"]
CMD ["browser"]

FROM browser AS reader
USER root
COPY docker/requirements-linux.txt /opt/textfold-docker/requirements-linux.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: \
        -r /opt/textfold-docker/requirements-linux.txt \
    && python -m pip check \
    && install -d -o textfold -g textfold -m 0700 /data /data/home /data/library /data/settings

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TEXTFOLD_DATA=/data/library \
    TEXTFOLD_SETTINGS_DIR=/data/settings \
    XDG_CACHE_HOME=/data/cache \
    XDG_DATA_HOME=/data
WORKDIR /opt/textfold
COPY textfold ./textfold
RUN chmod -R a+rX /opt/textfold

USER textfold
CMD ["reader"]
