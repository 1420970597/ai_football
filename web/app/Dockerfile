FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de
COPY web/app/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/app/index.html /usr/share/nginx/html/index.html
COPY web/app/app.css /usr/share/nginx/html/assets/app.css
COPY web/app/app.js /usr/share/nginx/html/assets/app.js
COPY web/app/ui.js /usr/share/nginx/html/assets/ui.js
COPY web/app/admin.js /usr/share/nginx/html/assets/admin.js
COPY web/app/identity.js /usr/share/nginx/html/assets/identity.js
COPY web/app/mfa.js /usr/share/nginx/html/assets/mfa.js
COPY web/app/qrcode.js /usr/share/nginx/html/assets/qrcode.js
COPY web/app/qrcode.LICENSE /usr/share/nginx/html/assets/qrcode.LICENSE
EXPOSE 80
