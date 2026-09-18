#!/usr/bin/env bash
# The Agent SDK's cli_path points here when OFFICE_SANDBOX=docker. It is a
# transparent stand-in for the `claude` binary: same arguments, same stdio,
# but the process runs in a container that holds only what this employee's
# role needs.
#
#   OFFICE_ROLE           who is running (label only)
#   OFFICE_SANDBOX_RW     1 = workspace mounted read-write (role has Write/Edit)
#   OFFICE_SANDBOX_NET    none | bridge   (bridge only for roles that fetch)
#   OFFICE_SANDBOX_IMAGE  image name (default digital-office-agent:latest)
#   OFFICE_WORKSPACE      host path mounted at /workspace
#   OFFICE_SANDBOX_DRYRUN 1 = print the docker command and exit 0
#
# Secrets: only the model credential crosses into the container. Anything a
# role needs beyond that is a deliberate extra -e in OFFICE_SANDBOX_EXTRA_ENV
# (comma-separated names), never the whole environment.
set -euo pipefail

image="${OFFICE_SANDBOX_IMAGE:-digital-office-agent:latest}"
workspace="${OFFICE_WORKSPACE:-$PWD}"
mode="ro"; [ "${OFFICE_SANDBOX_RW:-0}" = "1" ] && mode="rw"
net="${OFFICE_SANDBOX_NET:-none}"
name="office-${OFFICE_ROLE:-agent}-$$"

args=(docker run --rm -i --name "$name"
      --network "$net"
      --cap-drop ALL --security-opt no-new-privileges
      --pids-limit 256 --memory "${OFFICE_SANDBOX_MEMORY:-1g}"
      --read-only --tmpfs /tmp:rw,size=256m --tmpfs /home/agent:rw,size=64m
      -v "$workspace:/workspace:$mode"
      -w /workspace
      -e HOME=/home/agent -e TERM=dumb -e LANG=C.UTF-8
      -e "OFFICE_ROLE=${OFFICE_ROLE:-agent}")

# The model credential is the one secret that must cross. Never both blindly:
# pass whichever the office is using.
for var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
           ANTHROPIC_BASE_URL CLAUDE_CONFIG_DIR HTTPS_PROXY HTTP_PROXY NO_PROXY; do
  if [ -n "${!var:-}" ]; then args+=(-e "$var"); fi
done
if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -d "${CLAUDE_CONFIG_DIR}" ]; then
  args+=(-v "${CLAUDE_CONFIG_DIR}:${CLAUDE_CONFIG_DIR}:rw")
fi
extra=()
if [ -n "${OFFICE_SANDBOX_EXTRA_ENV:-}" ]; then
  IFS=',' read -r -a extra <<< "${OFFICE_SANDBOX_EXTRA_ENV}"
fi
for var in ${extra[@]+"${extra[@]}"}; do
  var="${var// /}"
  [ -n "$var" ] && [ -n "${!var:-}" ] && args+=(-e "$var")
done

args+=("$image" "$@")

if [ "${OFFICE_SANDBOX_DRYRUN:-0}" = "1" ]; then
  printf '%q ' "${args[@]}"; echo
  exit 0
fi
exec "${args[@]}"
