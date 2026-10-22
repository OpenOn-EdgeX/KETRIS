#!/bin/bash
# KRAKEN controller 컨테이너 엔트리포인트 (Exp_42)
#
# 구 SHM(v1, magic=PRSM) 잔존 시 명시적으로 제거 후 재생성한다 (Exp_41 설계 —
# 신 코드는 v1 attach 를 거부하므로, 여기서 지우지 않으면 gating_lib/libbless
# 쪽에서 기동 실패로 드러난다. 조용한 오독 대신 명시 처리).
set -e

# [Exp_123 2부] 배포 검증 스탬프 — deploy_verify 가 이 줄을 읽는다.
STAMP="$(cat /opt/kraken/BUILD_STAMP 2>/dev/null || echo unstamped)"
echo "[controller] build=$STAMP"
# [Exp_134] 로그 회전 대비 — 스탬프를 파일로도 남긴다(§0 참조).
mkdir -p /var/lib/kraken && printf '%s\n' "$STAMP" > /var/lib/kraken/stamp-controller \
  || echo "[controller][경고] 스탬프 파일 기록 실패 — deploy_verify 는 로그 경로만 쓴다"

GROUP="${KRAKEN_GROUP:-default}"
SHM="/dev/shm/kraken_${GROUP}"

if [ -f "$SHM" ]; then
  python3 - "$SHM" <<'EOF'
import struct, sys, os
p = sys.argv[1]
with open(p, "rb") as f:
    magic = struct.unpack("<I", f.read(4))[0]
if magic == 0x50525332:  # PRS2
    print(f"[entrypoint-controller] SHM v2 감지({p}) — create_shm 이 재초기화")
else:
    name = "v1(PRSM)" if magic == 0x5052534D else f"unknown(0x{magic:08X})"
    print(f"[entrypoint-controller] 구 SHM {name} 감지 → 명시적 제거 후 v2 재생성")
    os.remove(p)
EOF
fi

# --sm/--mem: 컨테이너에는 nvidia-smi 가 없으므로 DS env 로 명시 주입
# (232 Pro6000: SM=188, mem=97887MB — 미지정 시 controller 폴백 80/40000 방지)
exec python3 -u /opt/kraken/controller/kraken_controller.py \
  --group "$GROUP" --port 8090 ${KRAKEN_CONTROLLER_ARGS}
