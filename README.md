# MCP Server Wrapper for LLM Integration

## Overview
This Node.js application serves as an MCP (Model Context Protocol) Server wrapper that integrates with Perplexity or other Large Language Models (LLMs) to provide inference capabilities for portfolio rebalancing. It runs inside a Docker container, ensuring easy deployment and scalability. This wrapper does not execute trades directly but instead supports generating portfolio rebalancing signals using state-of-the-art LLM-powered inference.

## Features
- Runs as an MCP Server in a Docker container
- Connects to LLM inference APIs like Perplexity or any other supported LLM
- Provides portfolio rebalancing recommendations based on LLM-generated analytics
- No trade execution; the system focuses on signal generation and analytics
- Modular and extensible Node.js codebase for easy customization

## Prerequisites
- Docker installed on your host machine
- Node.js (for local development and build)
- Perplexity desktop app or Claude 
- Configuration of API keys or tokens for the robinhood api

## Getting Started

### Build and Run with Docker
1. Clone My repository:

2. Run the container:

    docker compose up

# Note: Replace the environment variable values in the docker-compose.yml

### Authentication
Login is tried in this order, cheapest first:

1. **Access token**: the freshest of the stored session (`/root/.tokens/session.json`) and `ROBINHOOD_ACCESS_TOKEN`.
2. **Refresh token**: silent renewal with no password and no phone approval. Sources are the stored session, `ROBINHOOD_REFRESH_TOKEN`, and the robin_stocks pickle. It also runs on a 401 and when the token has less than 30 min left. Robinhood rotates the refresh token on every use, and the newest one is saved automatically.
3. **Username/password**: sends a device-approval push to the Robinhood app and is rate-limited by `ROBINHOOD_LOGIN_MIN_GAP_SEC`. Its refresh token is saved, so you only approve once.

Copy tokens with a "copy value" action, not from the DevTools header view. That view truncates long values with `…`, which the wrapper now rejects.

## References
- MCP Protocol and Server concepts
- Integration with Perplexity and other LLM APIs
- Portfolio rebalancing strategies with AI inference
- Perplexity Local MCP integration

## Architecture

- **Node.js MCP Server**: Implements MCP Protocol for communication.
- **LLM Integration Module**: Handles calls to external LLMs such as Perplexity for inference.
- **Docker Container**: Provides isolated, reproducible run environment.

## Development

### Running Locally
1. Install dependencies:
    
    npm install

2. Start the application:

    node index.js

### Testing
Run tests with:

    npx @modelcontextprotocol/inspector node dist/index.js

# Connecting a Locally Running MCP to Perplexity

###  Configure Perplexity to Connect to MCP
1. Go to below path
    Settings -> Connectors -> + Add Connector
2. Provide a Name "Robinhood" so your LLM targets the mcp 
3. Under advanced tab  
    ### Copy paste the perplexity.config.json content in Advanced tab in connectors 
    ### Update your Perplexity environment variables to point to your MCP instance
4. Save and exit. You should be able to see your MCP showing runnng with number of tools.

###  Start Using and happy trading 

Once connected, you can use Perplexity to send commands and queries to your local MCP seamlessly. and you should be able to see the mcp under sources

# Note: Replace the environment variable values in perplexity.config.json
