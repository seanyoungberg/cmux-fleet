#!/usr/bin/env bash
# drive-child.sh <surface-uuid> <prompt...> - reliably submit a prompt to a claude TUI.
# `cmux send` with a trailing \n only TYPES into the input (the TUI treats the newline as a line
# break, not a submit). So: send the text, THEN a separate `send-key enter` to actually submit.
CMUX="${CMUX_BIN:-$(command -v cmux || echo /Applications/cmux.app/Contents/Resources/bin/cmux)}"; export CMUX_QUIET=1
SURF="$1"; shift; TEXT="$*"
"$CMUX" send --surface "$SURF" "$TEXT" >/dev/null 2>&1
"$CMUX" send-key --surface "$SURF" enter >/dev/null 2>&1
echo "[drive] submitted to ${SURF:0:8}"
