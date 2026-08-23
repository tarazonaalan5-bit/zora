# Sacar el backend de Zora a internet (para que funcione desde cualquier lugar)

Ahora mismo el backend corre en `localhost` en tu PC, así que el celular y
la laptop solo pueden hablarle si están en la misma red WiFi. Esto lo
resuelve todo: lo pones en un servidor con IP pública y ya no dependes de
estar en la misma red.

## Opción recomendada: una VPS barata (DigitalOcean, Hetzner, AWS Lightsail)

Cuestan entre 4 y 6 USD/mes. Pasos:

1. **Crea el servidor**: elige Ubuntu 22.04, el plan más barato (1 GB RAM
   alcanza de sobra para este backend).
2. **Copia los archivos** al servidor:
   ```bash
   scp zora_backend.py root@TU_IP_DEL_SERVIDOR:/root/
   ```
3. **Instala Python** (normalmente ya viene) y corre el backend:
   ```bash
   ssh root@TU_IP_DEL_SERVIDOR
   export ANTHROPIC_API_KEY="tu-api-key"      # opcional, para el cerebro en la nube
   export ZORA_SMTP_HOST="smtp.gmail.com"     # opcional, para que el SOS mande correos reales
   export ZORA_SMTP_USER="tu_correo@gmail.com"
   export ZORA_SMTP_PASS="tu-contraseña-de-aplicación-de-gmail"
   python3 zora_backend.py
   ```
4. **Que siga corriendo aunque cierres la sesión SSH** (con `systemd`, la
   forma correcta — se reinicia solo si el servidor reinicia o si el
   proceso falla):

   Crea `/etc/systemd/system/zora.service`:
   ```ini
   [Unit]
   Description=Zora backend
   After=network.target

   [Service]
   ExecStart=/usr/bin/python3 /root/zora_backend.py
   WorkingDirectory=/root
   Restart=always
   Environment=ANTHROPIC_API_KEY=tu-api-key
   Environment=ZORA_SMTP_HOST=smtp.gmail.com
   Environment=ZORA_SMTP_USER=tu_correo@gmail.com
   Environment=ZORA_SMTP_PASS=tu-contraseña-de-aplicación

   [Install]
   WantedBy=multi-user.target
   ```
   Luego:
   ```bash
   sudo systemctl enable --now zora
   ```

5. **En cada app/cliente** (web, escritorio, Android), cambia la "URL del
   backend" de `http://localhost:8000` a `http://TU_IP_DEL_SERVIDOR:8000`.

6. **HTTPS (recomendado, no obligatorio para probar)**: si compras un
   dominio y lo apuntas a la IP del servidor, puedes poner Nginx +
   Certbot (Let's Encrypt, gratis) delante del backend para tener
   `https://zora.tudominio.com` en vez de una IP con puerto expuesto.
   Esto también evita que Android bloquee el tráfico por no ser HTTPS —
   con dominio propio ya no necesitas el archivo
   `network_security_config.xml` que dejamos como parche.

## Nota de seguridad importante

El backend actual, tal como está, es un **prototipo funcional, no un
sistema listo para producción con datos reales de tu familia**. Antes de
usarlo con datos reales (ubicación, contactos de emergencia) en un
servidor expuesto a internet, como mínimo deberías:
- Poner HTTPS (paso 6 arriba) — si no, las contraseñas y tokens viajan
  sin cifrar.
- Limitar quién puede registrarse (`/register` ahora mismo es público:
  cualquiera que encuentre tu IP podría crear una cuenta).
- Hacer respaldos periódicos de `zora.db`.

Puedo ayudarte a resolver cualquiera de estos si quieres seguir con eso.
