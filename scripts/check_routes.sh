#!/usr/bin/env bash
# Load every route in a headless browser and fail on a console error or an empty page.
#
# A build succeeding proves the modules parse, not that they run: a missing import is a clean
# build and a blank screen. This shipped exactly that once - an import rewrite dropped the api
# helpers, the page it broke was not the page that got screenshotted, and the deployed site was
# a white rectangle until someone said so.
#
# Usage: scripts/check_routes.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8000}"
CHROME="${CHROME:-/c/Program Files/Google/Chrome/Application/chrome.exe}"
ROUTES=("" "#/app/command" "#/app/incidents" "#/app/incidents/INC-0001" "#/app/simulate" "#/app/health" "#/app/proof")
fail=0
for route in "${ROUTES[@]}"; do
  out=$("$CHROME" --headless=new --disable-gpu --no-sandbox --enable-logging=stderr --v=0 \
        --virtual-time-budget=9000 --dump-dom "$BASE/$route" 2>&1)
  errs=$(echo "$out" | grep -icE 'CONSOLE.*(ReferenceError|TypeError|Uncaught)')
  rendered=$(echo "$out" | grep -c 'class="\(shell\|lp\)')
  status=ok
  if [ "$errs" != "0" ] || [ "$rendered" = "0" ]; then status=FAIL; fail=1; fi
  printf '%-18s console_errors=%s rendered=%s  %s\n' "${route:-/}" "$errs" "$rendered" "$status"
done
[ $fail -eq 0 ] && echo "all routes ok" || echo "ROUTE CHECK FAILED"
exit $fail
