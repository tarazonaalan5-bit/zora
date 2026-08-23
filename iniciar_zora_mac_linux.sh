#!/bin/bash
# ============================================================
#  Iniciar Zora (Mac / Linux)
# ============================================================
#  Tus API keys NO van en este archivo. Van todas juntas en
#  "zora.env" (el archivo de al lado de este) - ahi las pegas,
#  una vez, y esto simplemente arranca el backend, que las lee
#  solo.
#
#  Si "zora.env" no existe todavia, Zora arranca igual, solo
#  que sin las funciones que necesitan esas keys.
#
#  Primera vez, dale permiso de ejecucion:
#      chmod +x iniciar_zora_mac_linux.sh
#  Luego, cada vez:
#      ./iniciar_zora_mac_linux.sh
# ============================================================

echo "Iniciando Zora..."
python3 zora_backend.py
