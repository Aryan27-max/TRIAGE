#!/usr/bin/env bash
# Populates hf-space/ — the staging area for the Hugging Face Space deploy target.
#
# hf-space/ is a SEPARATE git repo (gitignored from this one, see DEPLOYMENT.md). This
# script copies exactly what the Dockerfile's own COPY lines need to build and run —
# not a guess, read straight off the Dockerfile — plus the Space-specific README.md
# frontmatter HF requires and this repo's root README.md must never carry.
#
# Safe to re-run any time src/, error_policy.json, the model, or the eval runs change:
# it wipes hf-space/ and rebuilds it from the current working tree.
#
#   scripts/sync_hf_space.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DEST="hf-space"

echo "Rebuilding ${DEST}/ from the current working tree..."
rm -rf "$DEST"
mkdir -p "$DEST"

# -- everything the Dockerfile's COPY lines require, verbatim ----------------------
cp -r src "$DEST/src"
find "$DEST/src" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
cp error_policy.json "$DEST/"
cp pyproject.toml "$DEST/"
cp uv.lock "$DEST/"
cp Dockerfile "$DEST/"
cp .dockerignore "$DEST/"

# hf-space/ becomes its own git repo (see DEPLOYMENT.md) — it needs its own .gitignore,
# independent of this repo's.
cat > "$DEST/.gitignore" <<'EOF'
__pycache__/
*.pyc
.venv/
.env
EOF

mkdir -p "$DEST/eval"
cp eval/__init__.py eval/run_arms.py eval/score.py eval/report.py "$DEST/eval/"
cp -r eval/model "$DEST/eval/model"

# Only the two 8000-payment exhibit runs — matching what's actually committed to this
# repo and what the Dockerfile bakes into the production image. The 12 MB training
# population (run_1NXsztG1.db) is deliberately not shipped; see .gitignore's note.
mkdir -p "$DEST/eval/runs"
for f in eval/runs/run_DbCttM8e.db eval/runs/run_gsoV0kLl.db; do
  if [ -f "$f" ]; then
    cp "$f" "$DEST/eval/runs/"
  else
    echo "WARNING: expected exhibit run missing: $f" >&2
  fi
done

# Space-specific README with HF's required frontmatter. This is NOT a copy of the
# root README.md — the root README is the submission document (see CLAUDE.md) and
# must never carry Spaces YAML.
cat > "$DEST/README.md" <<'EOF'
---
title: TRIAGE API
sdk: docker
app_port: 7860
---

# TRIAGE API

The decision layer for Razorpay payment failures: 110 published error codes classified
into eight bounded, auditable recovery actions (retry, switch rail, switch instrument,
nudge the customer, wait for status, stop, or alert the merchant).

This is a **read-only exhibit**: two pre-computed 8000-payment evaluation runs are
baked into the image and served as-is. `POST /v1/recovery/decide` is stateless and
still works — ask it for a decision on any of the 110 codes. Every other write is
refused with `503`. See `/docs` for the full API, and the project's GitHub repo for
the submission this Space supports.
EOF

echo "Done. ${DEST}/ is a plain directory, not yet a git repo — see DEPLOYMENT.md for"
echo "the exact commands to turn it into one and push it to Hugging Face."
