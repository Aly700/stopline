#!/bin/sh
# Press the demo's keys on a timer, for recording the one-minute video.
# Start scripts/demo.sh first, put the page full screen, then run this and start recording at the countdown.
set -eu
base="${STOPLINE_URL:-http://127.0.0.1:8785}"
key() { curl -s -m 20 -X POST "$base/api/key/$1" >/dev/null; printf '%s  key %s\n' "$(date +%H:%M:%S)" "$1"; }

key 1                      # baseline run starts before the recording
sleep 10
echo "start recording in:"; for n in 5 4 3 2 1; do echo "  $n"; sleep 1; done
echo "0:00  recording"
sleep 7;  key S            # 0:07  stop; the restart lands after it
sleep 8;  key 2            # 0:15  StopLine run
sleep 11; key K            # 0:26  cut the router, kill worker two
sleep 3;  key S            # 0:29  stop
sleep 4;  key W            # 0:33  resume worker two; refused
sleep 10; key R            # 0:43  router back; receipt closes
sleep 6;  key M            # 0:49  the numbers
sleep 8;  echo "0:57  end"
