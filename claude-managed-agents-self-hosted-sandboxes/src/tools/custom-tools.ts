import { z } from "zod";
import { defineTool, type CustomTool } from "./custom-tools-runtime";
import { extractPdfText } from "./pdf-text";

/**
 * Companion to the my2cents.ai post on Claude Managed Agents self-hosted
 * sandboxes on Cloudflare. Demonstrates the inline-bindings differentiator:
 * a custom tool that reaches an R2 bucket through a Worker binding rather
 * than over HTTP.
 *
 * Required wrangler.jsonc binding:
 *
 *   "r2_buckets": [
 *     {
 *       "binding": "PDFS",
 *       "bucket_name": "claude-managed-agents-pdfs"
 *     }
 *   ]
 */
export const CUSTOM_TOOLS: CustomTool[] = [
  defineTool({
    name: "cf_list_pdfs",
    description:
      "List PDF objects in the configured R2 bucket. Returns key and size in bytes (tab-separated) for each object.",
    inputSchema: z.object({
      prefix: z
        .string()
        .optional()
        .describe("Optional R2 key prefix to filter by, e.g. 'pdfs/'"),
    }),
    requires: (env) =>
      Boolean((env as unknown as { PDFS?: unknown }).PDFS),
    run: async ({ prefix }, { env }) => {
      const pdfs = (env as unknown as { PDFS: R2Bucket }).PDFS;
      const listed = await pdfs.list({ prefix });
      const rows = listed.objects.map((o) => `${o.key}\t${o.size}`);
      return rows.length ? rows.join("\n") : "(no objects found)";
    },
  }),

  defineTool({
    name: "cf_read_pdf",
    description:
      "Fetch a PDF from R2 by key and return its extracted text. Call cf_list_pdfs first to discover keys.",
    inputSchema: z.object({
      key: z
        .string()
        .describe("R2 object key, e.g. pdfs/q1-2026.pdf"),
    }),
    requires: (env) =>
      Boolean((env as unknown as { PDFS?: unknown }).PDFS),
    run: async ({ key }, { env }) => {
      const pdfs = (env as unknown as { PDFS: R2Bucket }).PDFS;
      const obj = await pdfs.get(key);
      if (!obj) return `error: no object at key ${key}`;
      const buffer = await obj.arrayBuffer();
      const text = await extractPdfText(buffer);
      return text || "(no extractable text)";
    },
  }),
];
