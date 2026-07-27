#!/usr/bin/env bash
# 给域名启用 HTTPS：nginx 反向代理 + Let's Encrypt 证书（自动续期）。
#   443: https 对外（远程运维 / 控制台加密访问）
#   80:  http 原样保留（企微回调按 http 注册，不重定向、不破坏）
# 用法（服务器 root）：
#   bash enable-https.sh            # 默认域名 ai.shsonghu.com
#   HTTPS_DOMAIN=xx.yy.com bash enable-https.sh
# 前置：轻量服务器防火墙需放行 TCP 443。

set -euo pipefail
DOMAIN="${HTTPS_DOMAIN:-ai.shsonghu.com}"
ENVFILE=/opt/pe/lawfirm-responder/.env

echo "==> 应用改为只监听本机 8020（对外 80/443 由 nginx 接管）"
grep -q "^RESPONDER_API_HOST=" "$ENVFILE" \
  && sed -i "s|^RESPONDER_API_HOST=.*|RESPONDER_API_HOST=127.0.0.1|" "$ENVFILE" \
  || echo "RESPONDER_API_HOST=127.0.0.1" >> "$ENVFILE"
grep -q "^RESPONDER_API_PORT=" "$ENVFILE" \
  && sed -i "s|^RESPONDER_API_PORT=.*|RESPONDER_API_PORT=8020|" "$ENVFILE" \
  || echo "RESPONDER_API_PORT=8020" >> "$ENVFILE"
systemctl restart responder
sleep 2
curl -fsS http://127.0.0.1:8020/health >/dev/null && echo "    应用已在 8020 运行 ✅"

echo "==> 安装 nginx 与证书工具"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq nginx python3-certbot-nginx

echo "==> 配置反向代理"
cat > /etc/nginx/sites-available/responder <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8020;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 30s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/responder /etc/nginx/sites-enabled/responder
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable -q nginx
systemctl restart nginx

echo "==> 申请 HTTPS 证书（Let's Encrypt，自动续期由 certbot.timer 负责）"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
  --register-unsafely-without-email --no-redirect

echo "==> 验证"
curl -fsS "http://$DOMAIN/health" >/dev/null && echo "    HTTP  80  ✅（企微回调通道不变）"
curl -fsS "https://$DOMAIN/health" && echo && echo "    HTTPS 443 ✅"
echo
echo "完成。若 https 验证失败，请检查轻量服务器防火墙是否已放行 TCP 443。"
