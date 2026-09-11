# Frontend proxy: the released 3.0.0 Envoy image with this repository's envoy.tmpl.yaml, which
# adds the /api/assistant/ route and drops the chatbot cluster. The released ENTRYPOINT still
# renders the template with envsubst at container start, so ${VAR} placeholders stay in place.
# Digest in base-images.json.
FROM ghcr.io/open-telemetry/demo:3.0.0-frontend-proxy@sha256:1c53bfb596970a014682925f804c686762c0c593949ce58a90ac38563f317841

COPY --chown=envoy:envoy docker/envoy.tmpl.yaml /home/envoy/envoy.tmpl.yaml
