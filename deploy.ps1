param(
    [string]$CommitMessage = "Auto update: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
)

Write-Host "📦 Добавляем изменения..." -ForegroundColor Green
git add .

# Проверяем, есть ли изменения для коммита
$status = git status --porcelain
if ($status) {
    Write-Host "📝 Коммит с сообщением: $CommitMessage" -ForegroundColor Green
    git commit -m $CommitMessage
} else {
    Write-Host "⚠️ Нет изменений для коммита." -ForegroundColor Yellow
}

Write-Host "🚀 Отправляем на GitHub..." -ForegroundColor Green
git push origin main

Write-Host "🔄 Обновляем сервер (72.56.40.234)..." -ForegroundColor Green
ssh root@72.56.40.234 "cd ~/transcriber_bot && git pull origin main && sudo systemctl restart transcriber_bot"

Write-Host "✅ Готово!" -ForegroundColor Green