// catalog-mcp: 40 small tools (10 resources x list/get/create/update), to see what a big tool list costs in context.
import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

const RESOURCES = ["customers", "invoices", "orders", "products", "shipments", "refunds", "subscriptions", "coupons", "warehouses", "suppliers"];

function createServer() {
  const server = new McpServer({ name: "catalog", version: "1.0.0" });
  for (const r of RESOURCES) {
    const one = r.replace(/s$/, "");
    server.registerTool(`list_${r}`, { description: `List ${r}, newest first, with optional paging.`, inputSchema: z.object({ limit: z.number().optional(), cursor: z.string().optional() }) },
      async () => ({ content: [{ type: "text", text: JSON.stringify([{ id: `${one}_1` }, { id: `${one}_2` }]) }] }));
    server.registerTool(`get_${one}`, { description: `Get one ${one} by id.`, inputSchema: z.object({ id: z.string() }) },
      async ({ id }) => ({ content: [{ type: "text", text: JSON.stringify({ id, kind: one }) }] }));
    server.registerTool(`create_${one}`, { description: `Create a ${one} from a JSON body.`, inputSchema: z.object({ body: z.record(z.string(), z.unknown()) }) },
      async () => ({ content: [{ type: "text", text: JSON.stringify({ id: `${one}_new` }) }] }));
    server.registerTool(`update_${one}`, { description: `Update fields on a ${one}.`, inputSchema: z.object({ id: z.string(), fields: z.record(z.string(), z.unknown()) }) },
      async ({ id }) => ({ content: [{ type: "text", text: JSON.stringify({ id, updated: true }) }] }));
  }
  return server;
}

const handler = createMcpHandler(createServer);

export default {
  fetch: (request: Request, env: unknown, ctx: ExecutionContext) => handler(request, env, ctx),
} satisfies ExportedHandler;
