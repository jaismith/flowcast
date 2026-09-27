#!/bin/bash
# flowcast training instance bootstrap (EC2 user data, first boot only).
# Installs /opt/flowcast/job.sh as a systemd service that runs on every boot, so a Spot instance that was
# stopped by an interruption resumes its run when EC2 starts it again.
set -euo pipefail
mkdir -p /opt/flowcast
cat > /opt/flowcast/env <<'FLOWCAST_ENV'
#__FLOWCAST_ENV__
FLOWCAST_ENV

cat > /opt/flowcast/job.sh <<'FLOWCAST_JOB'
#!/bin/bash
set -uo pipefail
source /opt/flowcast/env
export HOME=/root AWS_DEFAULT_REGION="$S3_REGION" PATH="/root/.local/bin:/usr/local/bin:$PATH" PYTHONUNBUFFERED=1
LOG=/opt/flowcast/job.log
exec >>"$LOG" 2>&1
RUN_S3="s3://$BUCKET/runs/$RUN_ID"
RUN_DIR="/opt/flowcast/runs/$RUN_ID"
imds() { curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')" "http://169.254.169.254/latest/meta-data/$1"; }
INSTANCE_ID=$(imds instance-id)
AZ=$(imds placement/availability-zone)
ITYPE=$(imds instance-type)
BOOT=$(( $(cat /opt/flowcast/boots 2>/dev/null || echo 0) + 1 ))
echo "$BOOT" > /opt/flowcast/boots
echo "=== $(date -u +%FT%TZ) boot $BOOT of run $RUN_ID on $INSTANCE_ID ($ITYPE, $AZ)"

status() {
  printf '{"status": "%s", "detail": "%s", "time": "%s", "instance": "%s", "boot": %s}\n' "$1" "${2:-}" "$(date -u +%FT%TZ)" "$INSTANCE_ID" "$BOOT" > /opt/flowcast/status.json
  aws s3 cp /opt/flowcast/status.json "$RUN_S3/status.json" --only-show-errors || true
}
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
trap 'echo "SIGTERM (instance stopping)"; upload_logs; exit 0' TERM

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

# Same-region datasets go to instance NVMe when it is big enough (fast, re-pulled after a stop). Cross-region
# datasets, and datasets larger than the NVMe (DATA_ON_EBS, decided by the launcher), are cached on the EBS root,
# which the launcher sizes for them and which survives Spot stop/start.
read -r -a DS_URIS <<< "$DATASET_URIS"
read -r -a DS_REGIONS <<< "$DATASET_REGIONS"
DATA=/opt/flowcast/data
cross_region=0
for r in "${DS_REGIONS[@]}"; do [ "$r" != "$REGION" ] && cross_region=1; done
if [ "$cross_region" = "0" ] && [ "${DATA_ON_EBS:-0}" = "0" ] && [ -d /opt/dlami/nvme ] && [ -w /opt/dlami/nvme ]; then DATA=/opt/dlami/nvme/flowcast-data; fi
mkdir -p "$DATA"
CUBES=""
for i in "${!DS_URIS[@]}"; do
  uri="${DS_URIS[$i]}"
  dest="$DATA/cube$i-$(basename "$uri")"
  status staging "syncing $uri"
  aws s3 sync "$uri" "$dest" --region "${DS_REGIONS[$i]}" --only-show-errors || fail "dataset sync $uri"
  CUBES="$CUBES $dest"
done

mkdir -p "$RUN_DIR"
if [ ! -f "$RUN_DIR/checkpoint.json" ]; then
  aws s3 sync "$RUN_S3/run/" "$RUN_DIR" --only-show-errors --exclude 'hindcast/*' || true
fi
aws s3 cp "$RUN_S3/config.yml" /opt/flowcast/config.yml --only-show-errors || fail "config download"

status training "boot $BOOT"
uv run flowcast-model train --config /opt/flowcast/config.yml --run-dir "$RUN_DIR" --cube $CUBES --sync-to "$RUN_S3/run/" || fail "training exited with $?"
sync_run

if [ "$(date +%s)" -lt $((DEADLINE_EPOCH - 600)) ]; then
  status hindcast
  uv run flowcast-model hindcast --run-dir "$RUN_DIR" --out "$RUN_DIR/hindcast" || fail "hindcast exited with $?"
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
chmod +x /opt/flowcast/job.sh

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

[Install]
WantedBy=multi-user.target
FLOWCAST_UNIT
systemctl daemon-reload
systemctl enable flowcast-train.service
systemctl start --no-block flowcast-train.service
