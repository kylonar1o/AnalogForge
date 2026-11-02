#!/usr/bin/env bash
# Package the whole repository (all commits + tracked files) for download/transfer.
#   scripts/package.sh   ->  dist/analogforge-<version>-repo.tar.gz  and  dist/analogforge-<version>-repo.zip
# Only committed content is packaged (a fresh `git clone` of this repository).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import re;print(re.search(r'^version = \"(.+)\"', open('pyproject.toml').read(), re.M).group(1))")
NAME="analogforge-${VERSION}"
if [ -n "$(git status --porcelain)" ]; then
  echo "warning: uncommitted changes are NOT included in the package" >&2
fi
mkdir -p dist
STAGE=$(mktemp -d)
git clone --quiet --no-hardlinks . "$STAGE/$NAME"
git -C "$STAGE/$NAME" remote remove origin
tar -C "$STAGE" -czf "dist/${NAME}-repo.tar.gz" "$NAME"
if command -v zip >/dev/null; then
  (cd "$STAGE" && zip -qr "$OLDPWD/dist/${NAME}-repo.zip" "$NAME")
fi
rm -rf "$STAGE"
ls -la dist/
