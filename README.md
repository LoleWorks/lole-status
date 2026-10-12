# Lole Status

One Telegram bot for the monitoring of every Lole Works client. The bot lives in one Telegram group with **Topics**
(Lole Works team only): one topic per client, plus **General** for the overview.

Each client server runs its own monitor (for BM Public: `bm-bi/monitor`). The monitor checks the server, Docker, the
database, the data loads and the connections, and sends its alerts and a heartbeat (its current state) to this hub
with its own key. The hub posts the alerts in the client's topic, notices when a client's monitor goes silent (server,
Docker or network down: `[TĂCERE]`, then `[REVENIT]`), answers commands and posts a daily overview in General.
Only the hub holds the bot token; a client server only has its own key, which can be rotated or disabled here.

Commands: in a client topic `/status`, `/surse`, `/server`; in General `/clienti`; `/ajutor` everywhere.

## Setup

1. Telegram: create the bot with @BotFather (`/newbot`, name "Lole Status"); create a group "Lole Status", turn on
   **Topics** (group settings), add the bot and make it **admin** with the right to manage topics.
2. Portainer: new stack `lole-status` from this repository, variable `TELEGRAM_BOT_TOKEN`; deploy.
3. Write `/start` in the group: the bot answers with the group id (until it is set). Put it in `TELEGRAM_GROUP_ID`, redeploy.
4. For each client:
   `docker exec -it lole-status-status-hub-1 python hub.py add-client "BM Public"`
   creates the topic and prints the client's key once. Put it in the client's monitor as `STATUS_HUB_KEY`
   (and `STATUS_HUB_URL`, e.g. `http://status-hub:8080` on the same server).

Other commands: `python hub.py list-clients`, `rotate-key <id>`, `disable-client <id>`, `enable-client <id>`.

## Where it runs

Today on BM Public's VM (`vm-psql`), on the Docker network `bm-bi`, without a public port. Limits of that choice:
if that VM stops, the hub stops too (BM's own outage cannot be reported from there), and monitors of other clients
cannot reach it until it is published behind HTTPS. Moving it to a Lole Works VPS is the same stack, deployed there.
