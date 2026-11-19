// tickets-mcp: a support-ticket MCP server with read and write tools, one of them dangerous.
// Stateless. Tickets live in memory for the life of the isolate, which is enough for the tests.
// The code is identical whether it runs open (the bypass test) or behind a self-hosted Access app (the fix).
import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

type Ticket = { id: number; subject: string; status: "open" | "closed" };
const tickets: Ticket[] = [
  { id: 1, subject: "Login page returns 500", status: "open" },
  { id: 2, subject: "Invoice PDF is blank", status: "open" },
  { id: 3, subject: "Cannot reset password", status: "closed" },
];
let nextId = 4;

const text = (t: unknown) => ({ content: [{ type: "text" as const, text: typeof t === "string" ? t : JSON.stringify(t) }] });

function createServer() {
  const server = new McpServer({ name: "tickets", version: "1.0.0" });

  server.registerTool(
    "list_tickets",
    { description: "List tickets, optionally only open ones.", inputSchema: z.object({ open_only: z.boolean().optional() }) },
    async ({ open_only }) => text(tickets.filter((t) => !open_only || t.status === "open")),
  );

  server.registerTool(
    "create_ticket",
    { description: "Create a ticket.", inputSchema: z.object({ subject: z.string() }) },
    async ({ subject }) => {
      const t: Ticket = { id: nextId++, subject, status: "open" };
      tickets.push(t);
      return text(t);
    },
  );

  server.registerTool(
    "delete_ticket",
    { description: "Permanently delete a ticket. Destructive.", inputSchema: z.object({ id: z.number() }) },
    async ({ id }) => {
      const i = tickets.findIndex((t) => t.id === id);
      if (i < 0) return { isError: true, ...text(`no ticket ${id}`) };
      tickets.splice(i, 1);
      return text(`deleted ${id}`);
    },
  );

  // Used by the failure tests: sleeps, to run into the portal's 60 second upstream timeout.
  server.registerTool(
    "slow_report",
    { description: "Build a report. Takes the given number of seconds.", inputSchema: z.object({ seconds: z.number().max(120) }) },
    async ({ seconds }) => {
      await new Promise((r) => setTimeout(r, seconds * 1000));
      return text(`report ready after ${seconds}s`);
    },
  );

  return server;
}

const handler = createMcpHandler(createServer);

export default {
  fetch: (request: Request, env: unknown, ctx: ExecutionContext) => handler(request, env, ctx),
} satisfies ExportedHandler;
