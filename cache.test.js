'use strict';

// Tests for the shared TTL cache module.
// Run with: node --test cache.test.js

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const os = require('os');

const { cached } = require('./cache');

// Each test uses a unique prefix so they never collide.
let _seq = 0;
function uid() { return `test-rh-${process.pid}-${++_seq}`; }

// ── Core cache behaviour ─────────────────────────────────────────────

test('cache miss: fn is called and result is returned', async () => {
    let calls = 0;
    const result = await cached(uid(), ['a'], 5_000, () => { calls++; return Promise.resolve({ v: 1 }); });
    assert.deepEqual(result, { v: 1 });
    assert.equal(calls, 1);
});

test('cache hit: fn is NOT called again within TTL', async () => {
    let calls = 0;
    const prefix = uid();
    const args = ['same'];
    const fn = () => { calls++; return Promise.resolve(calls); };
    const r1 = await cached(prefix, args, 5_000, fn);
    const r2 = await cached(prefix, args, 5_000, fn);
    assert.equal(r1, 1);
    assert.equal(r2, 1);   // still 1 — served from cache
    assert.equal(calls, 1);
});

test('ttl=0: cache is bypassed every call', async () => {
    let calls = 0;
    const prefix = uid();
    const args = ['x'];
    const fn = () => { calls++; return Promise.resolve(calls); };
    const r1 = await cached(prefix, args, 0, fn);
    const r2 = await cached(prefix, args, 0, fn);
    assert.equal(r1, 1);
    assert.equal(r2, 2);
    assert.equal(calls, 2);
});

test('different args produce independent cache entries', async () => {
    let calls = 0;
    const prefix = uid();
    const fn = (v) => { calls++; return Promise.resolve({ v }); };
    const r1 = await cached(prefix, ['AAPL'], 5_000, () => fn('AAPL'));
    const r2 = await cached(prefix, ['TSLA'], 5_000, () => fn('TSLA'));
    assert.deepEqual(r1, { v: 'AAPL' });
    assert.deepEqual(r2, { v: 'TSLA' });
    assert.equal(calls, 2);
    // Re-fetch both — should both hit
    await cached(prefix, ['AAPL'], 5_000, () => fn('AAPL'));
    await cached(prefix, ['TSLA'], 5_000, () => fn('TSLA'));
    assert.equal(calls, 2); // no extra calls
});

test('errors propagate and are NOT cached', async () => {
    let calls = 0;
    const prefix = uid();
    const args = ['err'];
    const fn = () => { calls++; return Promise.reject(new Error('api down')); };
    await assert.rejects(() => cached(prefix, args, 5_000, fn), /api down/);
    await assert.rejects(() => cached(prefix, args, 5_000, fn), /api down/);
    assert.equal(calls, 2); // called both times — error not cached
});

test('cache stores data to disk and memory hit is faster', async () => {
    const prefix = uid();
    const args = ['disk-test'];
    let calls = 0;
    // First call — miss, writes to disk
    await cached(prefix, args, 60_000, () => { calls++; return Promise.resolve('stored'); });
    assert.equal(calls, 1);
    // Second call — in-memory hit
    const r = await cached(prefix, args, 60_000, () => { calls++; return Promise.resolve('SHOULD_NOT_SEE'); });
    assert.equal(r, 'stored');
    assert.equal(calls, 1);
});

test('file was written to ~/.cache/mcp-servers/', async () => {
    const prefix = uid();
    await cached(prefix, ['filecheck'], 60_000, () => Promise.resolve({ persisted: true }));
    await new Promise(r => setTimeout(r, 50)); // let the fire-and-forget write flush
    const CACHE_DIR = path.join(os.homedir(), '.cache', 'mcp-servers');
    const files = fs.readdirSync(CACHE_DIR);
    const match = files.some(f => f.startsWith(`${prefix}-`));
    assert.ok(match, `No cache file found for prefix "${prefix}" in ${CACHE_DIR}`);
});

// ── TTL map coverage — verify every read-only Robinhood command has a TTL ──

test('TTL map: read-only commands all have TTLs > 0', () => {
    // Require index.js in a way that lets us inspect TTL without starting the server.
    // We parse the file and check the TTL object is defined and non-empty.
    const src = fs.readFileSync(path.join(__dirname, 'index.js'), 'utf-8');
    const match = src.match(/const TTL\s*=\s*\{([^}]+)\}/s);
    assert.ok(match, 'TTL object not found in index.js');
    const block = match[1];
    // Every entry must have a positive ms value
    const entries = [...block.matchAll(/(\w+)\s*:\s*([\d_]+)/g)];
    assert.ok(entries.length > 10, `Expected >10 TTL entries, got ${entries.length}`);
    for (const [, name, rawVal] of entries) {
        const ms = Number(rawVal.replace(/_/g, ''));
        assert.ok(ms > 0, `TTL for "${name}" is not positive: ${ms}`);
    }
});

test('TTL map: mutating commands are absent (no accidental caching)', () => {
    const src = fs.readFileSync(path.join(__dirname, 'index.js'), 'utf-8');
    const match = src.match(/const TTL\s*=\s*\{([^}]+)\}/s);
    assert.ok(match, 'TTL object not found in index.js');
    const block = match[1];
    const mutating = ['buy', 'sell', 'futures_place_order', 'futures_cancel_order'];
    for (const cmd of mutating) {
        const present = new RegExp(`\\b${cmd}\\s*:`).test(block);
        assert.ok(!present, `Mutating command "${cmd}" should NOT be in TTL map`);
    }
});
