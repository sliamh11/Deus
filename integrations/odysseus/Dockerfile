# Deus curated-memory MCP sidecar for Odysseus.
# Standalone image — does NOT mirror container/Dockerfile (that's the Node agent
# image). All this needs is Python + mcp + sqlite-vec.
FROM python:3.11-slim

# mcp: the MCP server framework (FastMCP). sqlite-vec: native vector extension.
RUN pip install --no-cache-dir "mcp>=1.27" sqlite-vec

# Build-time verification: importing sqlite_vec is NOT enough — it succeeds even
# when extension loading is disabled, then fails at query time. Exercise the
# FULL load sequence so a broken image fails the build, not a user's recall.
RUN python -c "import sqlite3, sqlite_vec; \
c = sqlite3.connect(':memory:'); \
c.enable_load_extension(True); \
sqlite_vec.load(c); \
print('vec_version:', c.execute('SELECT vec_version()').fetchone()[0]); \
import mcp; print('mcp ok')"

WORKDIR /app
COPY _embed.py share_mcp_server.py /app/

# Mounted read-only at runtime; declared so the path exists if no mount is given.
VOLUME ["/data"]
EXPOSE 8200

CMD ["python", "share_mcp_server.py"]
