#!/usr/bin/env bash
# Instala el servicio de usuario de systemd y genera el bookmarklet.
# Installs the systemd user service and generates the bookmarklet.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8765}"
YTDLP="${YTDLP:-$(command -v yt-dlp)}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ytdlp-bookmarklet.service"

# Idioma: español si el sistema está en español, inglés en otro caso.
sys_lang="${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}"
case "${sys_lang%%:*}" in
  es*) LANG_CODE="es" ;;
  *)   LANG_CODE="en" ;;
esac

if [ "$LANG_CODE" = "es" ]; then
  DESCRIPTION="Servidor local para descargar vídeos con yt-dlp desde un bookmarklet"
  MSG_INSTALLED="Servicio instalado y arrancado en http://127.0.0.1:$PORT (descargas en"
  MSG_SAVED="Bookmarklet guardado en"
  MSG_BOOKMARK="Crea un marcador en Chrome y pega ese texto como URL."
  MSG_JS_FOUND="Runtime de JavaScript para yt-dlp:"
  MSG_JS_MISSING="Aviso: no se ha encontrado deno ni node. YouTube puede omitir formatos; instala deno o exporta JS_RUNTIME=node:/ruta/a/node y vuelve a ejecutar install.sh."
  DEFAULT_DOWNLOADS="$HOME/Descargas"
else
  DESCRIPTION="Local server to download videos with yt-dlp from a bookmarklet"
  MSG_INSTALLED="Service installed and running on http://127.0.0.1:$PORT (downloads in"
  MSG_SAVED="Bookmarklet saved to"
  MSG_BOOKMARK="Create a bookmark in Chrome and paste that text as its URL."
  MSG_JS_FOUND="JavaScript runtime for yt-dlp:"
  MSG_JS_MISSING="Warning: neither deno nor node was found. YouTube may omit formats; install deno or export JS_RUNTIME=node:/path/to/node and run install.sh again."
  DEFAULT_DOWNLOADS="$HOME/Downloads"
fi

DOWNLOAD_DIR="${DOWNLOAD_DIR:-$(xdg-user-dir DOWNLOAD 2>/dev/null || echo "$DEFAULT_DOWNLOADS")}"

# Runtime de JavaScript para yt-dlp: deno si está en el PATH; si no, el node más
# reciente (PATH o nvm). Se puede forzar con JS_RUNTIME=deno o JS_RUNTIME=node:/ruta.
detect_js_runtime() {
  local node
  if command -v deno >/dev/null 2>&1; then
    echo "deno:$(command -v deno)"; return
  fi
  node="$(command -v node 2>/dev/null || true)"
  if [ -z "$node" ] || [ ! -x "$node" ]; then
    node="$(ls -1 "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -V | tail -1 || true)"
  fi
  [ -n "$node" ] && [ -x "$node" ] && echo "node:$node"
}
JS_RUNTIME="${JS_RUNTIME:-$(detect_js_runtime)}"
JS_RUNTIME_ARG=""
[ -n "$JS_RUNTIME" ] && JS_RUNTIME_ARG="--js-runtime $JS_RUNTIME"

mkdir -p "$UNIT_DIR"
sed -e "s|@DIR@|$DIR|g" \
    -e "s|@PORT@|$PORT|g" \
    -e "s|@DOWNLOAD_DIR@|$DOWNLOAD_DIR|g" \
    -e "s|@YTDLP@|$YTDLP|g" \
    -e "s|@LANG@|$LANG_CODE|g" \
    -e "s|@JS_RUNTIME_ARG@|$JS_RUNTIME_ARG|g" \
    -e "s|@DESCRIPTION@|$DESCRIPTION|g" \
    "$DIR/ytdlp-bookmarklet.service" > "$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now ytdlp-bookmarklet.service
systemctl --user restart ytdlp-bookmarklet.service

python3 "$DIR/server.py" --port "$PORT" --bookmarklet > "$DIR/bookmarklet.txt"

echo "$MSG_INSTALLED $DOWNLOAD_DIR)"
if [ -n "$JS_RUNTIME" ]; then echo "$MSG_JS_FOUND $JS_RUNTIME"; else echo "$MSG_JS_MISSING"; fi
echo "$MSG_SAVED $DIR/bookmarklet.txt:"
echo
cat "$DIR/bookmarklet.txt"
echo
echo "$MSG_BOOKMARK"
