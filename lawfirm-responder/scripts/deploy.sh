#!/usr/bin/env bash
# 一键部署（Ubuntu 22.04/24.04，root 执行，可重复运行）：
#   拉代码 → 装依赖 → 生成 .env → systemd 常驻服务（80 端口）→ 打印企微后台需要填的三个值
#
# 用法（在服务器终端粘贴，凭据通过环境变量传入）：
#   WECOM_CORP_ID=xxx WECOM_CORP_SECRET=xxx WECOM_AGENT_ID=xxx DEEPSEEK_API_KEY=xxx \
#     bash <(curl -fsSL https://raw.githubusercontent.com/FutureWarren/pe/claude/law-firm-wechat-ai-responder-q3nttv/lawfirm-responder/scripts/deploy.sh)
#
# 可选环境变量：
#   WECOM_TOKEN / WECOM_AES_KEY / ADMIN_TOKEN（不传则自动生成并打印）
#   WECOM_KF_SECRET             微信客服通道
#   WECOM_BOT_TOKEN / WECOM_BOT_AES_KEY / BOT_DEFAULT_NOTIFY_USERID  群聊助手
#   PUBLIC_BASE_URL             控制台对外地址（律师登录链接用），如 https://ai.example.com
#   DEFAULT_NOTIFY_USERID       兜底提醒接收人
#   KF_DEFAULT_LAWYER_NAME / KF_DEFAULT_CASE_TYPE   客服会话建档默认值
#   ANTHROPIC_API_KEY           备用模型供应商

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
apt-get install -y -qq --no-install-recommends git python3-venv python3-pip curl ca-certificates sqlite3

echo "==> 设置时区 Asia/Shanghai（白天/夜间补位等待时长按本地时间判断）"
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true

echo "==> 拉取代码（$BRANCH）"
if [ -d "$APP_DIR/.git" ]; then
  # 已有仓库的 origin 可能是 GitHub 直连地址；直连不通时改指向镜像，否则 fetch 会长时间挂死
  if [ -n "$GH_MIRROR" ] && ! git -C "$APP_DIR" remote get-url origin | grep -q "$GH_MIRROR"; then
    git -C "$APP_DIR" remote set-url origin "$REPO"
    echo "    origin 已切换为加速镜像"
  fi
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
RESPONDER_WECOM_KF_SECRET=${WECOM_KF_SECRET:-}
RESPONDER_WECOM_BOT_TOKEN=${WECOM_BOT_TOKEN:-}
RESPONDER_WECOM_BOT_AES_KEY=${WECOM_BOT_AES_KEY:-}
RESPONDER_BOT_DEFAULT_NOTIFY_USERID=${BOT_DEFAULT_NOTIFY_USERID:-}
RESPONDER_DEFAULT_NOTIFY_USERID=${DEFAULT_NOTIFY_USERID:-}
RESPONDER_KF_DEFAULT_LAWYER_NAME=${KF_DEFAULT_LAWYER_NAME:-}
RESPONDER_KF_DEFAULT_CASE_TYPE=${KF_DEFAULT_CASE_TYPE:-}
RESPONDER_PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
RESPONDER_API_HOST=0.0.0.0
RESPONDER_API_PORT=80
RESPONDER_DB_PATH=/opt/pe/lawfirm-responder/responder.db
EOF
  chmod 600 "$ENVFILE"
else
  echo "==> .env 已存在，保留原配置（仅补齐缺失项）"
  # 新版本引入的配置项按需追加，绝不覆盖既有值
  _ensure_env() {
    grep -q "^$1=" "$ENVFILE" || echo "$1=$2" >> "$ENVFILE"
  }
  _ensure_env RESPONDER_WECOM_KF_SECRET "${WECOM_KF_SECRET:-}"
  _ensure_env RESPONDER_WECOM_BOT_TOKEN "${WECOM_BOT_TOKEN:-}"
  _ensure_env RESPONDER_WECOM_BOT_AES_KEY "${WECOM_BOT_AES_KEY:-}"
  # 群聊没有「接待人」可查，线索简报的默认接收人只能显式指定，否则简报无人可推
  _ensure_env RESPONDER_BOT_DEFAULT_NOTIFY_USERID "${BOT_DEFAULT_NOTIFY_USERID:-}"
  # 律师登录链接的对外地址（留空则按请求 Host 推断，经 nginx 部署无需配置）
  _ensure_env RESPONDER_PUBLIC_BASE_URL "${PUBLIC_BASE_URL:-}"
  _ensure_env RESPONDER_DEFAULT_NOTIFY_USERID "${DEFAULT_NOTIFY_USERID:-}"
  _ensure_env RESPONDER_KF_DEFAULT_LAWYER_NAME "${KF_DEFAULT_LAWYER_NAME:-}"
  _ensure_env RESPONDER_KF_DEFAULT_CASE_TYPE "${KF_DEFAULT_CASE_TYPE:-}"
  _ensure_env ANTHROPIC_API_KEY "${ANTHROPIC_API_KEY:-}"
  _ensure_env DEEPSEEK_API_KEY "${DEEPSEEK_API_KEY:-}"
  # 带 RESPONDER_ 前缀的项：传入即覆盖
  for k in WECOM_BOT_TOKEN WECOM_BOT_AES_KEY BOT_DEFAULT_NOTIFY_USERID \
           DEFAULT_NOTIFY_USERID KF_DEFAULT_LAWYER_NAME KF_DEFAULT_CASE_TYPE \
           PUBLIC_BASE_URL; do
    v="$(eval echo "\${$k:-}")"
    [ -n "$v" ] && sed -i "s|^RESPONDER_$k=.*|RESPONDER_$k=$v|" "$ENVFILE"
  done
  # 无前缀的模型 key 同理（此前重跑命令带上也不会写入）
  for k in DEEPSEEK_API_KEY ANTHROPIC_API_KEY; do
    v="$(eval echo "\${$k:-}")"
    [ -n "$v" ] && sed -i "s|^$k=.*|$k=$v|" "$ENVFILE"
  done
  # 传入 WECOM_KF_SECRET 时以传入值为准（首次配置客服通道的路径）
  if [ -n "${WECOM_KF_SECRET:-}" ]; then
    sed -i "s|^RESPONDER_WECOM_KF_SECRET=.*|RESPONDER_WECOM_KF_SECRET=${WECOM_KF_SECRET}|" "$ENVFILE"
  fi
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
echo "==> 配置每日数据库备份（全量留痕是合规兜底，备份保留 30 天）"
mkdir -p /opt/pe-backups
cat > /usr/local/bin/responder-backup <<BACKUP
#!/usr/bin/env bash
set -e
DB=\$(grep -oP '^RESPONDER_DB_PATH=\K.*' "$ENVFILE" 2>/dev/null || echo /opt/pe/lawfirm-responder/responder.db)
[ -f "\$DB" ] || exit 0
sqlite3 "\$DB" ".backup '/opt/pe-backups/responder-\$(date +%F).db'"
find /opt/pe-backups -name 'responder-*.db' -mtime +30 -delete
BACKUP
chmod +x /usr/local/bin/responder-backup
cat > /etc/systemd/system/responder-backup.service <<EOF
[Unit]
Description=Responder DB daily backup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/responder-backup
EOF
cat > /etc/systemd/system/responder-backup.timer <<EOF
[Unit]
Description=Responder DB daily backup timer

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable -q responder
systemctl enable -q --now responder-backup.timer
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
KF=$(grep -oP '^RESPONDER_WECOM_KF_SECRET=\K.*' "$ENVFILE" || true)
if [ -n "$KF" ]; then
  echo "   微信客服通道：已配置 ✅（客户咨询自动进入，无需 @）"
else
  echo "   微信客服通道：未配置（可选）。后台 → 客户与上下游 → 微信客服 → API"
  echo "   拿到 Secret 后重跑本命令并加上 WECOM_KF_SECRET=xxx 即可开启"
fi
BOTT=$(grep -oP '^RESPONDER_WECOM_BOT_TOKEN=\K.*' "$ENVFILE" || true)
if [ -n "$BOTT" ]; then
  echo "   群聊助手通道：已配置 ✅（群内 @ 机器人触发，新群自动建档）"
else
  echo "   群聊助手通道：未配置（可选）。后台 → 应用管理 → 智能机器人 → 创建"
  echo "   接收消息 URL 填 https://<域名>/wecom/bot/callback，拿到它自己的"
  echo "   Token / EncodingAESKey 后重跑本命令并加上："
  echo "   WECOM_BOT_TOKEN=xxx WECOM_BOT_AES_KEY=xxx BOT_DEFAULT_NOTIFY_USERID=<接收简报的企微userid>"
fi
echo
echo "③ 控制台访问令牌（发给运维/Claude 用，勿泄露）："
echo "   X-Admin-Token: $(grep -oP '^RESPONDER_ADMIN_TOKEN=\K.*' "$ENVFILE")"
echo
echo "常用命令：systemctl status responder / journalctl -u responder -f"
echo "=============================================================="
