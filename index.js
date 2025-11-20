#!/usr/bin/env node

require('dotenv').config();
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { z } = require('zod');
const { spawn } = require('child_process');
const path = require('path');

const server = new McpServer({
    name: 'robinhood-mcp',
    version: '1.0.0',
});

const PYTHON_SCRIPT = path.join(__dirname, 'rh_wrapper.py');

let pythonProcess = null;
let commandQueue = [];

function getPythonProcess() {
    if (pythonProcess) return pythonProcess;

    pythonProcess = spawn('python3', [PYTHON_SCRIPT], {
        env: { ...process.env },
        stdio: ['pipe', 'pipe', 'pipe']
    });

    pythonProcess.stderr.on('data', (data) => {
        console.error(`Python stderr: ${data}`);
    });

    pythonProcess.on('exit', (code) => {
        console.error(`Python process exited with code ${code}`);
        pythonProcess = null;
        while (commandQueue.length > 0) {
            const pending = commandQueue.shift();
            pending.reject(new Error(`Python process exited unexpectedly with code ${code}`));
        }
    });

    let buffer = '';
    pythonProcess.stdout.on('data', (data) => {
        buffer += data.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop(); 

        for (const line of lines) {
            if (!line.trim()) continue;

            const pending = commandQueue.shift();
            if (pending) {
                try {
                    const result = JSON.parse(line);
                    if (result.status === 'error') {
                        pending.reject(new Error(result.error));
                    } else {
                        pending.resolve(result.data);
                    }
                } catch (e) {
                    pending.reject(new Error(`Failed to parse Python response: ${e.message}. Raw output: ${line}`));
                }
            } else {
                console.warn('Received unexpected Python output:', line);
            }
        }
    });

    return pythonProcess;
}

function runPythonCommand(command, args = []) {
    return new Promise((resolve, reject) => {
        const proc = getPythonProcess();
        commandQueue.push({ resolve, reject });

        const payload = JSON.stringify({ command, args }) + '\n';
        proc.stdin.write(payload);
    });
}

server.tool(
    'get_quote',
    'Get the latest stock quote for a symbol',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
    },
    async ({ symbol }) => {
        try {
            const quote = await runPythonCommand('quote', [symbol]);
            return {
                content: [{ type: 'text', text: JSON.stringify(quote, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching quote: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'get_portfolio',
    'Get portfolio summary',
    {},
    async () => {
        try {
            const portfolio = await runPythonCommand('portfolio');
            return {
                content: [{ type: 'text', text: JSON.stringify(portfolio, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching portfolio: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'get_positions',
    'Get open positions',
    {},
    async () => {
        try {
            const positions = await runPythonCommand('positions');
            return {
                content: [{ type: 'text', text: JSON.stringify(positions, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching positions: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'buy_stock',
    'Place a market buy order for a stock',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
        quantity: z.number().describe('Number of shares to buy'),
    },
    async ({ symbol, quantity }) => {
        try {
            const order = await runPythonCommand('buy', [symbol, quantity]);
            return {
                content: [{ type: 'text', text: JSON.stringify(order, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error placing buy order: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'sell_stock',
    'Place a market sell order for a stock',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
        quantity: z.number().describe('Number of shares to sell'),
    },
    async ({ symbol, quantity }) => {
        try {
            const order = await runPythonCommand('sell', [symbol, quantity]);
            return {
                content: [{ type: 'text', text: JSON.stringify(order, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error placing sell order: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'get_recent_orders',
    'Get a list of recent orders',
    {},
    async () => {
        try {
            const orders = await runPythonCommand('orders');
            return {
                content: [{ type: 'text', text: JSON.stringify(orders, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching recent orders: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'get_instrument_info',
    'Get basic instrument information by symbol',
    {
        symbol: z.string().describe('The stock symbol (e.g., AAPL)'),
    },
    async ({ symbol }) => {
        try {
            const info = await runPythonCommand('instrument', [symbol]);
            return {
                content: [{ type: 'text', text: JSON.stringify(info, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching instrument info: ${error.message}` }],
                isError: true,
            };
        }
    }
);

server.tool(
    'get_watchlists',
    'Get user watchlists',
    {},
    async () => {
        try {
            const watchlists = await runPythonCommand('watchlists');
            return {
                content: [{ type: 'text', text: JSON.stringify(watchlists, null, 2) }],
            };
        } catch (error) {
            return {
                content: [{ type: 'text', text: `Error fetching watchlists: ${error.message}` }],
                isError: true,
            };
        }
    }
);

async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error('Robinhood MCP Server running on stdio');
}

main().catch((error) => {
    console.error('Fatal error in main():', error);
    process.exit(1);
});
