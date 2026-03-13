#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${OLLAMA_MODEL_ALIAS_BY_MODEL:-}" ]]; then
  echo "OLLAMA_MODEL_ALIAS_BY_MODEL is empty; nothing to build."
  exit 0
fi

default_ctx="${OLLAMA_NUM_CTX:-4096}"

declare -A ctx_map
IFS=',' read -r -a ctx_entries <<< "${OLLAMA_NUM_CTX_BY_MODEL:-}"
for entry in "${ctx_entries[@]}"; do
  entry="${entry//[[:space:]]/}"
  [[ -z "$entry" ]] && continue
  [[ "$entry" != *"="* ]] && continue
  model="${entry%%=*}"
  ctx="${entry#*=}"
  [[ -z "$model" || -z "$ctx" ]] && continue
  ctx_map["$model"]="$ctx"
done

IFS=',' read -r -a alias_entries <<< "${OLLAMA_MODEL_ALIAS_BY_MODEL:-}"
for entry in "${alias_entries[@]}"; do
  entry="${entry//[[:space:]]/}"
  [[ -z "$entry" ]] && continue
  [[ "$entry" != *"="* ]] && continue
  model="${entry%%=*}"
  alias="${entry#*=}"
  [[ -z "$model" || -z "$alias" ]] && continue

  ctx="${ctx_map[$model]:-$default_ctx}"
  tmp_modelfile="$(mktemp)"
  safe_alias="${alias//[:\/]/_}"
  container_modelfile="/tmp/${safe_alias}.Modelfile"

  printf 'FROM %s\nPARAMETER num_ctx %s\n' "$model" "$ctx" > "$tmp_modelfile"
  echo "Creating alias $alias (FROM $model, num_ctx=$ctx)"
  docker compose exec -T ollama sh -lc "cat > '$container_modelfile' && ollama create '$alias' -f '$container_modelfile'" < "$tmp_modelfile"
  rm -f "$tmp_modelfile"
done

echo
echo "Current Ollama models:"
docker compose exec -T ollama ollama list
