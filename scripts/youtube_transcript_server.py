"""YouTube transcript MCP server (stdio transport)."""
import re
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp package not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

from youtube_transcript_api import YouTubeTranscriptApi

mcp = FastMCP("youtube-transcript")

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")
_api = YouTubeTranscriptApi()


@mcp.tool(structured_output=False)
def get_transcript(url: str, lang: str = "en") -> str:
    """Extract transcript from a YouTube video URL or video ID."""
    m = _VIDEO_ID_RE.search(url)
    video_id = m.group(1) if m else url.strip()
    try:
        transcript = _api.fetch(video_id, languages=[lang])
    except Exception as exc:
        return f"Error: {exc}"
    return "\n".join(s.text for s in transcript)


if __name__ == "__main__":
    mcp.run()
