#!/bin/sh
# Press the demo's keys for the one-minute recording, each when the page says it is ready for it.
# Start scripts/demo.sh first, put the page full screen, then run this and start recording at the countdown.
set -u
base="${STOPLINE_URL:-http://127.0.0.1:8785}"
key() { curl -s -m 30 -X POST "$base/api/key/$1" >/dev/null || echo "  key $1 did not answer"; printf '%s  key %s\n' "$(date +%H:%M:%S)" "$1"; }
ready() {   # wait until the page shows "Ready for <key>", at most $2 seconds
    n=0
    until curl -s -m 5 "$base/api/state" | grep -q "\"ready_key\": *\"$1\""; do
        n=$((n + 1)); [ "$n" -ge $(( ${2:-20} * 2 )) ] && { echo "  (not ready for $1 after ${2:-20}s, pressing anyway)"; return; }
        sleep 0.5
    done
}

key 1                      # the baseline run starts before the recording
ready S 30
echo "start recording in:"; for n in 5 4 3 2 1; do echo "  $n"; sleep 1; done
echo "0:00  recording"
sleep 7;  key S            # about 0:07  stop; the restart lands after it
sleep 8;  key 2            # about 0:15  StopLine run
ready K 20; key K          # about 0:26  cut the router, kill worker two
ready S 10; key S          # stop
ready W 10; key W          # resume worker two; refused
sleep 9;  ready R 5; key R # the router is back; the receipt closes
sleep 6;  key M            # the numbers
sleep 8;  echo "end"
