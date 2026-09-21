'use strict';

const CONTRACT_VERSION = 1;

/**
 * Build the wire object for wa.events.<account_id>.
 * Media bytes never travel here — only the sha256 of an already-uploaded file.
 */
function serializeMessage(msg, { accountId, chatId, authorName, media, quotedMessageId }) {
  return {
    v: CONTRACT_VERSION,
    account_id: accountId,
    chat_id: chatId,
    message_id: msg.id && msg.id._serialized ? msg.id._serialized : null,
    timestamp: msg.timestamp ?? null,
    from: msg.from ?? null,
    author: msg.author ?? msg.from ?? null,
    author_name: authorName ?? null,
    body: msg.body ?? '',
    type: msg.type ?? null,
    has_quoted: Boolean(msg.hasQuotedMsg),
    quoted_message_id: quotedMessageId ?? null,
    media: media ?? null,
    raw: JSON.parse(JSON.stringify(msg)),
  };
}

module.exports = { serializeMessage, CONTRACT_VERSION };
