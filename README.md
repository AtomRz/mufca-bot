# MUFCA Bot [AtomDC] v3.0

Discord-бот для сканирования крипто пар на Gate.io с сигналами индикатора MUFCA.

## Пары и таймфреймы
- BTC/USDT — 1H, 4H
- ETH/USDT — 1H, 4H

## Логика (идентична Pine Script индикатору)
- FRAMA Channel + HTF Bias (4H/1D)
- Andean Oscillator + MFI KMeans (A-трек)
- UT Bot (U-трек)
- CHOP + ATR фильтры
- Fake Breakout + Liquidity Sweep фильтры
- Cooldown 2 бара, Position Guard
- AI Confidence 0–100%

## Команды Discord
| Команда | Описание |
|---|---|
| `!status` | Статус всех пар и позиций |
| `!scan ETH/USDT 1h` | Ручной запрос сигнала |

---

## 🐳 Запуск через Docker

### 1. Клонировать репозиторий
```bash
git clone https://github.com/atomrz/mufca-bot.git
cd mufca-bot
```

### 2. Создать .env файл
```bash
cp .env.example .env
nano .env
```
Заполни:
```
DISCORD_TOKEN=твой_токен
CHANNEL_NAME=general
```

### 3. Запустить
```bash
docker compose up -d
```

### 4. Логи
```bash
docker compose logs -f
```

### 5. Остановить
```bash
docker compose down
```

---

## 🖥️ TrueNAS Scale (Custom App)

1. Apps → Custom App
2. **Repository:** `ghcr.io/atomrz/mufca-bot` или собрать локально
3. **Environment Variables:**
   - `DISCORD_TOKEN` = твой токен
   - `CHANNEL_NAME` = general
4. **Restart Policy:** `Unless Stopped`

---

## 📦 Обновление бота
```bash
git pull
docker compose up -d --build
```
