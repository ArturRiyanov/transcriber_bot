#!/bin/bash

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Проверяем, есть ли изменения
if git diff --quiet && git diff --cached --quiet && git status --porcelain | grep -q '^??'; then
    echo -e "${YELLOW}⚠️  Нет изменений для коммита.${NC}"
else
    # Коммит
    COMMIT_MSG=${1:-"Auto update: $(date '+%Y-%m-%d %H:%M:%S')"}
    echo -e "${GREEN}📦 Добавляем изменения...${NC}"
    git add .
    echo -e "${GREEN}📝 Коммит с сообщением: ${COMMIT_MSG}${NC}"
    git commit -m "$COMMIT_MSG"
fi

# Пуш на GitHub
echo -e "${GREEN}🚀 Отправляем на GitHub...${NC}"
if git push origin main; then
    echo -e "${GREEN}✅ Пуш успешен.${NC}"
else
    echo -e "${RED}❌ Ошибка при пуше.${NC}"
    exit 1
fi

# Обновление на сервере
echo -e "${GREEN}🔄 Обновляем сервер...${NC}"
SSH_HOST="buf-2-vm-faba"   # или IP-адрес
SSH_USER="root"

# Проверяем SSH-соединение
if ssh -q -o BatchMode=yes -o ConnectTimeout=5 $SSH_USER@$SSH_HOST "exit"; then
    echo -e "${GREEN}✅ SSH соединение установлено.${NC}"
else
    echo -e "${RED}❌ Не удалось подключиться по SSH к $SSH_HOST.${NC}"
    echo -e "${YELLOW}Проверьте доступность и настройку ключей.${NC}"
    exit 1
fi

# Выполняем команды на сервере
ssh $SSH_USER@$SSH_HOST << EOF
    cd ~/transcriber_bot
    echo "📥 Pull последних изменений..."
    git pull origin main
    echo "🔄 Перезапуск сервиса..."
    sudo systemctl restart transcriber_bot
    echo "✅ Готово."
EOF

echo -e "${GREEN}🎉 Деплой завершён!${NC}"