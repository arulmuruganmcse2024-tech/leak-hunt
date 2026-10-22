#!/bin/sh
# usage: devserver.sh start <port> <speed> <dbfile> | stop <dbfile>
# (start writes <dbfile>.pid and <dbfile>.log; stop reads the pid file, so it never matches processes by name)
SP="$(cd "$(dirname "$0")/.." && pwd)"
case "$1" in
  start)
    rm -f "$4" "$4.power"
    LEAKHUNT_DEVHOST=1 LEAKHUNT_SPEED="$3" LEAKHUNT_DB="$4" LEAKHUNT_PUBLIC_URL="http://127.0.0.1:$2" LEAKHUNT_KEY=devkey PORT="$2" \
      nohup python3 "$SP/serve.py" > "$4.log" 2>&1 &
    echo $! > "$4.pid"; sleep 2; curl -s "http://127.0.0.1:$2/healthz"; echo
    # The simulated meter's synthetic history and its live readings meet in the first two meter intervals (20 simulated
    # seconds); a fill asked for then is refused with "your water meter has just restarted". Wait until that has passed.
    sleep "$(awk "BEGIN { print 1 + 25 / $3 }")" ;;
  stop)
    kill "$(cat "$2.pid")" 2>/dev/null; echo stopped ;;
esac
