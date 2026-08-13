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

# **这一行不能少，少了这套 HTTPS 等于白上。**
# 应用默认不信 X-Forwarded-For（信了就等于谁都能换个头绕过登录锁定）。
# 而 nginx 一挡在前面，所有请求的对端地址就都变成 127.0.0.1——
# 于是「同一 IP 15 分钟最多错 8 次」这道锁，从「按人分桶」塌成「全世界一个桶」：
# 一个爆破者能把所里所有人一起锁在门外，而他自己换个 IP 继续试。
# 配成 1 = 只信我们自己这一跳写的那一段（从右往左数）。
grep -q "^RESPONDER_TRUSTED_PROXY_HOPS=" "$ENVFILE" \
  && sed -i "s|^RESPONDER_TRUSTED_PROXY_HOPS=.*|RESPONDER_TRUSTED_PROXY_HOPS=1|" "$ENVFILE" \
  || echo "RESPONDER_TRUSTED_PROXY_HOPS=1" >> "$ENVFILE"
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

    # 企微回调按 http 注册，**这几条永远不能跳转**——跳了消息就进不来，
    # 而表现是「AI 一句话都不回」，且后台一切正常。
    location /wecom/  { proxy_pass http://127.0.0.1:8020; include /etc/nginx/proxy_params_responder; }
    location /douyin/ { proxy_pass http://127.0.0.1:8020; include /etc/nginx/proxy_params_responder; }
    location /ingest  { proxy_pass http://127.0.0.1:8020; include /etc/nginx/proxy_params_responder; }
    location /health  { proxy_pass http://127.0.0.1:8020; include /etc/nginx/proxy_params_responder; }

    # 其余（控制台、律师登录链接、客户档案导出）一律跳 https：
    # 那些页面上是全所客户的咨询原文和手机号，而免登录链接本身就等于密码。
    location / { return 301 https://\$host\$request_uri; }
}
EOF

# 转发头单独抽一份：X-Forwarded-For 决定登录锁定按谁分桶，
# X-Forwarded-Proto 决定应用生成的登录链接是 http 还是 https。
cat > /etc/nginx/proxy_params_responder <<'EOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_read_timeout 30s;
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
curl -fsS -o /dev/null -w '%{http_code}\n' "http://$DOMAIN/ui" | grep -q 301 \
  && echo "    控制台已强制 https ✅"
curl -fsS "https://$DOMAIN/health" && echo && echo "    HTTPS 443 ✅"
echo
echo "完成。若 https 验证失败，请检查轻量服务器防火墙是否已放行 TCP 443。"
