'use strict';

const crypto = require('node:crypto');
const { S3Client, PutObjectCommand, HeadObjectCommand } = require('@aws-sdk/client-s3');

const BUCKET = process.env.S3_BUCKET || 'raw-events';
const MAX_BYTES = Number(process.env.MEDIA_MAX_BYTES || 52428800);

const s3 = new S3Client({
  endpoint: process.env.S3_ENDPOINT_URL || 'http://minio:9000',
  region: process.env.S3_REGION || 'us-east-1',
  credentials: {
    accessKeyId: process.env.S3_ACCESS_KEY || 'minio',
    secretAccessKey: process.env.S3_SECRET_KEY || 'miniosecret',
  },
  forcePathStyle: true,
});

/**
 * Upload an attachment under media/<sha256>, matching the Python side's key
 * so the same file from any source is stored once.
 * Returns null when there is no media, it is too large, or S3 is unavailable.
 */
async function uploadMedia(msg) {
  if (!msg.hasMedia) return null;

  let media;
  try {
    media = await msg.downloadMedia();
  } catch (err) {
    console.error(`[media] download failed for ${msg.id?._serialized}: ${err.message}`);
    return null;
  }
  if (!media || !media.data) return null;

  const bytes = Buffer.from(media.data, 'base64');
  if (bytes.length > MAX_BYTES) {
    console.warn(`[media] skipped ${bytes.length} bytes, over the limit`);
    return null;
  }

  const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  const key = `media/${sha256}`;

  try {
    try {
      await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key }));
    } catch {
      await s3.send(new PutObjectCommand({
        Bucket: BUCKET,
        Key: key,
        Body: bytes,
        ContentType: media.mimetype || 'application/octet-stream',
      }));
    }
  } catch (err) {
    // ponytail: без ретрая, событие сохраняется без файла
    console.error(`[media] upload ${key} failed: ${err.message}`);
    return null;
  }

  return {
    sha256,
    mime: media.mimetype || null,
    size: bytes.length,
    filename: media.filename || null,
  };
}

module.exports = { uploadMedia };
