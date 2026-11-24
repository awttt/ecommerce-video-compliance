#!/usr/bin/env bash
# Stage or upload the Windows installer.
#
# Upload protocol (the download server is write-only and holds one lock):
#   1. VideoCompliance-X.Y.Z-Setup.exe
#   2. VideoCompliance-X.Y.Z-Setup.manifest
#   3. latest.manifest (stable name the desktop client polls)
# The files are sent one after another, both to ":/". Do not pass --delete
# or --remove-source-files, and do not change the remote path.
#
# Secrets (all required, or the upload is skipped):
#   DEPLOY_SSH_HOST, DEPLOY_SSH_USER, DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS
#
# Usage:
#   upload_installer.sh stage VERSION ARTIFACT_DIR OUT_DIR
#   upload_installer.sh upload VERSION ARTIFACT_DIR
set -euo pipefail
export LC_ALL=C

DOWNLOAD_BASE="https://token4u.cc/download"

die() {
  # stderr stays visible when a helper runs inside $(...).
  printf '%s\n' "::error::$*" >&2
  exit 1
}

note() {
  printf '%s\n' "$*"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
  fi
}

version_ok() {
  [[ "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
}

reject_parent_segment() {
  local label=$1 value=$2
  case "$value" in
    *..*)
      die "${label} must not contain a parent-directory segment"
      ;;
  esac
}

find_installer() {
  local version=$1 dir=$2
  local expected="VideoCompliance-${version}-Setup.exe"
  local -a found=()
  local path

  if [[ ! -d "$dir" ]]; then
    die "artifact directory ${dir} does not exist"
  fi
  while IFS= read -r -d '' path; do
    found+=("$path")
  done < <(find "$dir" -type f -name "$expected" -print0)
  if [[ ${#found[@]} -ne 1 ]]; then
    die "expected one ${expected} under ${dir}, found ${#found[@]}"
  fi
  printf '%s\n' "${found[0]}"
}

file_sha256() {
  local file=$1
  local hash
  hash=$(sha256sum -- "$file" | awk 'NR==1 { print tolower($1) }')
  if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
    die "sha256 of ${file} was not 64 lowercase hex digits"
  fi
  printf '%s\n' "$hash"
}

body_matches() {
  local body=$1 want=$2
  local lower line first
  lower=${body,,}
  want=${want,,}
  local compact
  compact=$(printf '%s' "$lower" | tr -d '[:space:]')
  if [[ "$compact" == "$want" || "$compact" == "sha256=${want}" ]]; then
    return 0
  fi
  line=$(printf '%s\n' "$lower" | awk 'NF { print; exit }')
  # shellcheck disable=SC2295
  first=${line%%[[:space:]]*}
  if [[ "$first" == "$want" || "$first" == "sha256=${want}" ]]; then
    return 0
  fi
  if printf '%s\n' "$lower" | grep -Fxq "sha256=${want}"; then
    return 0
  fi
  return 1
}

manifest_matches() {
  local body=$1
  printf '%s\n' "$body" | grep -Fxq "version=${version}" \
    && printf '%s\n' "$body" | grep -Fxq "sha256=${hash}" \
    && printf '%s\n' "$body" | grep -Fxq "file=${expected}"
}

http_get() {
  local url=$1 dest=$2
  local code
  code=$(curl --silent --show-error --max-time 20 --max-filesize 4096 \
    --output "$dest" --write-out "%{http_code}" -- "$url" || true)
  if [[ ! "$code" =~ ^[0-9]{3}$ ]]; then
    code="000"
  fi
  printf '%s' "$code"
}

classify_remote() {
  local url=$1 want=$2
  local tmp code body
  tmp=$(mktemp)
  code=$(http_get "$url" "$tmp")
  body=$(cat "$tmp" 2>/dev/null || true)
  rm -f "$tmp"
  REMOTE_CODE=$code
  REMOTE_STATE="unknown"
  REMOTE_SNIPPET=$(printf '%s' "$body" | tr '\r\n' '  ' | cut -c1-160)
  REMOTE_SNIPPET=${REMOTE_SNIPPET//::/}
  REMOTE_SNIPPET=${REMOTE_SNIPPET//%/}
  if [[ "$code" == "404" ]]; then
    REMOTE_STATE="absent"
    return 0
  fi
  if [[ "$code" == "200" ]] && body_matches "$body" "$want"; then
    REMOTE_STATE="same"
    return 0
  fi
  if [[ "$code" == "200" ]]; then
    local hex found_other=""
    while IFS= read -r hex; do
      hex=${hex,,}
      if [[ "$hex" != "$want" ]]; then
        found_other=1
        break
      fi
    done < <(printf '%s' "$body" | grep -oE '[0-9a-fA-F]{64}' || true)
    if [[ -n "$found_other" ]]; then
      REMOTE_STATE="different"
      return 0
    fi
  fi
  REMOTE_STATE="unknown"
}

write_secret_file() {
  local dest=$1 content=$2
  local previous
  previous=$(umask)
  umask 077
  printf '%s' "$content" > "$dest"
  if [[ "$content" != *$'\n' ]]; then
    printf '\n' >> "$dest"
  fi
  chmod 600 "$dest"
  umask "$previous"
  local mode
  mode=$(stat -c '%a' "$dest")
  if [[ "$mode" != "600" ]]; then
    die "refusing to continue: ${dest} mode is ${mode}, expected 600"
  fi
}

require_version() {
  if ! version_ok "$1"; then
    die "version ${1} must be X.Y.Z with no leading zeros"
  fi
  reject_parent_segment "version" "$1"
}

cmd_stage() {
  local version=$1 dir=$2 out=$3
  local src expected
  require_version "$version"
  reject_parent_segment "output directory" "$out"
  src=$(find_installer "$version" "$dir") || exit 1
  expected="VideoCompliance-${version}-Setup.exe"
  mkdir -p "$out"
  cp -- "$src" "$out/$expected"
  (
    cd "$out"
    sha256sum -- "$expected" > "${expected}.sha256"
    sha256sum -c "${expected}.sha256" >/dev/null
  )
  echo "Staged ${out}/${expected}"
}

poll_seconds() {
  local seconds=300
  # Tests may set a shorter wait. Values at or above five minutes are ignored,
  # so a real run cannot be told to poll longer than that.
  if [[ "${DOWNLOAD_POLL_SECONDS:-}" =~ ^[0-9]+$ ]]; then
    local requested=$((10#${DOWNLOAD_POLL_SECONDS}))
    if ((requested < 300)); then
      seconds=$requested
    fi
  fi
  printf '%s' "$seconds"
}

cmd_upload() {
  local version=$1 dir=$2
  local src expected hash manifest url remote key hosts host user
  local missing=() state attempt deadline remain poll

  require_version "$version"
  expected="VideoCompliance-${version}-Setup.exe"
  local expected_ref="refs/tags/v${version}"
  if [[ "${GITHUB_REF:-}" != "$expected_ref" ]]; then
    die "refusing to upload: GITHUB_REF must be ${expected_ref} (v + __version__), got '${GITHUB_REF:-}'"
  fi
  src=$(find_installer "$version" "$dir") || exit 1

  work=$(mktemp -d)
  cleanup() {
    if [[ -n "${work:-}" ]]; then
      rm -rf "$work"
    fi
    rm -f "$HOME/.ssh/ecv_deploy" "$HOME/.ssh/known_hosts_ecv"
  }
  trap cleanup EXIT

  cp -- "$src" "$work/$expected"
  hash=$(file_sha256 "$work/$expected")
  manifest="VideoCompliance-${version}-Setup.manifest"
  printf 'version=%s\nsha256=%s\n' "$version" "$hash" > "$work/$manifest"
  echo "Local sha256 ${hash}"

  key=${DEPLOY_SSH_KEY-}
  hosts=${DEPLOY_KNOWN_HOSTS-}
  host=${DEPLOY_SSH_HOST-}
  user=${DEPLOY_SSH_USER-}
  key=${key//$'\r'/}
  hosts=${hosts//$'\r'/}
  host=${host//$'\r'/}
  user=${user//$'\r'/}

  [[ -z "${user//[[:space:]]/}" ]] && missing+=("DEPLOY_SSH_USER")
  [[ -z "${host//[[:space:]]/}" ]] && missing+=("DEPLOY_SSH_HOST")
  [[ -z "${key//[[:space:]]/}" ]] && missing+=("DEPLOY_SSH_KEY")
  [[ -z "${hosts//[[:space:]]/}" ]] && missing+=("DEPLOY_KNOWN_HOSTS")
  if [[ ${#missing[@]} -gt 0 ]]; then
    local joined=""
    local name
    for name in "${missing[@]}"; do
      joined+="${name}, "
    done
    joined=${joined%, }
    echo "::notice title=Download upload skipped::Missing secrets: ${joined}. The installer was not uploaded."
    note "Download upload skipped. Missing secrets: ${joined}."
    note "Add them under Settings, Secrets and variables, Actions, then re-run this workflow. The GitHub Release is still published."
    exit 0
  fi

  if [[ "$user" =~ [[:space:]] || ! "$user" =~ ^[A-Za-z0-9._-]+$ || "$user" == -* ]]; then
    die "DEPLOY_SSH_USER must be a single username"
  fi
  if [[ "$host" =~ [[:space:]] || ! "$host" =~ ^[A-Za-z0-9.-]+$ || "$host" == -* || "$host" == .* || "$host" == *. ]]; then
    die "DEPLOY_SSH_HOST must be a hostname or IPv4 address"
  fi
  reject_parent_segment "DEPLOY_SSH_USER" "$user"
  reject_parent_segment "DEPLOY_SSH_HOST" "$host"

  remote="${user}@${host}:/"
  if [[ "$remote" != "${user}@${host}:/" ]]; then
    die "refusing remote destination"
  fi
  case "$remote" in
    *" "*|*"'"*)
      die "refusing remote destination"
      ;;
  esac

  command -v curl >/dev/null || die "curl is not installed"
  url="${DOWNLOAD_BASE}/${expected}.sha256"
  state=""
  REMOTE_CODE=""
  for attempt in 1 2 3; do
    classify_remote "$url" "$hash"
    state=$REMOTE_STATE
    case "$state" in
      same | absent | different) break ;;
    esac
    sleep 5
  done
  skip_payload=0
  case "$state" in
    same)
      note "Download server already has ${expected} with the same sha256. Refreshing latest.manifest only."
      skip_payload=1
      ;;
    absent) ;;
    different)
      die "version ${version} is already published with a different sha256. Version numbers cannot be reused, because a cache sits in front of the download. Bump __version__ and tag the new version. Last response: ${REMOTE_SNIPPET}"
      ;;
    *)
      die "could not read ${url} before upload (last HTTP ${REMOTE_CODE:-000}). The installer was not uploaded."
      ;;
  esac

  command -v rsync >/dev/null || die "rsync is not installed"
  command -v ssh >/dev/null || die "ssh is not installed"

  umask 077
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  # Private key and known_hosts are created with umask 077 and never printed.
  write_secret_file "$HOME/.ssh/ecv_deploy" "$key"
  write_secret_file "$HOME/.ssh/known_hosts_ecv" "$hosts"
  unset key hosts

  # shellcheck disable=SC2088
  SSH="ssh -i ~/.ssh/ecv_deploy -o UserKnownHostsFile=~/.ssh/known_hosts_ecv -o StrictHostKeyChecking=yes -o BatchMode=yes -o IdentitiesOnly=yes"
  printf 'version=%s\nfile=%s\nsha256=%s\n' "$version" "$expected" "$hash" > "$work/latest.manifest"
  echo "Remote path is :/"
  if [[ "$skip_payload" -eq 0 ]]; then
    echo "Uploading ${expected}"
    rsync -t --chmod=F644 -e "$SSH" "$work/$expected" "$remote"
    echo "Uploading ${manifest}"
    rsync -t --chmod=F644 -e "$SSH" "$work/$manifest" "$remote"

    poll=$(poll_seconds)
    deadline=$((SECONDS + poll))
    attempt=0
    while true; do
      attempt=$((attempt + 1))
      classify_remote "$url" "$hash"
      if [[ "$REMOTE_STATE" == "same" ]]; then
        note "Published sha256 matches ${url}"
        break
      fi
      echo "Published sha256 not ready (attempt ${attempt}, HTTP ${REMOTE_CODE}, ${REMOTE_STATE})."
      if ((SECONDS >= deadline)); then
        die "sha256 at ${url} did not match within ${poll} seconds (last HTTP ${REMOTE_CODE})."
      fi
      remain=$((deadline - SECONDS))
      if ((remain > 10)); then
        sleep 10
      elif ((remain > 0)); then
        sleep "$remain"
      fi
    done
  fi

  echo "Uploading latest.manifest"
  rsync -t --chmod=F644 -e "$SSH" "$work/latest.manifest" "$remote"
  poll=$(poll_seconds)
  deadline=$((SECONDS + poll))
  attempt=0
  while true; do
    attempt=$((attempt + 1))
    latest_tmp=$(mktemp)
    latest_code=$(http_get "${DOWNLOAD_BASE}/latest.manifest" "$latest_tmp")
    latest_body=$(cat "$latest_tmp" 2>/dev/null || true)
    rm -f "$latest_tmp"
    if [[ "$latest_code" == "200" ]] && manifest_matches "$latest_body"; then
      note "Published latest.manifest for ${version}"
      exit 0
    fi
    echo "latest.manifest not ready (attempt ${attempt}, HTTP ${latest_code})."
    if ((SECONDS >= deadline)); then
      die "latest.manifest did not match within ${poll} seconds (last HTTP ${latest_code})."
    fi
    remain=$((deadline - SECONDS))
    if ((remain > 10)); then
      sleep 10
    elif ((remain > 0)); then
      sleep "$remain"
    fi
  done
}

main() {
  local cmd=${1:-}
  case "$cmd" in
    stage)
      [[ $# -eq 4 ]] || die "usage: upload_installer.sh stage VERSION ARTIFACT_DIR OUT_DIR"
      cmd_stage "$2" "$3" "$4"
      ;;
    upload)
      [[ $# -eq 3 ]] || die "usage: upload_installer.sh upload VERSION ARTIFACT_DIR"
      cmd_upload "$2" "$3"
      ;;
    *)
      echo "usage: upload_installer.sh stage VERSION ARTIFACT_DIR OUT_DIR" >&2
      echo "       upload_installer.sh upload VERSION ARTIFACT_DIR" >&2
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
