interface PageFooterProps {
  leftText?: string;
  rightText?: string;
}

export function PageFooter({
  leftText = 'Sample market data · Simulated trades only. No real orders are placed.',
  rightText = 'All times IST (UTC+5:30)',
}: PageFooterProps) {
  return (
    <footer className="mt-5 flex flex-col justify-between gap-1 border-t border-[#E4E4E7]/70 pt-3 text-[11px] text-[#71717A] sm:flex-row sm:items-center">
      <span>{leftText}</span>
      <span>{rightText}</span>
    </footer>
  );
}
