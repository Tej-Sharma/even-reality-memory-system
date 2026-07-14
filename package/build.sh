#!/usr/bin/env bash
# Build the packaged .ehpk for the Even dev portal.
#
# Bundles the REAL lens app (src/app/glasses/lib) into dist/app.js so the
# package itself contains the bridge calls reviewers check for. The previous
# thin-launcher (redirect to constella.app/glasses) was rejected because the
# reviewer analyzes the uploaded bundle, which contained no SDK calls.
#
# Usage: ./glasses-pack/build.sh   (from the constella-website repo root)
set -euo pipefail
cd "$(dirname "$0")/.."

npx esbuild glasses-pack/entry.ts \
  --bundle \
  --format=iife \
  --platform=browser \
  --target=es2019 \
  --define:process.env.NEXT_PUBLIC_API_BASE='"https://fastfind.app"' \
  --outfile=glasses-pack/dist/app.js

npx @evenrealities/evenhub-cli pack glasses-pack/app.json glasses-pack/dist -o constella-glasses.ehpk
echo "Built constella-glasses.ehpk"
