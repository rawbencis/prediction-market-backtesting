# sourced by the run_* scripts: block until it is safe to start the next heavy job
wait_ok() {
  while :; do
    hm=$(date -u +%H%M); busy=0
    for s in breakout-eval momentum-eval rd3-eval ohioai-match; do [ "$(systemctl is-active $s)" = active ] && busy=1; done
    if [ "$hm" -ge 0012 ] && [ "$hm" -lt 0110 ]; then busy=1; fi
    [ $busy = 0 ] && return
    sleep 30
  done
}
