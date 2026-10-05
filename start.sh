#!/usr/bin/env bash
# SafePath — Quick Start Script
# Activates the virtual environment and launches the FastAPI server.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  echo "[ERROR] Virtual environment not found. Run setup first."
  exit 1
fi

echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║       SafePath — Heuristic Routing Engine v1.0           ║"
echo "  ║  Route: http://localhost:8000                            ║"
echo "  ║  Docs:  http://localhost:8000/docs                       ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload
