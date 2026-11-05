// The my2cents.ai mark: a speech bubble with "2¢", the same shape the blog uses.
export function LogoMark({ className, size = 18 }: { className?: string; size?: number }) {
  return (
    <svg className={className} viewBox="0 0 32 38" width={size} height={Math.round((size * 38) / 32)} aria-hidden focusable="false">
      <path
        d="M6,0 H26 C29.3,0 32,2.7 32,6 V26 C32,29.3 29.3,32 26,32 L22,32 L26,37 L18,32 H6 C2.7,32 0,29.3 0,26 V6 C0,2.7 2.7,0 6,0Z"
        fill="var(--brass)"
      />
      <text x="16" y="22" textAnchor="middle" fontFamily="Georgia, serif" fontSize="16" fontWeight="bold" fill="var(--brass-ink)">
        2¢
      </text>
    </svg>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={className}>
      my2cents<span className="wordmark-tld">.ai</span>
    </span>
  );
}
