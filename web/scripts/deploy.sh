#!/usr/bin/env bash
# Build web/ and publish it to the site bucket behind CloudFront.
#
#   FLOWCAST_SITE_BUCKET=<bucket> FLOWCAST_DISTRIBUTION_ID=<id> scripts/deploy.sh [--preview <name>] [--dry-run]
#
# Production goes to the bucket root; --preview <name> builds with base /preview/<name>/ and publishes there.
# Content-hashed files under assets/ are cached for a year as immutable; index.html is no-cache and is the only
# path invalidated (page routes are rewritten to it at the edge by deploy/spa-rewrite.js, so nothing else holds
# a cached copy). Old hashed assets are kept, so a page loaded before a deploy can still fetch its chunks.
# --dry-run builds, prints what would be uploaded (aws s3 --dryrun) and skips the invalidation.
set -euo pipefail
cd "$(dirname "$0")/.."

preview=""
dry=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preview)
      preview="${2:-}"
      [[ "$preview" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "--preview needs a name of lowercase letters, digits and dashes" >&2; exit 2; }
      shift 2
      ;;
    --dry-run) dry="--dryrun"; shift ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

bucket="${FLOWCAST_SITE_BUCKET:?set FLOWCAST_SITE_BUCKET to the site bucket name}"
distribution="${FLOWCAST_DISTRIBUTION_ID:?set FLOWCAST_DISTRIBUTION_ID to the CloudFront distribution id}"
if [[ -n "$preview" ]]; then
  base="/preview/$preview/"
else
  base="/"
fi
dest="s3://$bucket${base}"

[[ -d node_modules ]] || npm ci
rm -rf dist
npx vite build --base "$base" --outDir dist
[[ -f dist/index.html && -d dist/assets ]] || { echo "build did not produce dist/index.html and dist/assets" >&2; exit 1; }

echo "Publishing $(du -sh dist | cut -f1) to $dest${dry:+ (dry run)}"
aws s3 sync dist/assets "${dest}assets" $dry --cache-control "public, max-age=31536000, immutable"
# Anything else from public/ (unhashed names): short cache.
aws s3 sync dist "$dest" $dry --exclude "index.html" --exclude "assets/*" --cache-control "public, max-age=300"
# Last, so the new page only goes live once everything it references is in place.
aws s3 cp dist/index.html "${dest}index.html" $dry --cache-control "no-cache" --content-type "text/html; charset=utf-8"

if [[ -n "$dry" ]]; then
  echo "Dry run: skipping invalidation of ${base}index.html"
else
  aws cloudfront create-invalidation --distribution-id "$distribution" --paths "${base}index.html" --query 'Invalidation.Id' --output text
fi
echo "Done: ${base}"
