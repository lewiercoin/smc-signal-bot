# SMC Signal Bot — Deploy na Hetzner CX22

## Wymagania serwera
- Ubuntu 22.04/24.04
- Python 3.11+
- Min 2GB RAM, 20GB SSD (CX22 = 4GB/40GB — OK)

## Krok 1: Przygotowanie serwera
```bash
# SSH do serwera
ssh root@<SERVER_IP>

# Update + Python
apt update && apt upgrade -y
apt install python3.11 python3.11-venv python3-pip git -y

# Firewall
ufw allow 22    # SSH
ufw allow 8443  # Webhook
ufw enable
```

## Krok 2: Deploy kodu
```bash
# Clone repo
git clone https://github.com/lewiercoin/smc-signal-bot.git
cd smc-signal-bot

# Venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Krok 3: Konfiguracja
```bash
# .env
cp deploy/.env.example .env
nano .env
# Wypełnij: OANDA_API_KEY, OANDA_ACCOUNT_ID, ANTHROPIC_API_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID,
# TELEGRAM_ADMIN_CHAT_ID, WEBHOOK_URL=https://<SERVER_IP>:8443
```

## Krok 4: SSL (dla webhook)
```bash
# Self-signed cert (wystarczający dla Telegram webhook)
mkdir -p deploy/ssl
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout deploy/ssl/private.key \
  -x509 -days 365 \
  -out deploy/ssl/cert.pem \
  -subj "/CN=<SERVER_IP>"

# Zarejestruj cert z Telegramem
# (python-telegram-bot robi to automatycznie przez set_webhook z cert param)
```

## Krok 5: Systemd service
```bash
sudo cp deploy/smc-signal-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smc-signal-bot
sudo systemctl start smc-signal-bot
```

## Krok 6: Paper Trading (pierwsza faza — przed live)
```bash
# Uruchom paper trading na 24-72h
python main.py paper

# Sprawdź logi
tail -f paper_trading/logs/summary_*.json

# Analiza wyników (po 20+ trades)
python main.py analyze
```

## Krok 7: Monitoring
```bash
# Status
sudo systemctl status smc-signal-bot

# Logi live
sudo journalctl -u smc-signal-bot -f

# Admin commands przez Telegram
/status   - status bota i scheduler
/health   - health check (DB, OANDA, Anthropic)
/scan     - ręczne skanowanie par
/last     - ostatni wygenerowany sygnał
```

## Krok 8: Przełączenie na live (po paper trading)
```bash
# Zmień OANDA_ENVIRONMENT w .env
OANDA_ENVIRONMENT=live   # zamiast: practice

# Restart
sudo systemctl restart smc-signal-bot
```

## Troubleshooting

### Bot nie startuje
```bash
sudo journalctl -u smc-signal-bot -n 50 --no-pager
```
Najczęstsze przyczyny: brak `.env`, błędny token Telegram, brak pip install.

### Webhook nie działa
- Sprawdź czy port 8443 jest otwarty: `ufw status`
- Sprawdź czy `WEBHOOK_URL` w `.env` zawiera `https://` i IP bez trailing slash
- Telegram wymaga HTTPS — upewnij się że cert.pem istnieje

### Paper trading nie generuje sygnałów
- Spread może blokować (MAX_SPREADS: EUR 2 pips, XAU 30 pips, BTC 50 pips)
- News blackout ±120 min od HIGH impact events
- Confluence score < 65 → None (normalnie 80–90% przypadków to None)
