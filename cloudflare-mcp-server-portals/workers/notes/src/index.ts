// notes-mcp: an open (no auth) MCP server with three read-only tools.
// Stateless: a new McpServer per request, no Durable Object.
import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

const NOTES = [
  { id: "n1", title: "Portal hostname", body: "The portal lives on a zone in the same account." },
  { id: "n2", title: "Tool names", body: "Tools are prefixed with the server id." },
  { id: "n3", title: "Timeouts", body: "Upstream tool calls time out after 60 seconds." },
];

function createServer() {
  const server = new McpServer({ name: "notes", version: "1.0.0" });

  server.registerTool(
    "list_notes",
    { description: "List every note (id and title).", inputSchema: z.object({}) },
    async () => ({ content: [{ type: "text", text: JSON.stringify(NOTES.map(({ id, title }) => ({ id, title }))) }] }),
  );

  server.registerTool(
    "get_note",
    { description: "Get one note by id.", inputSchema: z.object({ id: z.string() }) },
    async ({ id }) => {
      const note = NOTES.find((n) => n.id === id);
      return note
        ? { content: [{ type: "text", text: JSON.stringify(note) }] }
        : { isError: true, content: [{ type: "text", text: `no note ${id}` }] };
    },
  );

  server.registerTool(
    "search_notes",
    { description: "Search note titles and bodies for a word.", inputSchema: z.object({ query: z.string() }) },
    async ({ query }) => {
      const q = query.toLowerCase();
      const hits = NOTES.filter((n) => (n.title + " " + n.body).toLowerCase().includes(q));
      return { content: [{ type: "text", text: JSON.stringify(hits) }] };
    },
  );

  return server;
}

const handler = createMcpHandler(createServer);

export default {
  fetch: (request: Request, env: unknown, ctx: ExecutionContext) => handler(request, env, ctx),
} satisfies ExportedHandler;
