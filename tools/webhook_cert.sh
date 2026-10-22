#!/bin/bash
# [Exp_137 1-C] webhook TLS 인증서 발급·갱신.
#
# ★만료되면 조용히 뚫린다. failurePolicy=Ignore 라 TLS 실패가 "호출 실패 → 통과" 로
#   처리되고, 파드는 정상 생성되며 아무 경고도 없다. 그래서 만료일을 배포 시 찍어
#   남기고, 이 스크립트 하나로 갱신이 끝나게 한다.
#
# usage: tools/webhook_cert.sh [--days N]        (기본 3650)
#   1) 자체서명 인증서 발급 (SAN = Service DNS)
#   2) kube-system/ketris-webhook-tls Secret 생성·갱신
#   3) MutatingWebhookConfiguration 의 caBundle 갱신 (등록돼 있으면)
set -u
cd "$(dirname "$0")/.."
NS=kube-system; SVC=ketris-webhook; SECRET=ketris-webhook-tls
CFG=ketris-env-webhook
DAYS=3650; [ "${1:-}" = "--days" ] && DAYS=$2
D=$(mktemp -d)

cat > $D/openssl.cnf <<EOF
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=$SVC.$NS.svc
[ext]
subjectAltName=DNS:$SVC,DNS:$SVC.$NS,DNS:$SVC.$NS.svc,DNS:$SVC.$NS.svc.cluster.local
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout $D/tls.key -out $D/tls.crt -config $D/openssl.cnf 2>/dev/null || {
    echo "❌ 인증서 발급 실패"; rm -rf $D; exit 1; }

kubectl -n $NS create secret tls $SECRET --cert=$D/tls.crt --key=$D/tls.key \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null || {
    echo "❌ Secret 갱신 실패"; rm -rf $D; exit 1; }

CA=$(base64 -w0 $D/tls.crt)
if kubectl get mutatingwebhookconfigurations $CFG >/dev/null 2>&1; then
  kubectl patch mutatingwebhookconfigurations $CFG --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/webhooks/0/clientConfig/caBundle\",\"value\":\"$CA\"}]" >/dev/null \
    && echo "  caBundle 갱신 완료"
elif [ -f webhook/deploy/webhook_config.yaml ]; then
  # 미등록이면 매니페스트를 caBundle 채워서 등록한다 — 배포가 한 번에 끝나게.
  #   (매니페스트는 자리표시자를 들고 있다. 실제 인증서를 레포에 두지 않기 위함.)
  sed "s|<BASE64_CA_BUNDLE>.*|$CA|" webhook/deploy/webhook_config.yaml | kubectl apply -f - >/dev/null \
    && echo "  $CFG 등록 완료 (caBundle 주입)"
else
  echo "  ⓘ $CFG 미등록이고 매니페스트도 없다 — Secret 만 갱신했다"
fi
echo "  만료: $(openssl x509 -in $D/tls.crt -noout -enddate | cut -d= -f2)"
echo "  SAN : $(openssl x509 -in $D/tls.crt -noout -ext subjectAltName | tail -1 | xargs)"
echo "$CA" > /tmp/kraken_webhook_ca.b64
echo "  caBundle → /tmp/kraken_webhook_ca.b64"

# ★[Exp_137] 갱신 뒤 파드를 반드시 재시작한다.
#   서버는 기동 시 인증서를 읽는다. Secret 만 바꾸면 **도는 파드는 옛 인증서를 계속
#   쓰고**, 등록된 caBundle 과 어긋나 TLS 가 실패한다. failurePolicy=Ignore 라 파드는
#   정상 생성되고 **아무 경고도 없다** — 갱신했는데 조용히 안 걸리는 상태가 된다.
#   (실제로 이 스크립트를 처음 돌렸을 때 그렇게 됐다.)
if kubectl -n $NS get deploy $SVC >/dev/null 2>&1; then
  kubectl -n $NS rollout restart deploy/$SVC >/dev/null
  kubectl -n $NS rollout status deploy/$SVC --timeout=180s >/dev/null \
    && echo "  파드 재시작 완료 — 새 인증서 적재됨" \
    || echo "  ⚠️ 파드 재시작이 끝나지 않았다 — 확인 필요"
else
  echo "  ⓘ $SVC Deployment 없음 — 파드 재시작 생략(호스트 실행 중이면 직접 재기동할 것)"
fi
rm -rf $D
