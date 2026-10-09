#!/bin/bash
# flowcast training instance bootstrap (EC2 user data, first boot only).
# Installs /opt/flowcast/job.sh as a systemd service that runs on every boot, so a Spot instance that was
# stopped by an interruption resumes its run when EC2 starts it again, and /opt/flowcast/guard.sh on a timer in
# its own unit, so a job that dies or stalls can't leave a paid instance idle.
set -euo pipefail
mkdir -p /opt/flowcast
cat > /opt/flowcast/env <<'FLOWCAST_ENV'
#__FLOWCAST_ENV__
FLOWCAST_ENV

cat > /opt/flowcast/lib.sh <<'FLOWCAST_LIB'
source /opt/flowcast/env
export HOME=/root AWS_DEFAULT_REGION="$REGION" PATH="/root/.local/bin:/usr/local/bin:$PATH" PYTHONUNBUFFERED=1
LOG=/opt/flowcast/job.log
RUN_S3="s3://$BUCKET/runs/$RUN_ID"
RUN_DIR="/opt/flowcast/runs/$RUN_ID"
imds() { curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')" "http://169.254.169.254/latest/meta-data/$1"; }
INSTANCE_ID=$(imds instance-id)
BOOT=$(cat /opt/flowcast/boots 2>/dev/null || echo 0)

status() {
  local detail=${2:-}
  detail=${detail//\\/\\\\}
  printf '{"status": "%s", "detail": "%s", "time": "%s", "instance": "%s", "boot": %s}\n' "$1" "${detail//\"/\'}" "$(date -u +%FT%TZ)" "$INSTANCE_ID" "$BOOT" > /opt/flowcast/status.json
  aws s3 cp /opt/flowcast/status.json "$RUN_S3/status.json" --only-show-errors || true
}
status_of() { python3 -c "import json;print(json.load(open('/opt/flowcast/status.json'))['status'])" 2>/dev/null; }
upload_logs() {
  aws s3 cp "$LOG" "$RUN_S3/logs/job.log" --only-show-errors || true
  [ -f /opt/flowcast/heartbeat.jsonl ] && aws s3 cp /opt/flowcast/heartbeat.jsonl "$RUN_S3/heartbeat.jsonl" --only-show-errors || true
}
sync_run() { [ -d "$RUN_DIR" ] && aws s3 sync "$RUN_DIR" "$RUN_S3/run/" --only-show-errors --exclude 'generic/*' || true; }
interrupted() {
  # a Spot interruption or any system shutdown: leave the instance alone so it can resume
  [ -n "$(imds spot/instance-action 2>/dev/null)" ] && return 0
  case "$(systemctl is-system-running 2>/dev/null)" in stopping) return 0 ;; esac
  return 1
}
terminate_self() {
  upload_logs
  local sir
  sir=$(aws ec2 --region "$REGION" describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].SpotInstanceRequestId' --output text 2>/dev/null)
  if [ -n "$sir" ] && [ "$sir" != "None" ]; then aws ec2 --region "$REGION" cancel-spot-instance-requests --spot-instance-request-ids "$sir" || true; fi
  aws ec2 --region "$REGION" terminate-instances --instance-ids "$INSTANCE_ID" || shutdown -h now
  sleep 600
  exit 0
}
fail() {
  if interrupted; then echo "interrupted during: $1"; upload_logs; exit 0; fi
  echo "FAILED: $1"; status failed "$1"; sync_run; terminate_self
}
# The latest kernel OOM kill of this boot, e.g. "2026-09-27T18:26:03 Out of memory: Killed process 3142 (pt_data_worker)".
kernel_oom() {
  journalctl -k -b -o short-iso --no-pager 2>/dev/null | grep 'Out of memory: Killed process' | tail -1 \
    | sed -E 's/^([0-9T:-]+)[^ ]* .*(Out of memory: Killed process [0-9]+ \([^)]*\)).*/\1 \2/'
}
FLOWCAST_LIB

cat > /opt/flowcast/job.sh <<'FLOWCAST_JOB'
#!/bin/bash
set -uo pipefail
source /opt/flowcast/lib.sh
exec >>"$LOG" 2>&1
AZ=$(imds placement/availability-zone)
ITYPE=$(imds instance-type)
BOOT=$((BOOT + 1))
echo "$BOOT" > /opt/flowcast/boots
rm -f /opt/flowcast/crash_reason
echo "=== $(date -u +%FT%TZ) boot $BOOT of run $RUN_ID on $INSTANCE_ID ($ITYPE, $AZ)"
# A training or hindcast step that exits abnormally: leave the reason and exit; the guard restarts the job once
# (it resumes from the latest checkpoint), then fails the run and terminates the instance.
crashed() {
  if interrupted; then echo "interrupted during: $1"; upload_logs; exit 0; fi
  local reason="$1 ($(df --output=avail -BG / | tail -1 | tr -d ' ') free on the root volume)"
  echo "$reason" > /opt/flowcast/crash_reason
  echo "CRASHED: $reason"; sync_run; upload_logs; exit 1
}
trap 'echo "SIGTERM (job stopping)"; upload_logs; exit 0' TERM

if [ "$BOOT" -gt "$MAX_BOOTS" ]; then fail "exceeded $MAX_BOOTS boots"; fi
if [ "$(date +%s)" -ge "$DEADLINE_EPOCH" ]; then status timeout "booted after deadline"; sync_run; terminate_self; fi

# Watchdog: heartbeat for cost accounting, periodic log upload, Spot notice sync, graceful stop, hard deadline.
(
  n=0
  while true; do
    now=$(date +%s)
    gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    printf '{"time": "%s", "boot": %s, "instance": "%s", "az": "%s", "type": "%s", "gpu_util_mem": "%s"}\n' "$(date -u +%FT%T+00:00)" "$BOOT" "$INSTANCE_ID" "$AZ" "$ITYPE" "$gpu" >> /opt/flowcast/heartbeat.jsonl
    if [ -n "$(imds spot/instance-action 2>/dev/null)" ]; then echo "Spot interruption notice"; sync_run; upload_logs; fi
    if [ "$now" -ge $((DEADLINE_EPOCH - 900)) ] && [ -d "$RUN_DIR" ]; then touch "$RUN_DIR/STOP"; fi
    if [ "$now" -ge "$DEADLINE_EPOCH" ]; then echo "hard deadline reached"; status timeout "max runtime reached"; sync_run; terminate_self; fi
    n=$((n + 1)); [ $((n % 5)) -eq 0 ] && upload_logs
    sleep 60
  done
) &

if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "uv install failed"; }; fi
if ! command -v aws >/dev/null; then uv tool install awscli || { echo "aws cli install failed"; shutdown -h now; }; fi
status booting "boot $BOOT"
if [ "$REQUIRE_GPU" = "1" ]; then nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv || fail "no GPU visible"; else nproc; free -g; fi
if [ ! -f /opt/flowcast/code/.ready ]; then
  rm -rf /opt/flowcast/code && mkdir -p /opt/flowcast/code
  aws s3 cp "$CODE_URI" - | tar xz -C /opt/flowcast/code || fail "code download"
  touch /opt/flowcast/code/.ready
fi
cd /opt/flowcast/code/model || fail "no model package"
uv sync --frozen --no-dev --python 3.12 || fail "uv sync"
export UV_NO_SYNC=1
if [ "$REQUIRE_GPU" = "1" ]; then
  uv run python -c "import torch; assert torch.cuda.is_available(), 'CUDA unavailable'; print('torch', torch.__version__, torch.cuda.get_device_name(0))" || fail "torch cannot use the GPU"
fi

# Datasets go to instance NVMe when it is big enough (fast, re-pulled after a stop). Datasets larger than the NVMe
# (DATA_ON_EBS, decided by the launcher) are cached on the EBS root, which the launcher sizes for them and which
# survives Spot stop/start. Datasets are always in this instance's region.
read -r -a DS_URIS <<< "$DATASET_URIS"
DATA=/opt/flowcast/data
# many parallel requests: the default 10 leaves most of the instance's network (10-25 Gbit/s) idle
aws configure set default.s3.max_concurrent_requests 64
aws configure set default.s3.max_queue_size 10000
if [ "${DATA_ON_EBS:-0}" = "0" ] && [ -d /opt/dlami/nvme ] && [ -w /opt/dlami/nvme ]; then DATA=/opt/dlami/nvme/flowcast-data; fi
mkdir -p "$DATA"
CUBES=""
for i in "${!DS_URIS[@]}"; do
  uri="${DS_URIS[$i]}"
  dest="$DATA/cube$i-$(basename "$uri")"
  # a completed copy on the root volume survives a Spot stop/start: skip the (long) sync on later boots
  if [ -f "$dest.complete" ] && [ "$(cat "$dest.complete")" = "$uri" ]; then
    echo "dataset $uri already on disk"
  else
    status staging "syncing $uri"
    aws s3 sync "$uri" "$dest" --region "$REGION" --only-show-errors || fail "dataset sync $uri"
    [ -n "$(ls -A "$dest" 2>/dev/null)" ] || fail "dataset $uri synced nothing (missing or empty)"
    echo "$uri" > "$dest.complete"
  fi
  CUBES="$CUBES $dest"
done

# Swap on the root volume turns short memory peaks (e.g. validation next to the loader workers' caches) into a
# slowdown instead of an OOM kill on 16 GB instances. Created after the dataset sync so it can't crowd it out.
if ! swapon --show | grep -q /swapfile; then
  if [ ! -f /swapfile ] && [ "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" -gt 40 ]; then
    fallocate -l 16G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null || rm -f /swapfile
  fi
  [ -f /swapfile ] && swapon /swapfile && sysctl -qw vm.swappiness=10
fi
df -BG --output=target,size,avail / "$DATA" | sed 's/^/disk /'

mkdir -p "$RUN_DIR"
if [ ! -f "$RUN_DIR/checkpoint.json" ]; then
  # hindcast files too: with flowcast.hindcast.resume a replacement instance skips the sites already done
  aws s3 sync "$RUN_S3/run/" "$RUN_DIR" --only-show-errors --exclude STOP || true
fi
# A STOP from an earlier deadline (synced to S3, or left on a restarted root volume) would end a resumed run after
# one epoch; the watchdog recreates it when this boot's own deadline nears.
rm -f "$RUN_DIR/STOP"
aws s3 cp "$RUN_S3/config.yml" /opt/flowcast/config.yml --only-show-errors || fail "config download"

status training "boot $BOOT"
uv run flowcast-model train --config /opt/flowcast/config.yml --run-dir "$RUN_DIR" --cube $CUBES --sync-to "$RUN_S3/run/" || crashed "training exited with $?"
sync_run

if [ "$(date +%s)" -lt $((DEADLINE_EPOCH - 600)) ]; then
  status hindcast
  uv run flowcast-model hindcast --run-dir "$RUN_DIR" --out "$RUN_DIR/hindcast" || crashed "hindcast exited with $?"
  sync_run
  if [ "$(date +%s)" -lt $((DEADLINE_EPOCH - 600)) ]; then
    status scoring
    uv run flowcast-model score-run --run-dir "$RUN_DIR" || echo "scoring failed with $? (hindcasts are uploaded)"
    sync_run
  fi
  status done "trained to epoch $(python3 -c "import json;print(json.load(open('$RUN_DIR/checkpoint.json'))['epoch'])")"
else
  status done "no time left for the hindcast"
fi
terminate_self
FLOWCAST_JOB

cat > /opt/flowcast/guard.sh <<'FLOWCAST_GUARD'
#!/bin/bash
# Runs every 2 min in its own unit. While the run is active (booting ... scoring), a job that is no longer
# running, or a training/hindcast step with no log output, checkpoint or heartbeat for STALL_MIN minutes, is
# restarted once and then failed: status "failed" with the reason (and any kernel OOM kill), instance terminated.
set -uo pipefail
source /opt/flowcast/lib.sh
exec >>"$LOG" 2>&1
STALL_MIN=${STALL_MIN:-20}
GUARD_RESTARTS=${GUARD_RESTARTS:-1}
state=$(status_of)
case "$state" in booting|staging|training|hindcast|scoring) ;; *) exit 0 ;; esac
interrupted && exit 0
newest() { stat -c %Y "$@" 2>/dev/null | sort -n | tail -1; }
now=$(date +%s)
job=$(systemctl is-active flowcast-train.service)
reason=""
case "$job" in
  active|activating|deactivating)
    if [ "$state" = training ] || [ "$state" = hindcast ]; then
      progress=$(newest "$LOG" "$RUN_DIR/checkpoint.json")
      beat=$(newest /opt/flowcast/heartbeat.jsonl)
      if [ $((now - ${progress:-0})) -gt $((STALL_MIN * 60)) ]; then reason="no log output or checkpoint for $STALL_MIN min during $state"
      elif [ $((now - ${beat:-0})) -gt $((STALL_MIN * 60)) ]; then reason="no heartbeat for $STALL_MIN min during $state"; fi
    fi ;;
  *) reason="job stopped during $state: $(cat /opt/flowcast/crash_reason 2>/dev/null || echo "service $job without an exit status")" ;;
esac
[ -z "$reason" ] && exit 0
oom=$(kernel_oom)
[ -n "$oom" ] && reason="$reason; kernel: $oom"
restarts=$(cat /opt/flowcast/guard_restarts 2>/dev/null || echo 0)
if [ "$restarts" -lt "$GUARD_RESTARTS" ]; then
  echo $((restarts + 1)) > /opt/flowcast/guard_restarts
  echo "GUARD $(date -u +%FT%TZ): $reason; restarting the job from its latest checkpoint"
  upload_logs
  systemctl stop flowcast-train.service
  systemctl start --no-block flowcast-train.service
  exit 0
fi
echo "GUARD $(date -u +%FT%TZ): $reason; already restarted $restarts time(s), failing the run"
systemctl stop flowcast-train.service
status failed "$reason"
sync_run
terminate_self
FLOWCAST_GUARD
chmod +x /opt/flowcast/job.sh /opt/flowcast/guard.sh

# OOMPolicy=continue: a data-loader worker killed by the kernel then surfaces as a training error (crashed ->
# guard restart) instead of systemd stopping the whole unit.
cat > /etc/systemd/system/flowcast-train.service <<'FLOWCAST_UNIT'
[Unit]
Description=flowcast training job
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart=/opt/flowcast/job.sh
Restart=no
TimeoutStopSec=60
OOMPolicy=continue

[Install]
WantedBy=multi-user.target
FLOWCAST_UNIT

cat > /etc/systemd/system/flowcast-guard.service <<'FLOWCAST_GUARD_UNIT'
[Unit]
Description=flowcast training guard (restart or fail a dead or stalled job)

[Service]
Type=oneshot
ExecStart=/opt/flowcast/guard.sh
FLOWCAST_GUARD_UNIT

cat > /etc/systemd/system/flowcast-guard.timer <<'FLOWCAST_GUARD_TIMER'
[Unit]
Description=flowcast training guard every 2 min

[Timer]
OnBootSec=5min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
FLOWCAST_GUARD_TIMER

systemctl daemon-reload
systemctl enable flowcast-train.service flowcast-guard.timer
systemctl start --no-block flowcast-train.service
systemctl start flowcast-guard.timer
