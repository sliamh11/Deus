#!/bin/bash
# Build the Deus agent container image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

IMAGE_NAME="deus-agent"
TAG="${1:-latest}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"

echo "Building Deus agent container image..."
echo "Image: ${IMAGE_NAME}:${TAG}"

# Stage skill agent files for the container build.
# Each skill with an agent.ts gets its own directory under container/skill-agents/.
# This staging step runs on every build so the container always has current skills.
STAGING_DIR="container/skill-agents"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

if [ -d ".claude/skills" ]; then
  for skill_dir in .claude/skills/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    if [ -f "$skill_dir/agent.ts" ]; then
      mkdir -p "$STAGING_DIR/$skill_name"
      cp "$skill_dir/agent.ts" "$STAGING_DIR/$skill_name/"
      echo "  Staged skill agent: $skill_name"
    fi
  done
fi

# Build from project root so Dockerfile can access staged files
${CONTAINER_RUNTIME} build -t "${IMAGE_NAME}:${TAG}" -f container/Dockerfile .

# Clean up staging directory
rm -rf "$STAGING_DIR"

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"groupFolder\":\"test\",\"chatJid\":\"test@g.us\",\"isMain\":false}' | ${CONTAINER_RUNTIME} run -i ${IMAGE_NAME}:${TAG}"
