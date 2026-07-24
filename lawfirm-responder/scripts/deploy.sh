#!/usr/bin/env bash
# 一键部署（Ubuntu 22.04/24.04，root 执行，可重复运行）：
#   拉代码 → 装依赖 → 生成 .env → systemd 常驻服务（80 端口）→ 打印企微后台需要填的三个值
#
# 用法（在服务器终端粘贴，凭据通过环境变量传入）：
#   WECOM_CORP_ID=xxx WECOM_CORP_SECRET=xxx WECOM_AGENT_ID=xxx DEEPSEEK_API_KEY=xxx \
#     bash <(curl -fsSL https://raw.githubusercontent.com/FutureWarren/pe/claude/law-firm-wechat-ai-responder-q3nttv/lawfirm-responder/scripts/deploy.sh)
#
# 可选环境变量：WECOM_TOKEN / WECOM_AES_KEY / ADMIN_TOKEN（不传则自动生成并打印）

set -euo pipefail

BRANCH="${DEPLOY_BRANCH:-claude/law-firm-wechat-ai-responder-q3nttv}"
REPO="${DEPLOY_REPO:-https://github.com/FutureWarren/pe.git}"
# 国内服务器访问 GitHub 不稳时，可传 GH_MIRROR=https://ghfast.top 走加速镜像
GH_MIRROR="${GH_MIRROR:-}"
APP_DIR=/opt/pe
VENV=/opt/pe-venv
ENVFILE="$APP_DIR/lawfirm-responder/.env"

echo "==> 检查系统版本"
PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 0)
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "❌ 本机 Python 为 $PYV，需要 ≥3.10。请把系统重装为 Ubuntu 22.04/24.04 后重试。" >&2
  echo "   路径：轻量云控制台 → 该实例 → 更多操作 → 重装系统 → 系统镜像 → Ubuntu 22.04" >&2
  exit 1
fi
echo "    Python $PYV ✅"

# GitHub 连通性探测：不通且未指定镜像时自动启用加速
if [ -z "$GH_MIRROR" ] && ! curl -fsS -m 8 -o /dev/null https://github.com; then
  GH_MIRROR="https://ghfast.top"
  echo "==> GitHub 直连不通，启用加速镜像 $GH_MIRROR"
fi
if [ -n "$GH_MIRROR" ]; then
  REPO="${GH_MIRROR}/${REPO}"
fi

echo "==> 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq --no-install-recommends git python3-venv python3-pip curl ca-certificates

echo "==> 拉取代码（$BRANCH）"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "==> 安装 Python 依赖"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$APP_DIR/lawfirm-responder"

# 注意：不能写 `tr </dev/urandom | head`——head 关管道会让 tr 收到 SIGPIPE，
# 在 set -o pipefail 下整条命令失败并静默退出脚本。先限量读取再过滤即可。
_rand() { head -c 4096 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c "$1"; }

if [ ! -f "$ENVFILE" ]; then
  echo "==> 生成 .env"
  : "${WECOM_CORP_ID:?缺少 WECOM_CORP_ID}"
  : "${WECOM_CORP_SECRET:?缺少 WECOM_CORP_SECRET}"
  : "${WECOM_AGENT_ID:?缺少 WECOM_AGENT_ID}"
  WECOM_TOKEN="${WECOM_TOKEN:-$(_rand 24)}"
  WECOM_AES_KEY="${WECOM_AES_KEY:-$(_rand 43)}"
  ADMIN_TOKEN="${ADMIN_TOKEN:-$(_rand 32)}"
  if [ "${#WECOM_TOKEN}" -lt 24 ] || [ "${#WECOM_AES_KEY}" -ne 43 ] || [ "${#ADMIN_TOKEN}" -lt 32 ]; then
    echo "❌ 随机密钥生成异常，请重跑一次部署命令。" >&2
    exit 1
  fi
  cat > "$ENVFILE" <<EOF
RESPONDER_MODE=shadow
RESPONDER_LLM_PROVIDER=auto
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}
RESPONDER_WECOM_CORP_ID=$WECOM_CORP_ID
RESPONDER_WECOM_CORP_SECRET=$WECOM_CORP_SECRET
RESPONDER_WECOM_AGENT_ID=$WECOM_AGENT_ID
RESPONDER_WECOM_TOKEN=$WECOM_TOKEN
RESPONDER_WECOM_ENCODING_AES_KEY=$WECOM_AES_KEY
RESPONDER_ADMIN_TOKEN=$ADMIN_TOKEN
RESPONDER_API_HOST=0.0.0.0
RESPONDER_API_PORT=80
RESPONDER_DB_PATH=/opt/pe/lawfirm-responder/responder.db
EOF
  chmod 600 "$ENVFILE"
else
  echo "==> .env 已存在，保留原配置"
fi

echo "==> 配置 systemd 服务"
cat > /etc/systemd/system/responder.service <<EOF
[Unit]
Description=Lawfirm AI First Responder
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR/lawfirm-responder
ExecStart=$VENV/bin/responder-api
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q responder
systemctl restart responder
sleep 2

PORT=$(grep -oP '^RESPONDER_API_PORT=\K.*' "$ENVFILE" || echo 80)
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "==> 服务运行正常 ✅"
else
  echo "==> 服务未响应 ❌ 查看日志：journalctl -u responder -n 50" >&2
  exit 1
fi

IP=$(curl -fsS ifconfig.me || curl -fsS ip.sb || echo "<服务器公网IP>")
echo
echo "=============================================================="
echo "部署完成。企业微信管理后台（应用管理 → 该应用）需要填三处："
echo
echo "① 接收消息 API 设置："
echo "   URL:            http://$IP/wecom/callback"
echo "   Token:          $(grep -oP '^RESPONDER_WECOM_TOKEN=\K.*' "$ENVFILE")"
echo "   EncodingAESKey: $(grep -oP '^RESPONDER_WECOM_ENCODING_AES_KEY=\K.*' "$ENVFILE")"
echo
echo "② 企业可信IP：$IP"
echo
echo "③ 控制台访问令牌（发给运维/Claude 用，勿泄露）："
echo "   X-Admin-Token: $(grep -oP '^RESPONDER_ADMIN_TOKEN=\K.*' "$ENVFILE")"
echo
echo "常用命令：systemctl status responder / journalctl -u responder -f"
echo "=============================================================="
