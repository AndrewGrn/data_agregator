'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const QRCode = require('qrcode');
const { serializeMessage } = require('./serialize');
const { uploadMedia } = require('./media');

/**
 * One WhatsApp session. Publishes messages to wa.events.<accountId> and
 * session state to wa.status.<accountId>.
 */
function createClient({ accountId, sessionId, nc, sc }) {
  const client = new Client({
    authStrategy: new LocalAuth({ clientId: sessionId, dataPath: '/sessions' }),
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  });

  const publishStatus = (status, extra = {}) => {
    nc.publish(`wa.status.${accountId}`, sc.encode(JSON.stringify({ status, ...extra })));
  };

  client.on('qr', async (qr) => {
    const dataUrl = await QRCode.toDataURL(qr);
    publishStatus('qr', { qr_data_url: dataUrl });
  });
  client.on('authenticated', () => publishStatus('authenticated'));
  client.on('ready', () => publishStatus('ready'));
  client.on('disconnected', (reason) => publishStatus('disconnected', { reason: String(reason) }));

  const publishMessage = async (msg, js) => {
    const media = await uploadMedia(msg);
    let quotedMessageId = null;
    if (msg.hasQuotedMsg) {
      try {
        const quoted = await msg.getQuotedMessage();
        quotedMessageId = quoted?.id?._serialized ?? null;
      } catch { /* quoted message may be gone */ }
    }

    let authorName = null;
    try {
      const contact = await msg.getContact();
      authorName = contact?.pushname || contact?.name || null;
    } catch { /* contact may be unavailable */ }

    const event = serializeMessage(msg, {
      accountId,
      chatId: msg.from,
      authorName,
      media,
      quotedMessageId,
    });
    await js.publish(`wa.events.${accountId}`, sc.encode(JSON.stringify(event)));
  };

  return { client, publishMessage, publishStatus };
}

module.exports = { createClient };
