'use strict';

const { connect, StringCodec } = require('nats');
const { Client: PgClient } = require('pg');
const { createClient } = require('./client');
const { serializeParticipants } = require('./serialize');

const sc = StringCodec();

async function loadAccounts() {
  const pg = new PgClient({ connectionString: process.env.DATABASE_URL_PG });
  await pg.connect();
  try {
    const { rows } = await pg.query(
      "SELECT id, credentials FROM parser_accounts WHERE parser_type = 'whatsapp' AND is_active = true",
    );
    return rows
      .map((row) => ({ accountId: row.id, sessionId: row.credentials?.session_id }))
      .filter((entry) => Boolean(entry.sessionId));
  } finally {
    await pg.end();
  }
}

async function main() {
  const nc = await connect({ servers: process.env.NATS_URL || 'nats://nats:4222' });
  const js = nc.jetstream();
  const accounts = await loadAccounts();
  console.log(`[bridge] starting ${accounts.length} session(s)`);

  for (const { accountId, sessionId } of accounts) {
    const { client, publishMessage } = createClient({ accountId, sessionId, nc, sc });

    client.on('message', (msg) => publishMessage(msg, js).catch(
      (err) => console.error(`[bridge] publish failed: ${err.message}`),
    ));
    // message_create also yields messages sent by this account itself.
    client.on('message_create', (msg) => {
      if (!msg.fromMe) return;
      publishMessage(msg, js).catch((err) => console.error(`[bridge] publish failed: ${err.message}`));
    });

    const backfill = nc.subscribe(`wa.backfill.${accountId}`);
    (async () => {
      for await (const request of backfill) {
        const { chat_id: chatId, limit } = JSON.parse(sc.decode(request.data));
        try {
          const chat = await client.getChatById(chatId);
          const messages = await chat.fetchMessages({ limit: Number(limit) || 200 });
          for (const msg of messages) {
            await publishMessage(msg, js);
          }
          console.log(`[bridge] backfilled ${messages.length} from ${chatId}`);
        } catch (err) {
          console.error(`[bridge] backfill ${chatId} failed: ${err.message}`);
        }
      }
    })();

    const participants = nc.subscribe(`wa.participants.${accountId}`);
    (async () => {
      for await (const request of participants) {
        const { chat_id: chatId } = JSON.parse(sc.decode(request.data));
        try {
          const chat = await client.getChatById(chatId);
          request.respond(sc.encode(JSON.stringify(serializeParticipants(chat))));
        } catch (err) {
          request.respond(sc.encode(JSON.stringify({ participants: [], error: err.message })));
        }
      }
    })();

    await client.initialize();
  }
}

main().catch((err) => {
  console.error(`[bridge] fatal: ${err.message}`);
  process.exit(1);
});
