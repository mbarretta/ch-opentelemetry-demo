# Agent: the released 3.0.0 agent image plus the concierge package, prompts, and the corrected
# shop tools. Built by `scripts/demo.py build` with the repository root as context; the root
# .dockerignore keeps everything else out. The digest is recorded in base-images.json.
FROM ghcr.io/open-telemetry/demo:3.0.0-agent@sha256:df5ee05e23e3bac13ce07e74a25f5c963a83387b3d50c27c17a476c71d0e4612

# The released image binds 0.0.0.0 through its own run.py; the concierge entry point reads
# AGENT_BIND and defaults to localhost for host runs.
ENV AGENT_BIND=0.0.0.0

COPY concierge /app/concierge
COPY prompts /app/prompts
COPY .runtime/tools.py /app/src/agents/tools.py

CMD ["python", "-m", "concierge.run_agent"]
