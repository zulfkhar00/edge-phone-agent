#!/bin/bash
# Keep every configured phone reachable over adb, one watcher per phone ([[phones]] in config.toml, or the old [phone]).
# The USB cable is the main link (the engine prefers it: usb_serial, else hw_serial); Wi-Fi adb is the backup, kept alive here.
# With the cable in: if Wi-Fi adb is off (after a phone reboot tcpip mode resets), switch it back on over the cable
#   (adb tcpip 5555, only while the engine is idle on THAT phone: it restarts adbd) and record the phone's Wi-Fi address.
# With the cable out, Wi-Fi recovery as before.
# The Wi-Fi address lives in config.toml (wifi_serial of the phone's [[phones]] entry, or [phone].serial, "ip:5555");
# the engine re-reads it, no restart needed. A phone added to config.toml needs a keeper restart.
# When a phone is not there over Wi-Fi:
#   1. reconnect to the same address (a short Wi-Fi drop);
#   2. find the phone on the local network by its hardware serial (hw_serial), checking every host
#      with port 5555 open (a new network or a new DHCP address), then write the new address into config.toml;
#   3. Wi-Fi band changes and reboots switch off both Wireless debugging and tcpip mode. MacroDroid on the phone
#      turns Wireless debugging back on; when its mDNS service shows up, connect to it and switch adbd back to
#      tcpip 5555.
# A device found on the network only counts when it reports this phone's hw_serial, so two phones never swap addresses.
# Every adb call is time-limited so a phone that drops mid-command can't hang this loop.
# Logs (logs/adb_keeper.log), every line tagged with the phone id: when a phone stops answering and why, progress
# every ~5 min while it is missing, and when and how it came back. One phone being away never stalls the others.
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python)"
log() { echo "$(date '+%F %T') [$ID] $*"; }
port() { timeout 2 bash -c "echo > /dev/tcp/${1%%:*}/${1##*:}" 2>/dev/null && echo open || echo closed; }
if command -v timeout >/dev/null; then t() { timeout 20 "$@"; }; else t() { perl -e 'alarm shift; exec @ARGV' 20 "$@"; }; fi  # Linux | macOS

wifi_cfg() { "$PY" phones.py wifi "$ID" 2>/dev/null; }
is_phone() {  # the device at $1 is THIS phone (an empty hw_serial in an old config accepts anything)
  [ -z "$HW" ] && return 0
  [ "$(t adb -s "$1" shell getprop ro.serialno 2>/dev/null | tr -d '\r')" = "$HW" ]
}

use() {  # the phone answers at $1 (ip:5555): make it the configured address
  [ "$1" = "$TCP" ] && return
  "$PY" phones.py set-wifi "$ID" "$1" || return
  log "phone Wi-Fi address $TCP -> $1"
  TCP="$1"
}

usb_state() { t adb -s "$USBS" get-state 2>&1 | grep -oE "device|unauthorized|offline" | head -1; }
engine_busy() {  # a job is driving this phone, or someone took over from the dashboard: don't restart adbd
  [ "$(sqlite3 engine.db "select count(*) from jobs where status='running' and (phone_id='$ID' or phone_id is null)" 2>/dev/null)" != "0" ] && return 0
  sqlite3 engine.db "select v from state where k='paused:$ID'" 2>/dev/null | grep -q true
}
wifi_over_usb() {  # cable in, Wi-Fi adb off: turn it back on over the cable
  local ip
  ip="$(t adb -s "$USBS" shell ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
  [ -z "$ip" ] && return 1  # phone not on Wi-Fi
  if [ "$(port "$ip:5555")" = closed ]; then
    engine_busy && return 1
    log "Wi-Fi adb off at $ip: switching it on over USB (adb tcpip 5555)"
    t adb -s "$USBS" tcpip 5555 >/dev/null 2>&1; sleep 5
    t adb -s "$USBS" wait-for-device >/dev/null 2>&1
  fi
  t adb connect "$ip:5555" >/dev/null 2>&1
  is_phone "$ip:5555" && use "$ip:5555"
}

scan() {  # every host on our IPv4 subnets with port 5555 open that is this phone
  for net in $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | grep '/24$' | cut -d. -f1-3); do
    for i in $(seq 1 254); do
      (timeout 1 bash -c "echo > /dev/tcp/$net.$i/5555" 2>/dev/null && echo "$net.$i:5555") &
    done
    wait
  done | while read -r a; do
    t adb connect "$a" >/dev/null 2>&1
    if is_phone "$a"; then echo "$a"; break; fi
    t adb disconnect "$a" >/dev/null 2>&1
  done
}

back() {  # $1: how it came back
  [ "$down" -gt 0 ] && log "phone back at $TCP after $(( $(date +%s) - down ))s ($1)"
  down=0; misses=0
}

keep_phone() {  # $1 id, $2 usb serial, $3 hw serial: runs in its own subshell, so its state is its own
  ID="$1"; USBS="$2"; HW="$3"
  [ -z "$USBS" ] && USBS="$HW"
  log "keeper started; USB ${USBS:--}, hw_serial ${HW:--}, Wi-Fi $(wifi_cfg)"
  misses=0; down=0; usb=""; usb_try=0  # down: when the phone stopped answering (0 = it answers); blips fixed by one reconnect are not logged
  while true; do
    TCP="$(wifi_cfg)"
    U="$(usb_state)"; U="${U:-missing}"
    [ "$U" != "$usb" ] && { log "USB: $U$([ "$U" = unauthorized ] && echo ' (approve "Allow USB debugging" on the phone)')"; usb="$U"; }
    if [ "$U" = device ]; then
      # the cable works: just keep the Wi-Fi backup up (at most every 2 min; no subnet scans needed)
      if [ -z "$TCP" ] || ! t adb -s "$TCP" get-state 2>/dev/null | grep -q device && [ $(( $(date +%s) - usb_try )) -ge 120 ]; then
        usb_try=$(date +%s)
        wifi_over_usb && back "Wi-Fi backup restored over USB"
      elif t adb -s "$TCP" get-state 2>/dev/null | grep -q device; then
        back "answered again"
      fi
      sleep 15; continue
    fi
    [ -z "$TCP" ] && { sleep 15; continue; }  # cable out and no Wi-Fi address known yet: nothing to recover
    if ! t adb -s "$TCP" get-state 2>/dev/null | grep -q device; then
      t adb disconnect "$TCP" >/dev/null 2>&1
      if t adb connect "$TCP" 2>/dev/null | grep -q "connected to" && is_phone "$TCP"; then
        back "reconnected to the same address"
      else
        if [ "$down" -eq 0 ]; then
          down=$(date +%s)
          log "phone $TCP not answering: port 5555 $(port "$TCP"), ping $(ping -c1 -W2 "${TCP%%:*}" >/dev/null 2>&1 && echo ok || echo fails)"
        fi
        P=""
        [ -n "$HW" ] && P=$(t adb mdns services | awk -v hw="adb-$HW" '$0 ~ hw && /tls-connect/ {print $NF; exit}')
        if [ -n "$P" ]; then
          t adb connect "$P" >/dev/null 2>&1; sleep 2
          t adb -s "$P" tcpip 5555 >/dev/null 2>&1; sleep 4
          NEW="${P%%:*}:5555"
          t adb connect "$NEW" >/dev/null 2>&1
          log "Wireless debugging found at $P: switched adbd back to $NEW"
          is_phone "$NEW" && { use "$NEW"; TCP="$NEW"; back "via Wireless debugging"; }
        else
          misses=$((misses + 1))
          if [ $((misses % 4)) -eq 1 ] && [ -n "$HW" ]; then  # a subnet scan about once a minute while the phone is missing
            NEW="$(scan)"
            [ -n "$NEW" ] && { use "$NEW"; TCP="$NEW"; back "found by subnet scan"; }
          fi
          [ "$down" -gt 0 ] && [ $((misses % 20)) -eq 0 ] && log "still missing after $(( $(date +%s) - down ))s: port 5555 $(port "$TCP"), Wireless debugging not advertised, not found on the subnet"
        fi
      fi
    else
      back "answered again"
    fi
    sleep 15
  done
}

# one watcher per phone: a phone that is away, unauthorised or being scanned never delays another one
LIST="$("$PY" phones.py keeper-list)" || { echo "adb keeper: cannot read the phones from config.toml" >&2; exit 1; }
echo "$LIST" | {
  while IFS='|' read -r id usb hw wifi; do
    [ -n "$id" ] && ( keep_phone "$id" "$usb" "$hw" ) &
  done
  wait
}
