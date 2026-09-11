# MCP: the released 3.0.0 mcp image plus the concierge package (telemetry processors and the
# run_mcp entry point) and the corrected shop tools at the path the released server imports.
# prompts/ is copied so the package layout matches the agent image (concierge locates prompts
# next to itself). Built with the repository root as context; digest in base-images.json.
FROM ghcr.io/open-telemetry/demo:3.0.0-mcp@sha256:654f860148baa3d92d1cc89fa7a14848cd7070cd9d7ea5a63773782c9f23fcc1

COPY concierge /app/concierge
COPY prompts /app/prompts
COPY .runtime/tools.py /app/src/mcp_server/tools.py

CMD ["python", "-m", "concierge.run_mcp"]
