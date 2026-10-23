#!/bin/bash
# Install the incumbent refresh tools T11 runs (experiments/incumbents) on a benchmark VM:
# pg_probackup for PostgreSQL 17 from Postgres Pro's repository, and WAL-G's PostgreSQL
# release binary from GitHub, checked against its published SHA-256 checksum.
#
# Debian 12 (bookworm), amd64, as root, after startup.sh has installed PostgreSQL 17
# from PGDG: `sudo infra/gcp/install_incumbents.sh`. Idempotent: a tool already at the
# wanted version is left alone. Prints both versions and records how each was installed
# in /usr/local/share/pgd-incumbents.txt.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -y -q -o DPkg::Lock::Timeout=600)

PG_MAJOR=${PG_MAJOR:-17}
# On 2026-10-23 the repository had pg-probackup-17 2.5.16 for bookworm. Without such a
# package, pg_probackup is built from this tag of the official source instead.
PG_PROBACKUP_TAG=${PG_PROBACKUP_TAG:-2.5.16}
PROBACKUP_KEY_URL=https://repo.postgrespro.ru/pg_probackup/keys/GPG-KEY-PG-PROBACKUP
PROBACKUP_REPO=https://repo.postgrespro.ru/pg_probackup/deb
PROBACKUP_KEYRING=/etc/apt/keyrings/pg_probackup.asc
PROBACKUP_SOURCE=https://github.com/postgrespro/pg_probackup
WALG_VERSION=${WALG_VERSION:-v3.0.9}
# The Ubuntu 22.04 build needs glibc 2.35 and Debian 12 has 2.36; the 24.04 build needs 2.39.
WALG_ASSET=${WALG_ASSET:-wal-g-pg-22.04-amd64.tar.gz}
# SHA-256 of that asset in the v3.0.9 release, from its .sha256 file and GitHub's asset
# digest. A different WALG_VERSION or WALG_ASSET needs its own WALG_SHA256, or
# WALG_SHA256= to rely on the published .sha256 file alone.
WALG_SHA256=${WALG_SHA256-4f03ee4679db7f660bfd1b2b8291ac5df247cd2ec48074b60c112f4897299f24}
WALG_BIN=/usr/local/bin/wal-g
PROVENANCE=/usr/local/share/pgd-incumbents.txt

fail() { echo "install_incumbents: $*" >&2; exit 1; }
note() { echo "$*" | tee -a "$PROVENANCE"; }

[ "$(id -u)" -eq 0 ] || fail "run as root (sudo $0)"
. /etc/os-release
[ "${VERSION_CODENAME:-}" = bookworm ] ||
  echo "install_incumbents: written for Debian 12 (bookworm), this is ${PRETTY_NAME:-unknown}" >&2
[ "$(dpkg --print-architecture)" = amd64 ] || fail "only amd64 is handled"
PG_BINDIR=/usr/lib/postgresql/$PG_MAJOR/bin
[ -x "$PG_BINDIR/postgres" ] || fail "PostgreSQL $PG_MAJOR is not installed; run startup.sh first"
install -d -m 0755 "$(dirname "$PROVENANCE")"
command -v curl >/dev/null && command -v gpg >/dev/null || {
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends curl ca-certificates gnupg
}

# ---------------------------------------------------------------------------
# pg_probackup
# ---------------------------------------------------------------------------
probackup_path() {
  command -v "pg_probackup-$PG_MAJOR" 2>/dev/null ||
    dpkg -L "pg-probackup-$PG_MAJOR" 2>/dev/null | grep -m1 "/bin/pg_probackup" || true
}

build_probackup() {
  # pg_probackup compiles part of PostgreSQL's own source (pg_basebackup's WAL streaming,
  # the WAL reader), so besides the PGDG server headers it needs the raw source tree of
  # the installed server version, checked against its published SHA-256.
  "${APT[@]}" install --no-install-recommends build-essential git bzip2 gettext \
    "postgresql-server-dev-$PG_MAJOR" libssl-dev zlib1g-dev libreadline-dev libzstd-dev \
    liblz4-dev libkrb5-dev libselinux1-dev libxml2-dev libxslt1-dev libpam0g-dev
  local version dir url
  version=$("$PG_BINDIR/pg_config" --version | awk '{print $2}')
  dir=$(mktemp -d /usr/local/src/pgd-probackup.XXXXXX)
  url=https://ftp.postgresql.org/pub/source/v$version/postgresql-$version.tar.bz2
  curl -fsSL -o "$dir/postgresql-$version.tar.bz2" "$url"
  curl -fsSL -o "$dir/postgresql-$version.tar.bz2.sha256" "$url.sha256"
  (cd "$dir" && sha256sum -c "postgresql-$version.tar.bz2.sha256")
  tar -xjf "$dir/postgresql-$version.tar.bz2" -C "$dir"
  git clone --quiet --depth 1 --branch "$PG_PROBACKUP_TAG" "$PROBACKUP_SOURCE" "$dir/pg_probackup"
  # PGDG's libpq-dev is the newest major's (18 on 2026-10-23), so its internal libpq-int.h
  # doesn't match a 17 server: take libpq's internal headers from the 17 source instead.
  # libpgcommon and libpgport live in the server's own lib directory. with_llvm=no skips
  # the optional JIT bitcode, which would hit the same header mismatch.
  make -C "$dir/pg_probackup" USE_PGXS=1 PG_CONFIG="$PG_BINDIR/pg_config" with_llvm=no \
    top_srcdir="$dir/postgresql-$version" \
    COPT="-I$dir/postgresql-$version/src/interfaces/libpq -I$dir/postgresql-$version/src/include -L$PG_BINDIR/../lib"
  install -m 0755 "$dir/pg_probackup/pg_probackup" "/usr/local/bin/pg_probackup-$PG_MAJOR"
  note "pg_probackup: built from $PROBACKUP_SOURCE tag $PG_PROBACKUP_TAG against" \
    "postgresql-server-dev-$PG_MAJOR and the PostgreSQL $version source" \
    "($(date -u +%FT%TZ)); $1"
  rm -rf "$dir"
}

if [ -z "$(probackup_path)" ] && [ "${PG_PROBACKUP_FROM_SOURCE:-0}" = 1 ]; then
  build_probackup "PG_PROBACKUP_FROM_SOURCE=1 was set"
elif [ -z "$(probackup_path)" ] && ! curl -fsS --connect-timeout 20 -o /dev/null "$PROBACKUP_KEY_URL"; then
  # Seen on 2026-10-23: repo.postgrespro.ru did not answer from us-central1-a.
  build_probackup "$PROBACKUP_REPO could not be reached"
elif [ -z "$(probackup_path)" ]; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL -o "$PROBACKUP_KEYRING.tmp" "$PROBACKUP_KEY_URL"
  gpg --quiet --show-keys "$PROBACKUP_KEYRING.tmp" >/dev/null || fail "bad key from $PROBACKUP_KEY_URL"
  mv "$PROBACKUP_KEYRING.tmp" "$PROBACKUP_KEYRING"
  echo "deb [arch=amd64 signed-by=$PROBACKUP_KEYRING] $PROBACKUP_REPO $VERSION_CODENAME main-$VERSION_CODENAME" \
    > /etc/apt/sources.list.d/pg_probackup.list
  "${APT[@]}" update
  if apt-cache policy "pg-probackup-$PG_MAJOR" | grep -q 'Candidate: [0-9]'; then
    "${APT[@]}" install --no-install-recommends "pg-probackup-$PG_MAJOR"
    note "pg_probackup: package pg-probackup-$PG_MAJOR" \
      "$(dpkg-query -W -f '${Version}' "pg-probackup-$PG_MAJOR") from $PROBACKUP_REPO" \
      "($VERSION_CODENAME, $(date -u +%FT%TZ))"
  else
    build_probackup "the repository had no pg-probackup-$PG_MAJOR for $VERSION_CODENAME"
  fi
fi
PROBACKUP=$(probackup_path)
[ -n "$PROBACKUP" ] || fail "pg_probackup-$PG_MAJOR was not installed"
if [ "$PROBACKUP" != "$(command -v "pg_probackup-$PG_MAJOR" 2>/dev/null || true)" ]; then
  ln -sf "$PROBACKUP" "/usr/local/bin/pg_probackup-$PG_MAJOR"
fi

# ---------------------------------------------------------------------------
# WAL-G
# ---------------------------------------------------------------------------
if ! { [ -x "$WALG_BIN" ] && "$WALG_BIN" --version 2>/dev/null | grep -q -- "$WALG_VERSION"; }; then
  dir=$(mktemp -d)
  trap 'rm -rf "$dir"' EXIT
  base=https://github.com/wal-g/wal-g/releases/download/$WALG_VERSION
  curl -fsSL -o "$dir/$WALG_ASSET" "$base/$WALG_ASSET"
  curl -fsSL -o "$dir/$WALG_ASSET.sha256" "$base/$WALG_ASSET.sha256"
  published=$(awk '{print $1}' "$dir/$WALG_ASSET.sha256")
  actual=$(sha256sum "$dir/$WALG_ASSET" | awk '{print $1}')
  [ "$actual" = "$published" ] || fail "$WALG_ASSET: SHA-256 $actual, published $published"
  if [ -n "$WALG_SHA256" ] && [ "$published" != "$WALG_SHA256" ]; then
    fail "$WALG_ASSET: published SHA-256 $published differs from the pinned $WALG_SHA256"
  fi
  mkdir "$dir/unpacked"
  tar -xzf "$dir/$WALG_ASSET" -C "$dir/unpacked"
  binary=$(find "$dir/unpacked" -type f -name 'wal-g*' | head -n 1)
  [ -n "$binary" ] || fail "$WALG_ASSET holds no wal-g binary"
  install -m 0755 "$binary" "$WALG_BIN"
  note "wal-g: $WALG_VERSION release asset $WALG_ASSET, SHA-256 $actual" \
    "(matches the published .sha256), installed as $WALG_BIN ($(date -u +%FT%TZ))"
fi

echo "pg_probackup: $PROBACKUP: $("$PROBACKUP" --version)"
echo "wal-g: $WALG_BIN: $("$WALG_BIN" --version | head -n 1)"
echo "provenance ($PROVENANCE):"
cat "$PROVENANCE"
