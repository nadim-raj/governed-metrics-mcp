#!/usr/bin/env bash
#
# The delivery pipeline (docs/operations.md). Boring when it works, reversible when it doesn't.
#
#   DEPLOY_HOST=deploy@metrics-host DEPLOY_DIR=/srv/governed-metrics scripts/deploy.sh
#
# Environment:
#   DEPLOY_HOST      ssh destination (required)
#   DEPLOY_DIR       remote base directory holding releases/ and current (required)
#   DEPLOY_SERVICE   systemd unit to restart              (default: governed-metrics)
#   DEPLOY_STATE     remote state directory to back up    (default: /var/lib/governed-metrics)
#   TARGET_PYTHON    interpreter the service runs on      (default: python3.12)
#   READY_URL        readiness endpoint on the host       (default: http://127.0.0.1:8080/readyz)
#   PAGE_COMMAND     local command run to page an operator (optional; receives the message)
#   BACKUP_DIR       local directory for state backups    (default: ~/governed-metrics-backups)
#
# Two places it refuses to continue: stale generated docs, and a rollback that would
# reinstate a known vulnerability (ADR 0013). In the second case it stops the service.
set -euo pipefail

: "${DEPLOY_HOST:?set DEPLOY_HOST}"
: "${DEPLOY_DIR:?set DEPLOY_DIR}"
SERVICE="${DEPLOY_SERVICE:-governed-metrics}"
STATE="${DEPLOY_STATE:-/var/lib/governed-metrics}"
PY="${TARGET_PYTHON:-python3.12}"
READY="${READY_URL:-http://127.0.0.1:8080/readyz}"
BACKUPS="${BACKUP_DIR:-$HOME/governed-metrics-backups}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
LOCAL_PY="${LOCAL_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$LOCAL_PY" ] || LOCAL_PY="$(command -v python3)"

say()  { printf '\n\033[1;36m→ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
page() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; [ -n "${PAGE_COMMAND:-}" ] && "$PAGE_COMMAND" "$*" || true; }
remote() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$DEPLOY_HOST" "$@"; }

# ---- 1. gates: nothing reaches the host unvalidated ------------------------------------
say "Validating definitions and policy"
"$LOCAL_PY" scripts/validate_knowledge.py || die "knowledge or policy failed validation"
say "Running tests (guard regressions included)"
"$LOCAL_PY" -m pytest -q || die "tests failed"
say "Checking generated docs"
"$LOCAL_PY" scripts/gen_docs.py --check || die "generated docs are stale: run scripts/gen_docs.py and commit"

[ -z "$(git status --porcelain)" ] || die "the working tree is not clean; deploy committed code only"
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short=12 HEAD)"
git branch -r --contains "$SHA" | grep -q . || die "$SHORT is not on any remote branch; push before deploying"

# ---- 2. one deploy at a time ------------------------------------------------------------
LOCK="$REPO/.deploy.lock"
mkdir "$LOCK" 2>/dev/null || die "another deploy is running here (remove $LOCK if it is stale)"
REMOTE_LOCK=0
cleanup() {
  rmdir "$LOCK" 2>/dev/null || true
  [ "$REMOTE_LOCK" = 1 ] && remote "rmdir '$DEPLOY_DIR/.deploy.lock'" 2>/dev/null || true
}
trap cleanup EXIT
remote "mkdir -p '$DEPLOY_DIR/releases' && mkdir '$DEPLOY_DIR/.deploy.lock'" \
  || die "another deploy holds the lock on $DEPLOY_HOST"
REMOTE_LOCK=1

# ---- 3. build the release on the target runtime -----------------------------------------
REL="$DEPLOY_DIR/releases/$SHORT"
say "Building release $SHORT on $DEPLOY_HOST with $PY"
git archive --format=tar "$SHA" | remote "rm -rf '$REL' && mkdir -p '$REL' && tar -x -C '$REL'"
remote "cd '$REL' && $PY -m venv .venv && .venv/bin/pip install --quiet . \
  && .venv/bin/python -m compileall -q src \
  && .venv/bin/python -c 'import governed_metrics.server' \
  && printf 'GM_KNOWLEDGE_VERSION=g:%s\n' '$SHORT' > .release.env" \
  || die "the release does not build or import on $PY; nothing was deployed"

# ---- 4. back up server-local state --------------------------------------------------------
mkdir -p "$BACKUPS" && chmod 700 "$BACKUPS"
SNAP="$BACKUPS/state-$(date +%Y%m%d-%H%M%S).tgz"
if remote "tar -czf - -C '$STATE' . 2>/dev/null" > "$SNAP" && tar -tzf "$SNAP" >/dev/null 2>&1; then
  chmod 600 "$SNAP"
  ls -1t "$BACKUPS"/state-*.tgz | tail -n +15 | while read -r old; do rm -f -- "$old"; done
  echo "   state backed up to $SNAP"
else
  rm -f "$SNAP"
  die "could not back up $STATE; refusing to deploy without a restore point"
fi

# ---- 5. switch and verify ----------------------------------------------------------------
PREV="$(remote "readlink '$DEPLOY_DIR/current' 2>/dev/null | xargs -r basename" || true)"

activate() {
  remote "ln -sfn '$DEPLOY_DIR/releases/$1' '$DEPLOY_DIR/current.tmp' \
    && mv -T '$DEPLOY_DIR/current.tmp' '$DEPLOY_DIR/current' && sudo systemctl restart '$SERVICE'"
}

healthy() {   # healthy <short-sha>: deep check passes AND it is serving that release
  local i body
  for i in $(seq 1 30); do
    body="$(remote "curl -fsS --max-time 5 '$READY'" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q '"ready": *true' \
       && printf '%s' "$body" | grep -q "\"knowledge_version\": *\"g:$1\""; then
      return 0
    fi
    sleep 2
  done
  printf '   last readiness answer: %s\n' "${body:-none}"
  return 1
}

crosses_security_fix() {   # would going back to $PREV drop a security fix in PREV..SHA?
  [ -n "$PREV" ] || return 1
  git cat-file -e "$PREV^{commit}" 2>/dev/null || git fetch -q origin || return 0
  git log --format='%s%n%b' "$PREV..$SHA" | grep -qiE '^fix\(security\)|\(security\)|^Security-Fix: *yes'
}

say "Activating $SHORT (previous: ${PREV:-none})"
activate "$SHORT" || true
if healthy "$SHORT"; then
  printf '\n\033[1;32m✓ %s is live and ready on %s\033[0m\n' "$SHORT" "$DEPLOY_HOST"
  exit 0
fi

# ---- 6. rollback, bounded by the security rule -------------------------------------------
# State and code stay consistent without a restore here: the server's only local state is the
# overlay and the audit log, both append-only JSON lines whose format no release migrates.
# Restoring the backup would erase grants and decisions recorded since the deploy, so it is
# kept as the operator's restore point instead. A release that ever migrates state must
# restore "$SNAP" in this step.
if [ -z "$PREV" ]; then
  remote "sudo systemctl stop '$SERVICE'" || true
  page "deploy $SHORT failed and there is no previous release; $SERVICE is stopped on $DEPLOY_HOST"
  exit 1
fi
if crosses_security_fix; then
  # An outage is loud, bounded and gets fixed. A silently reinstated vulnerability is none
  # of those. Fix forward instead.
  remote "sudo systemctl stop '$SERVICE'" || true
  page "deploy $SHORT failed and rolling back to $PREV would drop a security fix; $SERVICE is STOPPED on $DEPLOY_HOST. Fix forward."
  exit 1
fi
say "Rolling back to $PREV"
activate "$PREV" || true
if healthy "$PREV"; then
  page "deploy $SHORT failed its readiness check; rolled back to $PREV, which is healthy"
  exit 1
fi
remote "sudo systemctl stop '$SERVICE'" || true
page "deploy $SHORT failed and the rollback to $PREV did not come up; $SERVICE is stopped on $DEPLOY_HOST"
exit 1
