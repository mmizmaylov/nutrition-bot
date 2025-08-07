#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<your-username>/nutrition-bot/main/deploy/setup-ubuntu-docker.sh | bash -s -- \
#     --repo-url https://github.com/<your-username>/nutrition-bot.git \
#     --telegram-token <TOKEN> \
#     --openai-key <KEY> \
#     [--timezone Europe/Moscow] [--model gpt-4o-mini]

REPO_URL=""
TELEGRAM_TOKEN=""
OPENAI_KEY=""
TIMEZONE="Europe/Moscow"
MODEL="gpt-4o-mini"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)
      REPO_URL="$2"; shift 2;;
    --telegram-token)
      TELEGRAM_TOKEN="$2"; shift 2;;
    --openai-key)
      OPENAI_KEY="$2"; shift 2;;
    --timezone)
      TIMEZONE="$2"; shift 2;;
    --model)
      MODEL="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if [[ -z "${REPO_URL}" || -z "${TELEGRAM_TOKEN}" || -z "${OPENAI_KEY}" ]]; then
  echo "Missing required args. See script header for usage." >&2
  exit 1
fi

sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg git

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

source /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] http://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

sudo mkdir -p /opt
cd /opt
if [[ ! -d nutrition-bot ]]; then
  sudo git clone "${REPO_URL}" nutrition-bot
fi
cd nutrition-bot

sudo tee .env >/dev/null <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_TOKEN}
OPENAI_API_KEY=${OPENAI_KEY}
OPENAI_VISION_MODEL=${MODEL}
DEFAULT_TIMEZONE=${TIMEZONE}
EOF

sudo docker compose up -d --build

echo "Started. View logs: sudo docker compose -f /opt/nutrition-bot/docker-compose.yml logs -f" 