# Telegram Moderator Bot - Docker Setup

A powerful Telegram moderation bot with subscription gates, content filters, and comprehensive admin tools.

## Features

- 📢 Subscription gate (require channel subscription before posting)
- 🛡️ Content filters (profanity, links, @mentions)
- ⚠️ Warning system with auto-mute/ban
- 📝 Comprehensive logging to database
- 🎮 Media permissions control
- 👮 Admin commands (!warn, !kick, !ban, !mute, etc.)
- 📊 Detailed logs viewable with !logs command

## Quick Start (Development)

### Prerequisites

- Docker and Docker Compose installed
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### Setup

1. **Clone or download the project**

2. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and add your bot token:
   ```
   BOT_TOKEN=your_bot_token_here
   ```

3. **Start the bot**
   ```bash
   docker compose up -d --build
   ```

4. **View logs**
   ```bash
   docker compose logs -f bot
   ```

5. **Stop the bot**
   ```bash
   docker compose down
   ```

## Production Deployment (Webhook Mode)

For production, use webhooks instead of polling for better performance and reliability.

### Prerequisites

- A domain name pointing to your server
- Open ports 80 and 443 on your server
- SSL certificate (automatically managed by Caddy)

### Setup

1. **Configure production environment**
   ```bash
   cp .env.example .env.prod
   ```
   
   Edit `.env.prod`:
   ```bash
   BOT_TOKEN=your_bot_token_here
   DATABASE_URL=postgresql+psycopg2://modbot:modbot@postgres:5432/modbot
   PUBLIC_URL=https://your-domain.com
   WEBHOOK_PATH=/webhook/secret-random-path
   WEBHOOK_SECRET=your-super-secret-token
   MODE=webhook
   ```

2. **Configure Caddy reverse proxy**
   
   Edit `Caddyfile` and replace `{your_domain}` with your actual domain:
   ```
   yourdomain.com {
       tls your@email.com
       ...
   }
   ```

3. **Start production stack**
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

4. **Check health**
   ```bash
   curl https://yourdomain.com/healthz
   ```

5. **View logs**
   ```bash
   docker compose -f docker-compose.prod.yml logs -f bot
   ```

## Commands

### User Commands
- `!help` - Show available commands
- `!rules` - Display chat rules
- `!me` - Check your warnings
- `!report` - Report a message (reply to message)

### Admin Commands
- `!warn @username [reason]` - Warn a user
- `!kick @username` - Kick a user
- `!ban @username [reason]` - Ban a user
- `!unban @username` - Unban a user
- `!mute @username [minutes]` - Mute a user
- `!unmute @username` - Unmute a user
- `!logs [count]` - View moderation logs
- `!setwarns <N>` - Set warning limit
- `!setmutetime <minutes>` - Set default mute duration
- `/settings` - View current settings
- `/setcaptcha @channel` - Set subscription requirement

## Configuration

### Database Options

**SQLite (Default - Development)**
```
DATABASE_URL=sqlite:///bot.db
```

**PostgreSQL (Recommended - Production)**
```
DATABASE_URL=postgresql+psycopg2://modbot:modbot@postgres:5432/modbot
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram bot token | Required |
| `DATABASE_URL` | Database connection string | `sqlite:///bot.db` |
| `MODE` | Bot mode (polling/webhook) | `polling` |
| `PUBLIC_URL` | Public URL for webhooks | - |
| `WEBHOOK_PATH` | Webhook endpoint path | `/webhook/{token}` |
| `WEBHOOK_SECRET` | Webhook secret token | - |
| `PORT` | Webhook server port | `8080` |

## Docker Commands

### Development

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f bot

# Stop
docker compose down

# Restart bot only
docker compose restart bot

# Rebuild after code changes
docker compose up -d --build bot
```

### Production

```bash
# Build and start
docker compose -f docker-compose.prod.yml up -d --build

# View logs
docker compose -f docker-compose.prod.yml logs -f bot

# Stop
docker compose -f docker-compose.prod.yml down

# Update after code changes
docker compose -f docker-compose.prod.yml up -d --build bot
```

## Backup and Restore

### Backup Database (SQLite)

```bash
docker compose cp bot:/app/bot.db ./backup-bot.db
```

### Backup Database (PostgreSQL)

```bash
docker compose exec postgres pg_dump -U modbot modbot > backup.sql
```

### Restore Database (PostgreSQL)

```bash
docker compose exec -T postgres psql -U modbot modbot < backup.sql
```

## Troubleshooting

### Bot not responding
1. Check logs: `docker compose logs -f bot`
2. Verify BOT_TOKEN in .env
3. Ensure bot is started: `docker compose ps`

### Database errors
1. Check database service: `docker compose ps postgres`
2. View database logs: `docker compose logs postgres`
3. Try recreating: `docker compose down -v && docker compose up -d`

### Webhook not working (production)
1. Verify domain points to your server
2. Check ports 80 and 443 are open
3. View Caddy logs: `docker compose -f docker-compose.prod.yml logs caddy`
4. Test webhook: `curl https://yourdomain.com/healthz`

## Security Notes

⚠️ **Important Security Practices:**

1. **Never commit `.env` or `.env.prod` to Git**
2. **Change default PostgreSQL password in production**
3. **Use strong WEBHOOK_SECRET in production**
4. **Keep your BOT_TOKEN private**
5. **Regularly update Docker images**

## Updating

To update the bot with new code:

```bash
# Development
git pull
docker compose up -d --build

# Production
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

## Support

For issues and questions, please check:
- Bot logs: `docker compose logs -f bot`
- [Aiogram Documentation](https://docs.aiogram.dev/)
- [Docker Documentation](https://docs.docker.com/)

## License

This project is provided as-is for educational and personal use.
