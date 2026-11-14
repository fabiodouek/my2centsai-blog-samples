# Cloudflare MCP Server Portals: companion code

Work in progress. Two small stateless MCP servers for Cloudflare Workers (`workers/notes`, `workers/tickets`)
built with `createMcpHandler`, used as the upstream servers behind a portal. The scripts that create and test the
portal are added once the runs are done.

```bash
cd workers/notes && npm install && npx wrangler dev   # http://localhost:8787/mcp
```
