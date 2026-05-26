# Sample PDFs

This directory ships three public-domain US Government Accountability Office (GAO) Highlights reports for the PDF-summarizer demo. They are small, text-based PDFs with the standard GAO structure (`Why GAO Did This Study` / `What GAO Found` / `What GAO Recommends`) — which maps cleanly onto the blog's demo prompt asking for **scope, headline number, and risks** per document.

## What's in here

| File | Topic | Pages |
|---|---|---|
| `gao-cybersecurity.pdf` | High-Risk Series: Urgent Action Needed to Address Critical Cybersecurity Challenges Facing the Nation | 4 |
| `gao-dod-readiness.pdf` | Military Readiness: Actions Needed for DOD to Address Challenges across the Air, Sea, Ground, and Space Domains | 1 |
| `gao-supply-chain.pdf` | Supply Chain Security: Actions Needed to Improve CBP Management of the Customs Trade Partnership Against Terrorism Program | 1 |

Three deliberately different domains so the agent's three summaries differ in content rather than just numbers.

## Upload to R2

After you have created the `claude-managed-agents-pdfs` bucket (see the parent README's Quick-start), upload the three samples with their `pdfs/` prefix:

```bash
npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-cybersecurity.pdf --file ./samples/gao-cybersecurity.pdf
npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-dod-readiness.pdf --file ./samples/gao-dod-readiness.pdf
npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-supply-chain.pdf  --file ./samples/gao-supply-chain.pdf
```

## Sources & License

All three files are reproductions of original publications by the US Government Accountability Office (GAO). Works of the US federal government are not subject to copyright protection in the United States (17 U.S.C. § 105) and are in the public domain.

| File | GAO report number | Source URL |
|---|---|---|
| `gao-cybersecurity.pdf` | GAO-24-107231 | https://www.gao.gov/assets/gao-24-107231-highlights.pdf |
| `gao-dod-readiness.pdf` | GAO-24-107463 | https://www.gao.gov/assets/gao-24-107463-highlights.pdf |
| `gao-supply-chain.pdf` | GAO-26-107893 | https://www.gao.gov/assets/gao-26-107893-highlights.pdf |

If you want to substitute different PDFs, anything text-based (i.e. selectable text, not a scanned image) and Flate-compressed will work with the bundled `pdf-text.ts` extractor. Scanned PDFs and PDFs with non-standard glyph encodings will return `(no extractable text)`.
