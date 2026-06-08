#!/bin/bash
# Doble clic para iniciar la herramienta de collages
cd "$(dirname "$0")"

# matar instancias previas
pkill -9 -f "app.py" 2>/dev/null
sleep 1

echo ""
echo "  Iniciando Collage Tool..."
echo "  Abriendo http://127.0.0.1:5050 en tu navegador"
echo ""
echo "  >>> Para CERRAR la herramienta: cierra esta ventana <<<"
echo ""

# abrir el navegador despues de 2 segundos
( sleep 2 && open "http://127.0.0.1:5050" ) &

# arrancar el servidor (se queda corriendo aqui)
./venv/bin/python app.py
