#!/usr/bin/env node

require('dotenv').config({ quiet: true });
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { z } = require('zod');
const { spawn } = require('child_process');
const path = require('path');

const server = new McpServer({
    name: 'robinhood-mcp',
    version: '1.1.0',
});

const PYTHON_SCRIPT = path.join(__dirname, 'rh_wrapper.py');

// ── Resource limits ──────────────────────────────────────────────────
const REQUEST_TIMEOUT_MS = 30_000;
const MAX_PENDING = 200;
const MAX_BUFFER_BYTES = 16 * 1024 * 1024; // 16 MB

// ── Subprocess + pending-request state ───────────────────────────────
let pythonProcess = null;
let stdoutBuffer = '';
let nextRequestId = 1;
const pending = new Map(); // id -> { resolve, reject, timer }
let isShuttingDown = false;

function rejectAllPending(err) {
    for (const [id, entry] of pending) {
        clearTimeout(entry.timer);
        try { entry.reject(err); } catch (_) {}
        pending.delete(id);
    }
}

function spawnPython() {
    const proc = spawn('python3', [PYTHON_SCRIPT], {
        env: { ...process.env },
        stdio: ['pipe', 'pipe', 'pipe'],
    });

    proc.stderr.on('data', (data) => {
        process.stderr.write(`[python] ${data}`);
    });

    proc.on('error', (err) => {
        console.error(`Python process error: ${err.message}`);
        rejectAllPending(new Error(`Python process error: ${err.message}`));
    });

    proc.on('exit', (code, signal) => {
        const reason = signal ? `signal ${signal}` : `code ${code}`;
        console.error(`Python process exited (${reason})`);
        if (proc === pythonProcess) {
            pythonProcess = null;
            stdoutBuffer = '';
            rejectAllPending(new Error(`Python process exited unexpectedly (${reason})`));
        }
    });

    proc.stdout.on('data', (data) => {
        stdoutBuffer += data.toString();

        if (stdoutBuffer.length > MAX_BUFFER_BYTES) {
            console.error(`Python stdout buffer exceeded ${MAX_BUFFER_BYTES} bytes, killing process`);
            stdoutBuffer = '';
            rejectAllPending(new Error('Python response exceeded buffer cap (16 MB)'));
            try { proc.kill('SIGKILL'); } catch (_) {}
            return;
        }

        const lines = stdoutBuffer.split('\n');
        stdoutBuffer = lines.pop() || '';

        for (const line of lines) {
            if (!line.trim()) continue;
            let parsed;
            try {
                parsed = JSON.parse(line);
            } catch (e) {
                console.error(`Failed to parse Python response: ${e.message}`);
                continue;
            }

            const id = parsed.id;
            if (id == null) {
                console.error('Received Python output with no id:', line.slice(0, 200));
                continue;
            }
            const entry = pending.get(id);
            if (!entry) {
                // Likely a late response after timeout — ignore.
                continue;
            }
            clearTimeout(entry.timer);
            pending.delete(id);

            if (parsed.status === 'error') {
                entry.reject(new Error(parsed.error || 'Unknown Python error'));
            } else {
                entry.resolve(parsed.data);
            }
        }
    });

    return proc;
}

function getPythonProcess() {
    if (pythonProcess && !pythonProcess.killed && pythonProcess.exitCode === null) {
        return pythonProcess;
    }
    pythonProcess = spawnPython();
    return pythonProcess;
}

function runPythonCommand(command, args = []) {
    return new Promise((resolve, reject) => {
        if (isShuttingDown) {
            return reject(new Error('Server is shutting down'));
        }
        if (pending.size >= MAX_PENDING) {
            return reject(new Error(`Too many in-flight requests (${pending.size}); back off and retry`));
        }

        const proc = getPythonProcess();
        if (!proc.stdin || !proc.stdin.writable) {
            return reject(new Error('Python stdin is not writable'));
        }

        const id = nextRequestId++;
        const timer = setTimeout(() => {
            if (pending.delete(id)) {
                reject(new Error(`Robinhood tool call '${command}' timed out after ${REQUEST_TIMEOUT_MS / 1000}s`));
            }
        }, REQUEST_TIMEOUT_MS);
        // Don't let an idle timer keep the event loop alive past shutdown.
        if (typeof timer.unref === 'function') timer.unref();

        pending.set(id, { resolve, reject, timer });

        const payload = JSON.stringify({ id, command, args }) + '\n';
        proc.stdin.write(payload, (err) => {
            if (err && pending.delete(id)) {
                clearTimeout(timer);
                reject(new Error(`Failed to write to Python stdin: ${err.message}`));
            }
        });
    });
}

// ── Helper to wrap tool handlers in compact-JSON + error formatting ──

function makeHandler(command, label, mapArgs = () => []) {
    return async (input) => {
        try {
            const data = await runPythonCommand(command, mapArgs(input));
            return { content: [{ type: 'text', text: JSON.stringify(data) }] };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error ${label}: ${error.message}` }],
                isError: true,
            };
        }
    };
}

// ── Tools ────────────────────────────────────────────────────────────

server.tool(
    'get_quote',
    'Get the latest stock quote for a symbol',
    { symbol: z.string().describe('The stock symbol (e.g., AAPL)') },
    makeHandler('quote', 'fetching quote', ({ symbol }) => [symbol])
);

server.tool(
    'get_portfolio',
    'Get portfolio summary',
    {},
    makeHandler('portfolio', 'fetching portfolio')
);

server.tool(
    'get_positions',
    'Get open positions',
    {},
    makeHandler('positions', 'fetching positions')
);

server.tool(
    'buy_stock',
    'Place a market buy order for a stock',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
        quantity: z.number().describe('Number of shares to buy'),
    },
    makeHandler('buy', 'placing buy order', ({ symbol, quantity }) => [symbol, quantity])
);

server.tool(
    'sell_stock',
    'Place a market sell order for a stock',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
        quantity: z.number().describe('Number of shares to sell'),
    },
    makeHandler('sell', 'placing sell order', ({ symbol, quantity }) => [symbol, quantity])
);

server.tool(
    'get_recent_orders',
    'Get a list of recent orders',
    {},
    makeHandler('orders', 'fetching recent orders')
);

server.tool(
    'get_instrument_info',
    'Get basic instrument information by symbol',
    { symbol: z.string().describe('The stock symbol (e.g., AAPL)') },
    makeHandler('instrument', 'fetching instrument info', ({ symbol }) => [symbol])
);

server.tool(
    'get_watchlists',
    'Get user watchlists',
    {},
    makeHandler('watchlists', 'fetching watchlists')
);

server.tool(
    'get_stock_historicals',
    'Get historical OHLCV candle data for a stock',
    {
        symbol: z.string().describe('Stock symbol (e.g., SPY, AAPL)'),
        interval: z.string().describe('Candle interval: 5minute, 10minute, hour, day, week').default('hour'),
        span: z.string().describe('Time span: day, week, month, 3month, year, 5year').default('month'),
    },
    makeHandler('historicals', 'fetching historicals', ({ symbol, interval, span }) => [symbol, interval, span])
);

server.tool(
    'get_crypto_historicals',
    'Get historical OHLCV candle data for crypto',
    {
        symbol: z.string().describe('Crypto symbol (e.g., BTC, ETH)'),
        interval: z.string().describe('Candle interval: 5minute, 10minute, hour, day, week').default('hour'),
        span: z.string().describe('Time span: day, week, month, 3month, year, 5year').default('month'),
    },
    makeHandler('crypto_historicals', 'fetching crypto historicals', ({ symbol, interval, span }) => [symbol, interval, span])
);

server.tool(
    'get_fundamentals',
    'Get stock fundamentals (market cap, P/E, EPS, etc.)',
    { symbol: z.string().describe('Stock symbol (e.g., AAPL)') },
    makeHandler('fundamentals', 'fetching fundamentals', ({ symbol }) => [symbol])
);

server.tool(
    'get_earnings',
    'Get earnings data for a stock',
    { symbol: z.string().describe('Stock symbol (e.g., AAPL)') },
    makeHandler('earnings', 'fetching earnings', ({ symbol }) => [symbol])
);

server.tool(
    'get_news',
    'Get latest news articles for a stock',
    { symbol: z.string().describe('Stock symbol (e.g., AAPL)') },
    makeHandler('news', 'fetching news', ({ symbol }) => [symbol])
);

server.tool(
    'get_top_movers',
    'Get top S&P 500 movers (up or down)',
    { direction: z.string().describe('Direction: up or down').default('up') },
    makeHandler('movers', 'fetching movers', ({ direction }) => [direction])
);

// ── Options Tools ────────────────────────────────────────────────────────

server.tool(
    'get_option_chain',
    'Get option chain info for a symbol (available expirations, strike range)',
    { symbol: z.string().describe('Stock symbol (e.g., GOOG, SPY)') },
    makeHandler('option_chain', 'fetching option chain', ({ symbol }) => [symbol])
);

server.tool(
    'get_option_expirations',
    'Get available expiration dates for a symbol',
    { symbol: z.string().describe('Stock symbol (e.g., GOOG, SPY)') },
    makeHandler('option_expirations', 'fetching option expirations', ({ symbol }) => [symbol])
);

server.tool(
    'get_options_by_expiration',
    'Get all options (with prices, greeks, IV) for a symbol at a specific expiration',
    {
        symbol: z.string().describe('Stock symbol (e.g., GOOG)'),
        expiration: z.string().describe('Expiration date YYYY-MM-DD (e.g., 2026-05-01)'),
        option_type: z.string().describe('Option type: call or put').default('call'),
    },
    makeHandler('option_find', 'fetching options by expiration',
        ({ symbol, expiration, option_type }) => [symbol, expiration, option_type])
);

server.tool(
    'get_option_quote',
    'Get live quote + greeks for a specific option (symbol, expiration, strike, type)',
    {
        symbol: z.string().describe('Stock symbol (e.g., GOOG)'),
        expiration: z.string().describe('Expiration date YYYY-MM-DD'),
        strike: z.string().describe('Strike price (e.g., 275)'),
        option_type: z.string().describe('call or put').default('call'),
    },
    makeHandler('option_quote', 'fetching option quote',
        ({ symbol, expiration, strike, option_type }) => [symbol, expiration, strike, option_type])
);

server.tool(
    'get_option_market_data',
    'Get full market data (bid/ask, greeks, IV, volume) for a specific option',
    {
        symbol: z.string().describe('Stock symbol (e.g., GOOG)'),
        expiration: z.string().describe('Expiration date YYYY-MM-DD'),
        strike: z.string().describe('Strike price (e.g., 275)'),
        option_type: z.string().describe('call or put').default('call'),
    },
    makeHandler('option_market_data', 'fetching option market data',
        ({ symbol, expiration, strike, option_type }) => [symbol, expiration, strike, option_type])
);

server.tool(
    'get_option_positions',
    'Get all open option positions in the account',
    {},
    makeHandler('option_positions', 'fetching option positions')
);

// ── Graceful shutdown ────────────────────────────────────────────────

function shutdown(signal) {
    if (isShuttingDown) return;
    isShuttingDown = true;
    console.error(`Received ${signal}, shutting down Robinhood MCP...`);

    rejectAllPending(new Error('Server shutting down'));

    if (pythonProcess && pythonProcess.exitCode === null) {
        const child = pythonProcess;
        try { child.stdin.end(); } catch (_) {}
        try { child.kill('SIGTERM'); } catch (_) {}
        const killTimer = setTimeout(() => {
            if (child.exitCode === null) {
                try { child.kill('SIGKILL'); } catch (_) {}
            }
            process.exit(0);
        }, 2000);
        if (typeof killTimer.unref === 'function') killTimer.unref();
        child.on('exit', () => process.exit(0));
    } else {
        process.exit(0);
    }
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGHUP', () => shutdown('SIGHUP'));

process.on('uncaughtException', (err) => {
    console.error('Uncaught exception:', err);
    shutdown('uncaughtException');
});
process.on('unhandledRejection', (reason) => {
    console.error('Unhandled rejection:', reason);
});

async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error('Robinhood MCP Server v1.1.0 running on stdio');
}

main().catch((error) => {
    console.error('Fatal error in main():', error);
    process.exit(1);
});
