#!/usr/bin/env bash
# test-setup.sh — fresh throwaway PROTOX_DATA_DIR + PROTOX_WORKSPACE for manual
# use-case runs (the test-uc-*.sh scripts create their own when these are unset).
#
#   source scripts/test-setup.sh          # vars set in the current shell
#   eval "$(scripts/test-setup.sh)"       # same, from a non-bash shell
set -euo pipefail

PROTOX_DATA_DIR="$(mktemp -d /tmp/protox-test-data.XXXXXX)"
PROTOX_WORKSPACE="$(mktemp -d /tmp/protox-test-ws.XXXXXX)"

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  # Executed: print the export block so the caller can eval/source it.
  printf 'export PROTOX_DATA_DIR=%q\n' "$PROTOX_DATA_DIR"
  printf 'export PROTOX_WORKSPACE=%q\n' "$PROTOX_WORKSPACE"
else
  # Sourced: set the vars in this shell.
  export PROTOX_DATA_DIR PROTOX_WORKSPACE
  printf 'test-setup: PROTOX_DATA_DIR=%s\n' "$PROTOX_DATA_DIR" >&2
  printf 'test-setup: PROTOX_WORKSPACE=%s\n' "$PROTOX_WORKSPACE" >&2
fi
