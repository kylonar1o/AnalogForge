#!/usr/bin/env bash
# Package the whole repository (including git history) for download/transfer.
#   scripts/package.sh            -> dist/analogforge-<version>-repo.tar.gz and .zip
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import re;print(re.search(r'^version = \"(.+)\"', open('pyproject.toml').read(), re.M).group(1))")
NAME="analogforge-${VERSION}"
mkdir -p dist
STAGE=$(mktemp -d)
# copy tracked + untracked-but-not-ignored files and the .git directory
git ls-files -co --exclude-standard -z | rsync -a --from0 --files-from=- ./ "$STAGE/$NAME/"
cp -a .git "$STAGE/$NAME/.git"
tar -C "$STAGE" -czf "dist/${NAME}-repo.tar.gz" "$NAME"
(cd "$STAGE" && zip -qr "${OLDPWD}/dist/${NAME}-repo.zip" "$NAME")
rm -rf "$STAGE"
ls -la dist/
