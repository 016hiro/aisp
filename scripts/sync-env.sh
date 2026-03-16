#!/usr/bin/env bash
# sync-env.sh — 将 .env.example 中新增的配置项追加到 .env
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXAMPLE=".env.example"
ENV=".env"

if [[ ! -f "$ENV" ]]; then
    cp "$EXAMPLE" "$ENV"
    echo "Created .env from .env.example"
    exit 0
fi

added=0
while IFS= read -r line; do
    # 匹配 "AISP_FOO=bar" 或 "# AISP_FOO=bar" 格式的行
    key=$(echo "$line" | sed -n 's/^#* *\(AISP_[A-Za-z0-9_]*\)=.*/\1/p')
    [[ -z "$key" ]] && continue

    # .env 中已存在该 key（无论注释与否）则跳过
    if ! grep -q "^#* *${key}=" "$ENV" 2>/dev/null; then
        if [[ $added -eq 0 ]]; then
            echo "" >> "$ENV"
        fi
        echo "$line" >> "$ENV"
        echo "  + $key"
        ((added++)) || true
    fi
done < "$EXAMPLE"

if [[ $added -eq 0 ]]; then
    echo ".env is up to date"
else
    echo "Synced $added new entries to .env"
fi
