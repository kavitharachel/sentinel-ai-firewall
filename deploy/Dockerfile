# SENTINEL public judges' demo.  Build from the REPOSITORY ROOT:   docker build -f deploy/Dockerfile -t sentinel-demo .
# Stage 1 runs scripts/build_public.py, which copies ONLY the allow-listed files and FAILS the build on any secret shape, denied path
# or forbidden import. Stage 2 contains nothing but that artifact. evidence/, tests/, *.xml and .env* never reach the final image.
FROM python:3.12-slim AS build
WORKDIR /src
COPY . /src
RUN python scripts/build_public.py --out /out/public

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SENTINEL_PUBLIC=1 SENTINEL_TRUSTED_PROXY=1 SENTINEL_TRUSTED_HOPS=1 PORT=8080
RUN useradd --system --no-create-home --uid 10001 sentinel
WORKDIR /app
COPY --from=build --chown=root:root /out/public /app
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080')), timeout=2)" || exit 1
# SENTINEL_PUBLIC_HOST (your public hostname) MUST be set at deploy time. No API keys or PayPal secrets are needed or read.
CMD ["python", "-m", "roadworthy.dashboard", "--public"]
