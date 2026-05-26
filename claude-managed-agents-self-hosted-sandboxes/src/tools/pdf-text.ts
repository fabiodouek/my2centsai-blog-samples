/**
 * Lightweight PDF text extractor for the Workers runtime.
 *
 * Approach: scan the PDF byte stream for content streams ("stream ... endstream"),
 * decompress any that carry the /FlateDecode filter (using the Web Standard
 * DecompressionStream available in Workers, browsers, and Node 18+),
 * then walk each decoded stream looking for text-showing operators ("Tj" / "TJ")
 * and concatenate the strings.
 *
 * This handles the common case: text-based PDFs from tools like LaTeX, Word,
 * Pages, browser print-to-PDF, GAO/government PDF generators, etc., all of
 * which Flate-compress their content streams.
 *
 * It does NOT handle:
 *   - image-only / scanned PDFs (no text operators)
 *   - encrypted streams
 *   - filter chains other than a single /FlateDecode
 *   - PNG-style predictors (only used in image streams, not text)
 *   - CMap/ToUnicode glyph remapping (raw glyph codes are returned for
 *     non-standard subset fonts)
 *
 * For those, route the buffer through Workers AI's vision models. That
 * path is intentionally out of scope here to keep the demo simple.
 */

const STREAM_START = new TextEncoder().encode("stream");
const STREAM_END = new TextEncoder().encode("endstream");
const TEXT_OPS = /\(((?:\\.|[^\\()])*)\)\s*Tj/g;
const TEXT_ARRAY_OPS = /\[((?:\\.|[^\\\[\]])*)\]\s*TJ/g;
const FILTER_DICT_RE = /\/Filter\s*(?:\[\s*([^\]]+?)\s*\]|(\/\w+))/;

export async function extractPdfText(buffer: ArrayBuffer): Promise<string> {
  const bytes = new Uint8Array(buffer);

  const streams = await collectContentStreams(bytes);
  if (streams.length === 0) return "";

  const pieces: string[] = [];
  for (const stream of streams) {
    pieces.push(...extractStringsFromStream(stream));
  }

  return pieces
    .join("\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

async function collectContentStreams(bytes: Uint8Array): Promise<string[]> {
  const out: string[] = [];
  let cursor = 0;

  while (cursor < bytes.length) {
    const startIdx = indexOf(bytes, STREAM_START, cursor);
    if (startIdx < 0) break;

    const dictStart = lastIndexOf(bytes, 0x3c /* '<' */, startIdx, 2);
    const dictBytes = dictStart >= 0 ? bytes.subarray(dictStart, startIdx) : new Uint8Array();
    const dictText = new TextDecoder("latin1").decode(dictBytes);
    const usesFlate = hasFlateFilter(dictText);

    let streamDataStart = startIdx + STREAM_START.length;
    if (bytes[streamDataStart] === 0x0d) streamDataStart++;
    if (bytes[streamDataStart] === 0x0a) streamDataStart++;

    const endIdx = indexOf(bytes, STREAM_END, streamDataStart);
    if (endIdx < 0) break;

    let streamDataEnd = endIdx;
    if (bytes[streamDataEnd - 1] === 0x0a) streamDataEnd--;
    if (bytes[streamDataEnd - 1] === 0x0d) streamDataEnd--;

    const streamBytes = bytes.subarray(streamDataStart, streamDataEnd);

    let decoded: string;
    if (usesFlate) {
      try {
        decoded = await inflate(streamBytes);
      } catch {
        decoded = new TextDecoder("latin1").decode(streamBytes);
      }
    } else {
      decoded = new TextDecoder("latin1").decode(streamBytes);
    }

    out.push(decoded);
    cursor = endIdx + STREAM_END.length;
  }

  return out;
}

function hasFlateFilter(dictText: string): boolean {
  const match = FILTER_DICT_RE.exec(dictText);
  if (!match) return false;
  const filters = match[1] ?? match[2] ?? "";
  return filters.includes("/FlateDecode") || filters.includes("/Fl");
}

async function inflate(bytes: Uint8Array): Promise<string> {
  const stream = new Response(bytes).body!.pipeThrough(
    new DecompressionStream("deflate"),
  );
  const decompressed = await new Response(stream).arrayBuffer();
  return new TextDecoder("latin1").decode(decompressed);
}

function indexOf(haystack: Uint8Array, needle: Uint8Array, from = 0): number {
  outer: for (let i = from; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

function lastIndexOf(haystack: Uint8Array, byte: number, before: number, count: number): number {
  let found = -1;
  let seen = 0;
  for (let i = before - 1; i >= 0; i--) {
    if (haystack[i] === byte) {
      found = i;
      if (++seen >= count) return found;
    }
  }
  return found;
}

function extractStringsFromStream(stream: string): string[] {
  const out: string[] = [];

  let m: RegExpExecArray | null;
  TEXT_OPS.lastIndex = 0;
  while ((m = TEXT_OPS.exec(stream)) !== null) {
    out.push(decodePdfString(m[1]));
  }

  TEXT_ARRAY_OPS.lastIndex = 0;
  while ((m = TEXT_ARRAY_OPS.exec(stream)) !== null) {
    const inner = m[1];
    const partRe = /\(((?:\\.|[^\\()])*)\)/g;
    let p: RegExpExecArray | null;
    const parts: string[] = [];
    while ((p = partRe.exec(inner)) !== null) {
      parts.push(decodePdfString(p[1]));
    }
    if (parts.length) out.push(parts.join(""));
  }

  return out;
}

function decodePdfString(raw: string): string {
  return raw
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\r")
    .replace(/\\t/g, "\t")
    .replace(/\\\(/g, "(")
    .replace(/\\\)/g, ")")
    .replace(/\\\\/g, "\\")
    .replace(/\\([0-7]{1,3})/g, (_, oct) =>
      String.fromCharCode(parseInt(oct, 8)),
    );
}
